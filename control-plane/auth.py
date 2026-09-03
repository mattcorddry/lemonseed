"""CP-native edge auth: login, signed session tokens, nginx gate (M6.2).

Account truth lives in the CP state store (db.py, Postgres). Sessions are
stateless HMAC-SHA256 signed tokens in a cookie — no session store to GC.
All credential handling stays in the control plane (no bcrypt on the
installer).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path
from typing import Any, Optional

import db
import data_services

LEMONSEED_STATE_DIR = Path("/var/lib/lemonseed")
SESSION_SECRET_FILE = LEMONSEED_STATE_DIR / "session-secret"
GATE_CONF = Path("/run/lemonseed/auth-gate.conf")
ADMIN_CONF = Path("/run/lemonseed/auth-admin.conf")
# Staged by the USB installer (ESP) and relocated by lemonseed-auth-reset-seed.
RESET_MARKER_FILE = LEMONSEED_STATE_DIR / "reset-admin"

ADMIN_GROUP = "admin"
BASE_GROUP = "users"
PG_WAIT_SECONDS = 180

# Session token parameters.
SESSION_INACTIVITY_SECONDS = 3600  # 1h
SESSION_ABSOLUTE_SECONDS = 36000  # 10h
SESSION_COOKIE = "lemonseed_session"

# Rate limiting: 5 failures per 5-minute window per IP → 15-min lockout.
RATE_LIMIT_MAX_FAILURES = 5
RATE_LIMIT_WINDOW_SECONDS = 300  # 5 min
RATE_LIMIT_LOCKOUT_SECONDS = 900  # 15 min

_USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")

# Set only from auth_request_set (CP session-verify response headers) — never
# from the client. Empty pre-auth → nginx omits the header entirely.
GATE_SNIPPET = """\
auth_request /_lemonseed/session-verify;
auth_request_set $auth_user $upstream_http_x_forwarded_user;
auth_request_set $auth_email $upstream_http_x_forwarded_email;
auth_request_set $auth_groups $upstream_http_x_forwarded_groups;
auth_request_set $auth_name $upstream_http_x_forwarded_name;
auth_request_set $auth_role $upstream_http_x_forwarded_role;
error_page 401 = @auth_unauthorized;
"""

# Admin group check is enforced by the CP (X-Forwarded-Groups), not nginx `if`
# (which runs in the rewrite phase, before auth_request sets variables).
ADMIN_SNIPPET = ""

_gate_lock = threading.Lock()
_gate_synced = False


def _write_conf(path: Path, content: str) -> None:
    """Atomically write an nginx gate snippet (read by the nginx master as root)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o644)
    tmp.replace(path)


def _load_session_secret() -> Optional[bytes]:
    """Read the 256-bit session signing secret (0600)."""
    try:
        data = SESSION_SECRET_FILE.read_bytes()
        if len(data) == 32:
            return data
    except OSError:
        pass
    return None


def _save_session_secret() -> bytes:
    """Generate and persist a new 256-bit session secret (0600)."""
    secret = secrets.token_bytes(32)
    SESSION_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SESSION_SECRET_FILE.with_name(SESSION_SECRET_FILE.name + ".tmp")
    tmp.write_bytes(secret)
    os.chmod(tmp, 0o600)
    tmp.replace(SESSION_SECRET_FILE)
    return secret


def ensure_session_secret() -> bytes:
    """Return the session secret, generating it if absent (first-admin bootstrap)."""
    existing = _load_session_secret()
    if existing is not None:
        return existing
    return _save_session_secret()


def rotate_session_secret() -> None:
    """Regenerate the session secret (invalidates all active sessions)."""
    _save_session_secret()


def _sign(payload: bytes, secret: bytes) -> bytes:
    return hmac.new(secret, payload, hashlib.sha256).digest()


