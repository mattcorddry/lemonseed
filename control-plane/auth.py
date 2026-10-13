"""CP-native edge auth: login, signed session tokens, nginx gate (M6.2).

Account truth lives in the CP state store (db.py, Postgres). A cookie token is
only a *credential*; whether it is still live is decided by the Valkey-backed
session store (sessions.py, M6.6 D2), which also makes the idle window slide and
lets one account be revoked on its next request (M6.6 D1).
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
from urllib.parse import unquote

import data_services
import db
import sessions

LEMONSEED_STATE_DIR = Path("/var/lib/lemonseed")
SESSION_SECRET_FILE = LEMONSEED_STATE_DIR / "session-secret"
GATE_CONF = Path("/run/lemonseed/auth-gate.conf")
ADMIN_CONF = Path("/run/lemonseed/auth-admin.conf")
# Staged by the USB installer (ESP) and relocated by lemonseed-auth-reset-seed.
RESET_MARKER_FILE = LEMONSEED_STATE_DIR / "reset-admin"

ADMIN_GROUP = "admin"
BASE_GROUP = "users"
PG_WAIT_SECONDS = 180

# Idle/absolute session windows live in sessions.py (one source of truth).
SESSION_COOKIE = "lemonseed_session"

# Rate limiting: 5 failures per 5-minute window per IP → 15-min lockout.
RATE_LIMIT_MAX_FAILURES = 5
RATE_LIMIT_WINDOW_SECONDS = 300  # 5 min
RATE_LIMIT_LOCKOUT_SECONDS = 900  # 15 min

_USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")

# Synthetic email domain for every portal identity (M6.3b). The appliance never
# sends mail, so the domain only has to satisfy Open WebUI's format check — it
# requires a dot in the domain but explicitly exempts ``@localhost``. That rules
# out ``@lemonseed`` (rejected by ``POST /api/v1/auths/add``, so OWUI's own admin
# UI could never create or edit such a row) and hostname-derived addresses, which
# would have to be re-derived after the box renames itself following the factory
# mDNS handoff — and OWUI keys identity on email, so a change duplicates the
# account instead of renaming it.
OWUI_EMAIL_DOMAIN = "localhost"


def owui_email(username: str) -> str:
    """Identity email for a portal username (see OWUI_EMAIL_DOMAIN)."""
    return f"{username}@{OWUI_EMAIL_DOMAIN}"

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


def safe_redirect_target(raw: str) -> str:
    """Sanitize a post-login redirect target to a same-origin relative path.

    The value arrives from a query string we did not compose ourselves, so the
    rule is: keep it only if no browser could resolve it off-appliance.

    Decoding *before* checking is deliberate -- ``%2f%2fevil.com`` must not
    clear a test that only looked at the encoded form. A ``\\`` is rejected
    because browsers resolve ``/\\host`` the same way they resolve ``//host``,
    which is exactly the bypass this check exists to close.
    """
    rd = unquote((raw or "").strip())
    if not rd.startswith("/"):
        return "/"
    # Only the path can change the origin; a ``//`` inside a query value is
    # data for our own page, not a host.
    path = rd.split("?", 1)[0].split("#", 1)[0]
    if path.startswith("//") or "//" in path or "\\" in path or ":" in path:
        return "/"
    return rd

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


def issue_token(
    username: str,
    email: str,
    groups: list[str],
    display_name: str = "",
    sid: str = "",
    generation: int = 0,
) -> str:
    """Create a signed session token: base64url(payload).base64url(hmac).

    ``display_name`` (claim ``n``) is the human-readable name forwarded to OWUI
    so it can label the account; it falls back to the username when empty.

    ``sid`` (claim ``s``) names the server-side session record, and ``gen`` is the
    account's session generation at mint time. Both are re-checked on verify, which
    is what turns this token from a self-sufficient credential into a reference to
    state that can be revoked.
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
            "s": sid,
            "gen": generation,
            "iat": now,
            "exp": now + sessions.SESSION_ABSOLUTE_SECONDS,
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


