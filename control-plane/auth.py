"""Edge auth via Authelia: bootstrap, derived file backend, nginx gate (M6.2a).

Account truth lives in the CP state store (db.py, Postgres). Authelia is a
stateless verifier: users.yaml / config.yml are derived artifacts and the
/run/lemonseed/auth-*.conf gate snippets are empty until the first admin
exists. All credential handling stays in the control plane (no bcrypt on the
installer).
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

import db
import data_services

AUTH_STATE_DIR = Path("/var/lib/authelia")
USERS_YAML = AUTH_STATE_DIR / "users.yaml"
CONFIG_YAML = AUTH_STATE_DIR / "config.yml"
GATE_CONF = Path("/run/lemonseed/auth-gate.conf")
ADMIN_CONF = Path("/run/lemonseed/auth-admin.conf")
# Staged by the USB installer (ESP) and relocated by lemonseed-auth-reset-seed.
RESET_MARKER_FILE = Path("/var/lib/lemonseed/reset-admin")

AUTH_HOST = "127.0.0.1"
AUTH_PORT = 9091
# Functional (not product-named) URL surface; the backend can be swapped later
# without touching the LAN surface.
AUTH_PATH = "/auth"
ADMIN_GROUP = "admin"
BASE_GROUP = "users"
# Dedicated Valkey logical DB for Authelia session keys (TTL = token expiry).
VALKEY_SESSION_DB = 1
PG_WAIT_SECONDS = 180

_USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")

# Set only from auth_request_set (Authelia response headers) — never from the
# client. Empty pre-auth → nginx omits the header entirely.
GATE_SNIPPET = """\
auth_request /_lemonseed/auth-verify;
auth_request_set $auth_user $upstream_http_remote_user;
auth_request_set $auth_email $upstream_http_remote_email;
auth_request_set $auth_groups $upstream_http_remote_groups;
error_page 401 = @auth_unauthorized;
"""

# /settings/ and other admin-only surfaces. The group check is enforced by
# the control plane (which receives X-Forwarded-Groups from nginx) because
# nginx `if` runs in the rewrite phase — before `auth_request` (access phase)
# sets $auth_groups. An nginx-level `if` here would always see "" and 403.
ADMIN_SNIPPET = ""

_gate_lock = threading.Lock()
_gate_synced = False


def _write_private(path: Path, content: str) -> None:
    """Atomically write a file Authelia (group) must read: 0640 root:authelia."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o640)
    try:
        import grp  # noqa: PLC0415

        os.chown(tmp, 0, grp.getgrnam("authelia").gr_gid)
    except (KeyError, OSError, AttributeError):
        pass
    tmp.replace(path)


def _write_conf(path: Path, content: str) -> None:
    """Atomically write an nginx gate snippet (read by the nginx master as root)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(path)


def _yaml_scalar(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_users_yaml(users: list[dict[str, Any]]) -> str:
    """Authelia 4.x file-backend YAML derived from CP users (many:many groups).

    4.x format: a `users` map keyed by username (not a 2.x-style list).
    """
    if not users:
        return "users: {}\n"
    lines = ["users:"]
    for user in users:
        username = str(user["username"])
        # displayname is required by the 4.x schema; fall back to the username.
        display_name = str(user.get("display_name") or username)
        lines.append(f"  {_yaml_scalar(username)}:")
        lines.append(f"    displayname: {_yaml_scalar(display_name)}")
        email = str(user.get("email") or "")
        if email:
            lines.append(f"    email: {_yaml_scalar(email)}")
        lines.append(f"    password: {_yaml_scalar(str(user['password_hash']))}")
        lines.append(f"    disabled: {'true' if user.get('disabled') else 'false'}")
        groups = list(user.get("groups") or [])
        if groups:
            lines.append("    groups:")
            for group in groups:
                lines.append(f"      - {_yaml_scalar(str(group))}")
    return "\n".join(lines) + "\n"


def render_config(session_secret: str, storage_encryption_key: str, domain: str) -> str:
    """Authelia 4.x config: loopback-only, file backend, Valkey sessions.

    - server.address uses the 4.x address syntax; the path is served at both /
      and /auth so nginx can keep a stable /auth/ prefix.
    - Password reset/change are disabled: users.yaml is a derived artifact of
      the CP state store, and lockout recovery goes through the installer +
      /setup (DESIGN M6.2).
    - The NTP startup check is disabled so the appliance boots offline.
    - session.cookies is required in 4.x; the domain is the appliance's mDNS
      hostname (set during setup). A hostname change requires re-setup.
    Generated once at first-admin bootstrap; the secrets persist in
    the file so reboots keep sessions and storage valid.
    """
    return f"""\
server:
  address: 'tcp://{AUTH_HOST}:{AUTH_PORT}{AUTH_PATH}'
  endpoints:
    authz:
      auth-request:
        implementation: "AuthRequest"