def issue_token(username: str, email: str, groups: list[str], display_name: str = "") -> str:
    """Create a signed session token: base64url(payload).base64url(hmac).

    ``display_name`` (claim ``n``) is the human-readable name forwarded to OWUI
    so it can label the account; it falls back to the username when empty.
    """
    secret = _load_session_secret()
    if secret is None:
        raise ValueError("session secret not initialized")
    now = int(time.time())
    payload = json.dumps(
        {
            "u": username,
            "e": email,
            "g": groups,
            "n": display_name or username,
            "iat": now,
            "exp": now + SESSION_ABSOLUTE_SECONDS,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    sig = _sign(payload, secret)
    return urlsafe_b64encode(payload).decode() + "." + urlsafe_b64encode(sig).decode()


def verify_token(token: str) -> Optional[dict[str, Any]]:
    """Verify a session token. Returns the payload dict or None if invalid/expired."""
    secret = _load_session_secret()
    if secret is None:
        return None
    try:
        b64_payload, b64_sig = token.split(".", 1)
        payload = urlsafe_b64decode(b64_payload.encode())
        sig = urlsafe_b64decode(b64_sig.encode())
    except (ValueError, UnicodeDecodeError):
        return None
    expected_sig = _sign(payload, secret)
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        data = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


def verify_credentials(username: str, password: str) -> Optional[dict[str, Any]]:
    """Check username/password against Postgres. Returns user dict or None."""
    import bcrypt  # noqa: PLC0415

    if not _pg_ready():
        return None
    try:
        conn = db.cp_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT username, display_name, email, password_hash, disabled FROM users WHERE username = %s",
                    (username,),
                )
                row = cur.fetchone()
                if row is None or row[4]:  # not found or disabled
                    return None
                cur.execute(
                    "SELECT name FROM user_groups WHERE username = %s ORDER BY name",
                    (username,),
                )
                groups = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None
    try:
        if not bcrypt.checkpw(password.encode("utf-8"), row[3].encode("utf-8")):
            return None
    except (ValueError, TypeError):
        return None
    return {
        "username": row[0],
        "display_name": row[1],
        "email": row[2],
        "groups": groups,
    }


# --- Rate limiting (in-memory, per-IP sliding window) ---

_rate_lock = threading.Lock()
_rate_failures: dict[str, list[float]] = {}  # ip → [timestamps]
_rate_lockouts: dict[str, float] = {}  # ip → unlock-at timestamp


def rate_limit_check(ip: str) -> bool:
    """Return True if the IP is allowed to attempt login. False if locked out."""
    now = time.time()
    with _rate_lock:
        locked_until = _rate_lockouts.get(ip, 0)
        if now < locked_until:
            return False
        # Prune old failures outside the window.
        failures = _rate_failures.get(ip, [])
        _rate_failures[ip] = [t for t in failures if now - t < RATE_LIMIT_WINDOW_SECONDS]
        return True


def rate_limit_record_failure(ip: str) -> None:
    """Record a failed login attempt; lock out if threshold exceeded."""
    now = time.time()
    with _rate_lock:
        failures = _rate_failures.setdefault(ip, [])
        failures.append(now)
        # Prune old entries.
        _rate_failures[ip] = [t for t in failures if now - t < RATE_LIMIT_WINDOW_SECONDS]
        if len(_rate_failures[ip]) >= RATE_LIMIT_MAX_FAILURES:
            _rate_lockouts[ip] = now + RATE_LIMIT_LOCKOUT_SECONDS
            _rate_failures[ip] = []


def rate_limit_reset(ip: str) -> None:
    """Clear failure count on successful login."""
    with _rate_lock:
        _rate_failures.pop(ip, None)
        _rate_lockouts.pop(ip, None)


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


def role(groups: list[str]) -> str:
    """OWUI role for a session: ``admin`` if in the admin group, else ``user``."""
    return "admin" if ADMIN_GROUP in {g.strip() for g in groups if g.strip()} else "user"


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


def auth_status() -> dict[str, Any]:
    """Cheap edge-auth summary for /api/status."""
    gate_enabled = GATE_CONF.is_file() and bool(GATE_CONF.read_text(encoding="utf-8").strip())
    user_count, admin_count = account_counts()
    return {
        "state": "enabled" if gate_enabled else "disabled",
        "users": user_count,
        "admins": admin_count,
        "reset_pending": reset_marker_present(),
    }


def create_admin(username: str, password: str) -> dict[str, Any]:
    """Create the first admin (or consume a lockout reset). Synchronous.

    Waits bounded for Postgres, then: upsert user, ensure default groups and
    mappings, generate session secret, flip the gate, reload nginx.
    Raises ValueError with a user-facing message on failure.
    """
    username = username.strip()[:64]
    if not _USERNAME_RE.fullmatch(username):
        raise ValueError(
            "admin username must be 1-64 characters of a-z, 0-9, '_' '.' or '-'"
        )
    if len(password) < 8:
        raise ValueError("admin password must be at least 8 characters")

    # OWUI trusted-header auth keys identity on email; the setup form has no
    # email field, so derive a deterministic local-part fallback to guarantee a
    # non-empty identity (and a stable one across re-logins / resets).
    email = f"{username}@lemonseed.local"

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
                VALUES (%s, %s, %s, %s, FALSE)
                ON CONFLICT (username) DO UPDATE
                  SET display_name = EXCLUDED.display_name,
                      email = EXCLUDED.email,
                      password_hash = EXCLUDED.password_hash,
                      disabled = FALSE
                """,
                (username, username, email, password_hash),
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

    # Generate the session signing secret (first-admin bootstrap).
    try:
        ensure_session_secret()
    except OSError as exc:
        raise ValueError(f"Could not initialize the session store: {exc}") from exc

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
                    # Ensure the session secret exists (reboot; /var persists).
                    ensure_session_secret()
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