def _as_int(value: Any, default: int = -1) -> int:
    """Coerce a stored field without letting a bad value raise on the gate path."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def user_generation(username: str) -> Optional[int]:
    """Current session generation for an account; None if missing or disabled.

    None fails closed: ``verify_session`` reads it as "no live session", so a
    Postgres outage cannot leave stale sessions authenticating. The portal is
    already unusable without the data plane, and login has always required it.
    """
    if not _pg_ready():
        return None
    try:
        conn = db.cp_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT token_generation FROM users"
                    " WHERE username = %s AND disabled = FALSE",
                    (username,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — fail closed, same as a missing account
        return None
    return None if row is None else _as_int(row[0], 0)


def revoke_sessions(username: str) -> bool:
    """Invalidate every outstanding token for one account.

    Bumps the generation counter that tokens carry, so disable, demote, delete and
    "sign out everywhere" all take effect on the account's next request — the
    revocation path M6.2 never had. Not used by an ordinary logout, which must not
    evict the user's other devices.
    """
    if not _pg_ready():
        return False
    try:
        conn = db.cp_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET token_generation = token_generation + 1"
                    " WHERE username = %s",
                    (username,),
                )
                return cur.rowcount > 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — caller reports, and verify still fails closed
        return False


def create_session(user: dict[str, Any]) -> str:
    """Open a server-side session for a verified user; return its cookie token.

    Raises ``sessions.SessionStoreUnavailable`` when Valkey is down: issuing a
    cookie we could not later validate would hand out access that cannot be
    revoked or expired, which is worse than a failed sign-in.
    """
    username = str(user["username"])
    generation = user_generation(username)
    if generation is None:
        raise ValueError("account is missing or disabled")
    now = int(time.time())
    sid = secrets.token_urlsafe(16)
    record = {
        "u": username,
        "e": user.get("email", ""),
        "g": list(user.get("groups", [])),
        "n": user.get("display_name") or username,
        "gen": generation,
        "iat": now,
    }
    if not sessions.create(sid, record, now=now):
        raise sessions.SessionStoreUnavailable("session is outside its absolute window")
    return issue_token(
        username,
        record["e"],
        record["g"],
        record["n"],
        sid=sid,
        generation=generation,
    )


def verify_session(token: str) -> Optional[dict[str, Any]]:
    """The gate's full check. Returns the session record, or None to sign in again.

    Four things must all hold: the signature is valid, a live session record
    exists for its id, the account's generation still matches (revocation), and
    the absolute ceiling has not passed (which also slides the idle window).

    Raises ``sessions.SessionStoreUnavailable`` when the store cannot be reached.
    The handler answers 503 rather than 401 so "appliance is broken" and "please
    sign in" stay distinguishable all the way to the browser.
    """
    data = verify_token(token)
    if data is None:
        return None
    sid = str(data.get("s") or "")
    if not sid:
        # A cookie from before M6.6, or one forged without a session id: there is
        # no server-side record to trust, so it cannot authenticate.
        return None
    record = sessions.read(sid)
    if record is None or record.get("u") != data.get("u"):
        return None
    username = str(record.get("u") or "")
    generation = user_generation(username)
    if generation is None or generation != _as_int(record.get("gen")):
        return None
    if not sessions.refresh(sid, _as_int(record.get("iat"), 0)):
        return None
    return record


def drop_session(token: str) -> None:
    """Best-effort teardown at logout. The caller clears the cookie regardless.

    Only a token whose signature verifies contributes a session id, so an
    unauthenticated request cannot delete someone else's session.
    """
    data = verify_token(token)
    sid = str((data or {}).get("s") or "")
    if sid:
        sessions.drop_quietly(sid)


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


def owui_identities() -> list[dict[str, Any]]:
    """Reconcile intent for Open WebUI (M6.3b users collection).

    One entry per user row: email is the join key (``owui_email(username)``,
    or the row's own email when it has one) and is_admin mirrors admin-group
    membership.
    Empty list when Postgres is unavailable — the reconcile loop treats that
    as "no identity truth this run" and skips user writes (users' desired set
    empty means OWUI accounts only show as drift, never deletions).
    """
    if not _pg_ready():
        return []
    try:
        conn = db.cp_connect()
    except Exception:  # noqa: BLE001 — reconcile boundary
        return []
    try:
        rows = _load_users(conn)
    except Exception:  # noqa: BLE001 — reconcile boundary
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return [
        {
            "email": str(row.get("email") or owui_email(row["username"])),
            "name": str(row.get("display_name") or row["username"]),
            "is_admin": ADMIN_GROUP in (row.get("groups") or []),
            "disabled": bool(row.get("disabled")),
        }
        for row in rows
    ]


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
            check=False,
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
    email = owui_email(username)

    reset = reset_marker_present()
    password_hash = hash_password(password)

    deadline = time.monotonic() + PG_WAIT_SECONDS
    while not _pg_ready():
        if time.monotonic() > deadline:
            raise ValueError(
                "PostgreSQL is not ready yet — wait a minute and submit setup again"
            )
        time.sleep(2)

    # Provisioning (CP + module databases) may still be running even once
    # Postgres accepts connections; wait it out under the same deadline.
    provision = data_services.ensure_provisioned()
    while provision.get("state") != "ready":
        if time.monotonic() > deadline:
            raise ValueError(
                f"data services are not ready yet ({provision.get('state')}); "
                "wait a minute and submit setup again"
            )
        time.sleep(2)
        provision = data_services.ensure_provisioned()

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