log:
  level: info

authentication_backend:
  password_change:
    disable: true
  password_reset:
    disable: true
  file:
    path: {USERS_YAML}
    watch: true

storage:
  local:
    path: {AUTH_STATE_DIR / 'db.sqlite3'}
  encryption_key: {storage_encryption_key}

session:
  secret: {session_secret}
  cookies:
    - name: authelia_session
      domain: {domain}
      same_site: lax
      inactivity: 1h
      expiration: 10h
      authelia_url: https://{domain}/auth/
      default_redirection_url: https://{domain}/
  redis:
    host: 127.0.0.1
    port: 6379
    database_index: {VALKEY_SESSION_DB}

access_control:
  default_policy: one_factor

notifier:
  filesystem:
    filename: {AUTH_STATE_DIR / 'notifications.txt'}

ntp:
  disable_startup_check: true
"""


def hash_password(password: str) -> str:
    """bcrypt hash via the bcrypt Python package (in the CP runtime)."""
    import bcrypt  # noqa: PLC0415

    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def reset_marker_present() -> bool:
    return RESET_MARKER_FILE.is_file()


def _load_users(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT username, display_name, email, password_hash, disabled
            FROM users ORDER BY username
            """
        )
        rows = cur.fetchall()
        cur.execute(
            """
            SELECT ug.username, ug.name
            FROM user_groups ug ORDER BY ug.username, ug.name
            """
        )
        groups_by_user: dict[str, list[str]] = {}
        for username, name in cur.fetchall():
            groups_by_user.setdefault(username, []).append(name)
    users: list[dict[str, Any]] = []
    for username, display_name, email, password_hash, disabled in rows:
        users.append(
            {
                "username": username,
                "display_name": display_name,
                "email": email,
                "password_hash": password_hash,
                "disabled": bool(disabled),
                "groups": groups_by_user.get(username, []),
            }
        )
    return users


def _pg_ready() -> bool:
    return bool(db.postgres_admin_dsn()) and db.pg_isready()


def admin_exists() -> Optional[bool]:
    """Enabled admin group members: True, False, or None when PG is not ready."""
    if not _pg_ready():
        return None
    try:
        conn = db.cp_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT 1 FROM user_groups ug
                    JOIN users u ON u.username = ug.username AND u.disabled = FALSE
                    WHERE ug.name = %s LIMIT 1
                    """,
                    (ADMIN_GROUP,),
                )
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — tri-state by design
        return None


def admin_bootstrap_required() -> bool:
    """/setup must offer admin creation iff admin group is empty or reset pending."""
    if reset_marker_present():
        return True
    return admin_exists() is not True


def account_counts() -> tuple[Optional[int], Optional[int]]:
    """(user_count, enabled_admin_count) or (None, None) when PG is not ready."""
    if not _pg_ready():
        return None, None
    try:
        conn = db.cp_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM users")
                user_count = int(cur.fetchone()[0])
                cur.execute(
                    """
                    SELECT COUNT(*) FROM user_groups ug
                    JOIN users u ON u.username = ug.username AND u.disabled = FALSE
                    WHERE ug.name = %s
                    """,
                    (ADMIN_GROUP,),
                )
                admin_count = int(cur.fetchone()[0])
            return user_count, admin_count
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None, None


def sync_users_yaml() -> bool:
    """Derive users.yaml from the CP state store (Authelia hot-reloads it)."""
    if not _pg_ready():
        return False
    try:
        conn = db.cp_connect()
        try:
            users = _load_users(conn)
        finally:
            conn.close()
        _rendered = render_users_yaml(users)
        _write_private(USERS_YAML, _rendered)
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_authelia_config(domain: str) -> bool:
    """Create config.yml exactly once (secrets generated at bootstrap)."""
    if CONFIG_YAML.is_file() and CONFIG_YAML.stat().st_size > 0:
        return True
    try:
        _write_private(
            CONFIG_YAML,
            render_config(secrets.token_hex(32), secrets.token_hex(32), domain),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def write_gate(enabled: bool) -> None:
    """Flip the nginx gate snippets (empty = open, same as pre-M6.2)."""
    _write_conf(GATE_CONF, GATE_SNIPPET if enabled else "")
    _write_conf(ADMIN_CONF, ADMIN_SNIPPET if enabled else "")


def reload_nginx() -> None:
    """Best-effort: nginx may not be up yet — it reads the files at start."""
    try:
        result = subprocess.run(
            ["systemctl", "reload", "nginx.service"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"nginx reload: {exc}", flush=True)
        return
    if result.returncode != 0:
        print(
            f"nginx reload failed ({result.returncode}): {result.stderr.strip()}",
            flush=True,
        )


def _authelia_health() -> str:
    import urllib.error
    import urllib.request

    url = f"http://{AUTH_HOST}:{AUTH_PORT}{AUTH_PATH}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=2):
            return "healthy"
    except (urllib.error.URLError, OSError, ValueError):
        return "down"


def auth_status() -> dict[str, Any]:
    """Cheap edge-auth summary for /api/status (live probe + counts)."""
    gate_enabled = GATE_CONF.is_file() and bool(GATE_CONF.read_text(encoding="utf-8").strip())
    user_count, admin_count = account_counts()
    return {
        "state": "enabled" if gate_enabled else "disabled",
        "service": _authelia_health(),
        "users": user_count,
        "admins": admin_count,
        "reset_pending": reset_marker_present(),
    }


def create_admin(username: str, password: str) -> dict[str, Any]:
    """Create the first admin (or consume a lockout reset). Synchronous.

    Waits bounded for Postgres, then: upsert user, ensure default groups and
    mappings, derive users.yaml/config.yml, flip the gate, reload nginx.
    Raises ValueError with a user-facing message on failure.
    """
    username = username.strip()[:64]
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError(
            "admin username must be 1-64 characters of a-z, 0-9, '_', '.' or '-'"
        )
    if len(password) < 8:
        raise ValueError("admin password must be at least 8 characters")

    reset = reset_marker_present()
    password_hash = hash_password(password)

    deadline = time.monotonic() + PG_WAIT_SECONDS
    while not _pg_ready():
        if time.monotonic() > deadline:
            raise ValueError(
                "PostgreSQL is not ready yet — wait a minute and submit setup again"
            )
        time.sleep(2)

    provision = data_services.ensure_provisioned()
    if provision.get("state") != "ready":
        raise ValueError(
            f"data services are not ready yet ({provision.get('state')}); "
            "wait a minute and submit setup again"
        )

    try:
        conn = db.cp_connect()
        with conn.cursor() as cur:
            if reset:
                # Demote all former admins; their other groups are preserved.
                cur.execute("DELETE FROM user_groups WHERE name = %s", (ADMIN_GROUP,))
            cur.execute(
                """
                INSERT INTO users (username, display_name, email, password_hash, disabled)
                VALUES (%s, %s, '', %s, FALSE)
                ON CONFLICT (username) DO UPDATE
                  SET display_name = EXCLUDED.display_name,
                      password_hash = EXCLUDED.password_hash,
                      disabled = FALSE
                """,
                (username, username, password_hash),
            )
            for group in (ADMIN_GROUP, BASE_GROUP):
                cur.execute(
                    "INSERT INTO groups (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                    (group,),
                )
            for group in (ADMIN_GROUP, BASE_GROUP):
                cur.execute(
                    """
                    INSERT INTO user_groups (username, name)
                    VALUES (%s, %s) ON CONFLICT (username, name) DO NOTHING
                    """,
                    (username, group),
                )
        conn.close()
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Could not create the admin account: {exc}") from exc

    if not sync_users_yaml():
        raise ValueError("Could not write the auth user store; submit setup again")
    hostname = Path("/etc/hostname").read_text(encoding="utf-8").strip() if Path("/etc/hostname").is_file() else "lemonseed"
    domain = f"{hostname}.local" if "." not in hostname else hostname
    if not ensure_authelia_config(domain):
        raise ValueError("Could not write the auth config; submit setup again")
    write_gate(True)
    reload_nginx()
    if reset:
        RESET_MARKER_FILE.unlink(missing_ok=True)
    return {"state": "ready", "username": username, "reset": reset}


def ensure_auth_gate_worker() -> None:
    """Re-assert the persistent gate state after reboot (/run is tmpfs).

    Non-blocking daemon thread, same pattern as the chat/data/modules workers.
    """

    def _run() -> None:
        global _gate_synced

        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if not _gate_lock.acquire(blocking=False):
                time.sleep(3)
                continue
            try:
                if _gate_synced:
                    return
                exists = admin_exists()
                if exists is None:
                    time.sleep(3)
                    continue
                write_gate(exists)
                if exists:
                    # Re-derive in case the /var files are ever missing; the
                    # existing config (and its secret) is preserved.
                    hostname = (
                        Path("/etc/hostname").read_text(encoding="utf-8").strip()
                        if Path("/etc/hostname").is_file()
                        else "lemonseed"
                    )
                    domain = f"{hostname}.local" if "." not in hostname else hostname
                    ensure_authelia_config(domain)
                    sync_users_yaml()
                    reload_nginx()
                _gate_synced = True
                return
            finally:
                _gate_lock.release()
        print(
            "lemonseed-auth: could not sync the auth gate (Postgres not ready)",
            flush=True,
        )

    thread = threading.Thread(target=_run, name="lemonseed-auth-gate", daemon=True)
    thread.start()
