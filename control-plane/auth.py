"""CP-native edge auth: login, signed session tokens, nginx gate (M6.2).

Account truth lives in the CP state store (db.py, Postgres). A cookie token is
only a *credential*; whether it is still live is decided by the Valkey-backed
session store (sessions.py, M6.6 D2), which also makes the idle window slide and
lets one account be revoked on its next request (M6.6 D1).
Sign-in is SRP-6a (srp.py), so the CP holds a verifier, never a password or a
password hash; the enrollment math runs in the browser.
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

import audit
import data_services
import db
import sessions
import srp

LEMONSEED_STATE_DIR = Path("/var/lib/lemonseed")
SESSION_SECRET_FILE = LEMONSEED_STATE_DIR / "session-secret"
GATE_CONF = Path("/run/lemonseed/auth-gate.conf")
ADMIN_CONF = Path("/run/lemonseed/auth-admin.conf")
# Persistent copies of the same snippets. /run is tmpfs and the boot seeder
# re-creates these files EMPTY before nginx starts, so a reboot opened a window
# during which the edge gate was off while the household cookie was already
# dead — an admin reaching /settings/ there got a bare needs-admin JSON. The
# seeder now restores the last-written state instead (bare-metal review).
GATE_PERSIST = LEMONSEED_STATE_DIR / "auth-gate.conf"
ADMIN_PERSIST = LEMONSEED_STATE_DIR / "auth-admin.conf"
# Written by the USB installer into the target's user-data (D-27). It used to
# name a `lemonseed-auth-reset-seed` unit that has never existed, which is why
# "Reset admin credentials" was a silent no-op on every install (F-2).
RESET_MARKER_FILE = LEMONSEED_STATE_DIR / "reset-admin"
# Public mirror of admin-group membership for the live installer, which cannot
# query Postgres (D-28). An affordance for informed consent, not a control.
ADMINS_FILE = LEMONSEED_STATE_DIR / "admins.json"

# cp_meta key naming the first-admin row (D-22). Reserved identities are
# identified *by row*, never by username: the first admin's name is whatever the
# operator typed at setup (`admin` is only the default), and anything that
# compares against the literal string breaks the day someone types another one.
FIRST_ADMIN_KEY = "first_admin"

ADMIN_GROUP = "admin"
BASE_GROUP = "users"
PG_WAIT_SECONDS = 180

# Idle/absolute session windows live in sessions.py (one source of truth).
SESSION_COOKIE = "lemonseed_session"

# Rate limiting: 5 failures per 5-minute window per IP → 15-min lockout.
RATE_LIMIT_MAX_FAILURES = 5
RATE_LIMIT_WINDOW_SECONDS = 300  # 5 min
RATE_LIMIT_LOCKOUT_SECONDS = 900  # 15 min

# Portal usernames (M7 P3, D-33): lower-case, letter-first, 2-16 characters.
# Short and boring on purpose — these land in log lines, ``ps`` output,
# home-directory-style paths and possibly PAM one day, so they stay inside the
# POSIX portable-filename charset and well under any length limit downstream.
# The first character must be a letter, which is also what refuses ".", ".."
# and every all-digits name.
USERNAME_RE = re.compile(r"[a-z][a-z0-9_.-]{1,15}")
USERNAME_MAX = 16

# Names a portal account must never own. Not a security boundary — a portal user
# cannot become these accounts — but squatting them makes a future that runs
# tasks as UIDs or exposes PAM into a migration we would rather not do.
# ``lemonseed`` is the prefix of the identities this appliance really creates
# (``lemonseed-cp`` the OWUI service account, ``lemonseed-lemond`` a host user).
# ``admin`` is deliberately NOT here: it is the documented default username, and
# the row it lands on is what D-22 protects, not the string.
RESERVED_USERNAMES = frozenset(
    {
        "root",
        "daemon",
        "bin",
        "sys",
        "sync",
        "games",
        "man",
        "lp",
        "mail",
        "news",
        "uucp",
        "proxy",
        "www-data",
        "nobody",
        "nscd",
        "nginx",
        "messagebus",
        "syslog",
        "dbus",
        "polkitd",
        "postgres",
        "valkey",
        "redis",
    }
)
RESERVED_USERNAME_PREFIXES = ("dhcp", "systemd", "_systemd", "lemonseed")

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
    """Identity email for a portal username (see OWUI_EMAIL_DOMAIN).

    **This, never ``users.email``, is what identifies an account to Open WebUI**
    (USERS-AND-GROUPS.md D-16'). ``users.email`` is the household's own address
    and the portal's only view of it; OWUI's identity string is derived from the
    immutable username, so nothing a user or an OWUI admin edits can move it —
    which is what makes the duplicate-account bug of D-15 structurally
    impossible rather than merely unlikely.
    """
    return f"{username}@{OWUI_EMAIL_DOMAIN}"


def normalize_username(raw: Any) -> str:
    """Trim and lower-case a username (D-33): ``John`` and ``john`` are one account.

    Must be applied on **every** path that accepts a username, and mirrored in
    the browser: the SRP identity string is hashed into ``x`` (D-4), so a path
    that skips it derives a *different verifier* from the same password and
    creates an account nobody can sign in to.
    """
    return str(raw or "").strip().lower()


def is_reserved_username(username: str) -> bool:
    return username in RESERVED_USERNAMES or username.startswith(
        RESERVED_USERNAME_PREFIXES
    )


def username_error(username: str) -> Optional[str]:
    """Why this (already normalised) username is unusable, or ``None``."""
    if not username:
        return "a username is required"
    if not USERNAME_RE.fullmatch(username):
        return (
            "username must be 2-16 characters: a letter first, then letters,"
            " digits, '_', '.' or '-'"
        )
    if username.endswith((".", "-")):
        return "username may not end in '.' or '-'"
    if is_reserved_username(username):
        return f"username '{username}' is reserved for the system"
    return None


# RFC 5322 dot-atom local part: the printable specials minus the ones that need
# quoting, in runs separated by single dots. Quoted strings and comments are
# refused deliberately — an address we could not type into a mail transport is
# not worth their parsing rules (D-34).
EMAIL_LOCAL_RE = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
    r"(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
)
EMAIL_DOMAIN_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+"
)


def normalize_email(raw: Any) -> str:
    """Trim and lower-case an address; email is case-insensitive (D-34)."""
    return str(raw or "").strip().lower()


def email_error(email: str) -> Optional[str]:
    """Why this address is unusable, or ``None``. Empty is valid (optional field).

    RFC 5322 addr-spec intersected with Open WebUI's own validator, which
    requires a dot in the domain and exempts only ``localhost``. Accepting
    anything OWUI would reject is a needless trap: the two records describe the
    same person even though the strings never have to match (D-16').
    """
    if not email:
        return None
    if len(email) > 254:
        return "email address is too long"
    local, sep, domain = email.rpartition("@")
    if not sep or not local or not domain or "@" in local:
        return "email address must look like name@example.com"
    if len(local) > 64:
        return "email address is too long before the '@'"
    if not EMAIL_LOCAL_RE.fullmatch(local):
        return (
            "email address may use letters, digits and !#$%&'*+/=?^_`{|}~-"
            " before the '@', separated by single dots"
        )
    if domain == OWUI_EMAIL_DOMAIN:
        return None
    if not EMAIL_DOMAIN_RE.fullmatch(domain):
        return "email domain must be a dotted name like example.com"
    return None


def display_name_error(name: Any) -> Optional[str]:
    """Validate a display name (D-25). It is forwarded in a header to Open WebUI,
    so control characters are refused rather than escaped somewhere later."""
    value = str(name or "").strip()
    if not value:
        return "a display name is required"
    if len(value) > 80:
        return "display name must be 80 characters or fewer"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "display name may not contain control characters"
    return None


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


def fold_secret() -> Optional[bytes]:
    """Seed for simulating unknown accounts (RFC 5054 §2.5.1.3).

    Reuses the session signing secret rather than introducing a second secret
    file. ``srp.fold_salt`` HMACs it under its own label, so this is a distinct
    subkey and not a second use of the signing key itself. ``None`` before the
    first admin exists, which is the same window in which there is no account to
    enumerate anyway.
    """
    return _load_session_secret()


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
        # The gate forwards this as X-Forwarded-Email, so it is the OWUI
        # identity — never users.email (D-16'). A household address in a real
        # profile field must not become the string OWUI selects accounts by.
        "e": owui_email(username),
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


def srp_load_user(username: str) -> Optional[dict[str, Any]]:
    """Load the row an SRP challenge or verify needs. ``None`` if unusable.

    Replaced ``verify_credentials`` outright rather than being added beside it.
    There is no code path left that takes a password, so there is no path that
    could send one.

    Both handshake phases call this. Verify re-reads instead of trusting the
    challenge, so an account disabled or deleted mid-handshake cannot complete.
    A missing ``srp_verifier`` is reported, not silently skipped: the caller must
    be able to tell "no such account" from "account that cannot sign in yet",
    and neither may become a distinguishable HTTP answer.
    """
    if not _pg_ready():
        return None
    try:
        conn = db.cp_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT username, display_name, email, srp_salt, srp_verifier, disabled,
                           password_reset_required
                    FROM users WHERE username = %s
                    """,
                    (username,),
                )
                row = cur.fetchone()
                if row is None or row[5]:  # not found, or disabled
                    return None
                cur.execute(
                    "SELECT name FROM user_groups WHERE username = %s ORDER BY name",
                    (username,),
                )
                groups = [r[0] for r in cur.fetchall()]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — service boundary; None means "answer 401"
        return None
    return {
        "username": row[0],
        "display_name": row[1],
        "email": row[2],
        "srp_salt": row[3] or "",
        "srp_verifier": row[4] or "",
        "groups": groups,
        # D-11: an unrotated one-time credential must not yield a session, so
        # verify needs the flag in the same read that authenticates.
        "password_reset_required": bool(row[6]),
    }


# --- Rate limiting ---
#
# Valkey first, per-process dicts as fallback. The old dicts were process state,
# so restarting the control plane — which an A/B update does on every upgrade —
# wiped every lockout and handed an attacker a fresh budget. That is the whole
# reason for the move, and ``test_lockout_survives_a_control_plane_restart`` is
# the test that the move actually bought something.
#
# Failure direction is **allow**, which is the opposite of the session store's
# fail-closed rule and deliberately so: a session store we cannot read must not
# serve an authenticated surface, but a counter we cannot read must not lock a
# whole household out of their own appliance. A degraded counter weakens a
# brute-force throttle; it never grants access.
#
# Keys are NOT boot-scoped, unlike session keys. Surviving a control-plane
# restart is the point; tying them to the boot id would throw that away.

RATE_LIMIT_KEY_PREFIX = "lemonseed:rl"
_rate_lock = threading.Lock()
_rate_failures: dict[str, list[float]] = {}  # ip → [timestamps]
_rate_lockouts: dict[str, float] = {}  # ip → unlock-at timestamp


def _failures_key(ip: str) -> str:
    return f"{RATE_LIMIT_KEY_PREFIX}:f:{ip}"


def _lockout_key(ip: str) -> str:
    return f"{RATE_LIMIT_KEY_PREFIX}:l:{ip}"


def _local_check(ip: str, now: float) -> bool:
    with _rate_lock:
        if now < _rate_lockouts.get(ip, 0):
            return False
        _rate_failures[ip] = [
            t for t in _rate_failures.get(ip, []) if now - t < RATE_LIMIT_WINDOW_SECONDS
        ]
        return True


def _local_record(ip: str, now: float) -> None:
    with _rate_lock:
        failures = _rate_failures.get(ip, []) + [now]
        _rate_failures[ip] = [t for t in failures if now - t < RATE_LIMIT_WINDOW_SECONDS]
        if len(_rate_failures[ip]) >= RATE_LIMIT_MAX_FAILURES:
            _rate_lockouts[ip] = now + RATE_LIMIT_LOCKOUT_SECONDS
            _rate_failures[ip] = []


def _local_reset(ip: str) -> None:
    with _rate_lock:
        _rate_failures.pop(ip, None)
        _rate_lockouts.pop(ip, None)


def rate_limit_check(ip: str) -> bool:
    """True if this IP may attempt to sign in; False while locked out."""
    now = time.time()
    try:
        return not sessions.client().exists(_lockout_key(ip))
    except Exception:  # noqa: BLE001 — unreachable counter must not lock anyone out
        sessions.forget_client()
        return _local_check(ip, now)


def rate_limit_record_failure(ip: str) -> None:
    """Record one failed attempt; trip the lockout at the threshold.

    The ``INCR`` and its ``EXPIRE`` share one pipeline, so a crash between them
    cannot leave a counter with no TTL — a permanent lockout is the worst
    possible outcome for a household, and is the one thing this design refuses.
    """
    now = time.time()
    # Mirrored locally even when Valkey works, so a store that dies mid-session
    # resumes from a counter that was actually being maintained.
    _local_record(ip, now)
    try:
        pipe = sessions.client().pipeline(transaction=True)
        pipe.incr(_failures_key(ip))
        pipe.expire(_failures_key(ip), RATE_LIMIT_WINDOW_SECONDS)
        count = int(pipe.execute()[0])
        if count >= RATE_LIMIT_MAX_FAILURES:
            sessions.client().set(_lockout_key(ip), "1", ex=RATE_LIMIT_LOCKOUT_SECONDS)
            sessions.client().delete(_failures_key(ip))
    except Exception:  # noqa: BLE001 — best effort; the local dicts already recorded
        sessions.forget_client()


def rate_limit_reset(ip: str) -> None:
    """Clear the counters after a successful sign-in."""
    _local_reset(ip)
    try:
        sessions.client().delete(_failures_key(ip), _lockout_key(ip))
    except Exception:  # noqa: BLE001 — a stale counter expires on its own
        sessions.forget_client()


def reset_marker_present() -> bool:
    return RESET_MARKER_FILE.is_file()


def _load_users(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT username, display_name, email, disabled,
                   password_reset_required, owui_id, last_login
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
    for (
        username,
        display_name,
        email,
        disabled,
        password_reset_required,
        owui_id,
        last_login,
    ) in rows:
        users.append(
            {
                "username": username,
                "display_name": display_name,
                "email": email,
                "disabled": bool(disabled),
                "password_reset_required": bool(password_reset_required),
                "owui_id": str(owui_id) if owui_id else "",
                "last_login": last_login.isoformat() if last_login else "",
                "groups": groups_by_user.get(username, []),
            }
        )
    return users


def load_users() -> list[dict[str, Any]]:
    """Every account with groups and flags, or ``[]`` when Postgres is unusable.

    ``[]`` means "no account truth this run", not "there are no accounts". Both
    callers treat it that way deliberately: the reconcile loop skips user writes
    rather than reading an empty desired-set as "delete everyone", and the portal
    shows an empty list rather than a wrong one.
    """
    if not _pg_ready():
        return []
    try:
        conn = db.cp_connect()
    except Exception:  # noqa: BLE001 — read boundary
        return []
    try:
        return _load_users(conn)
    except Exception:  # noqa: BLE001 — read boundary
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _pg_ready() -> bool:
    return bool(db.postgres_admin_dsn()) and db.pg_isready()


def pg_ready() -> bool:
    """Public form of the Postgres readiness check, for sibling modules."""
    return _pg_ready()


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


def ensure_system_groups(cur) -> None:
    """Idempotently seed ``admin``/``users`` and re-assert their system flag (D-3).

    Seeding is write-layer work, never migration SQL: the additive-only guard
    demands ``IF NOT EXISTS`` on every non-``ALTER`` statement, so an
    ``INSERT ... ON CONFLICT`` cannot appear there at all. Re-asserting
    ``system = TRUE`` on every call is what lets a hand-edited row converge
    without a backfill pass (D-31).
    """
    for name in (ADMIN_GROUP, BASE_GROUP):
        cur.execute(
            """
            INSERT INTO groups (name, system) VALUES (%s, TRUE)
            ON CONFLICT (name) DO UPDATE SET system = TRUE
            """,
            (name,),
        )


def list_admins() -> Optional[list[str]]:
    """Admin-group usernames, sorted; ``None`` when Postgres is not readable.

    The tri-state is not ceremony: a caller that reads ``None`` as "no admins"
    would publish an empty roster and tell the installer a box has no admin
    accounts to reset. Same convention as :func:`admin_exists`.
    """
    if not _pg_ready():
        return None
    try:
        conn = db.cp_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT username FROM user_groups WHERE name = %s ORDER BY username",
                    (ADMIN_GROUP,),
                )
                return [str(row[0]) for row in cur.fetchall()]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — affordance read path
        return None


def publish_admin_roster() -> Optional[list[str]]:
    """Publish ``admins.json`` for the live installer; return the roster (D-28).

    Called after every admin-group change so the checkbox a user ticks during a
    preserve-mode reinstall can name the accounts it is about to reset. A write
    failure is logged and skipped — the file is informed consent, not a control,
    and stale is tolerable while *wrong-but-confident* is not, which is why a
    Postgres read failure leaves the previous roster in place instead of
    publishing an empty one.
    """
    admins = list_admins()
    if admins is None:
        return None
    try:
        ADMINS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = ADMINS_FILE.with_name(ADMINS_FILE.name + ".tmp")
        tmp.write_text(json.dumps({"admins": admins}, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o644)
        tmp.replace(ADMINS_FILE)
    except OSError as exc:
        print(f"admin roster not published: {exc}", flush=True)
    return admins


def role(groups: list[str]) -> str:
    """OWUI role for a session: ``admin`` if in the admin group, else ``user``."""
    return "admin" if ADMIN_GROUP in {g.strip() for g in groups if g.strip()} else "user"


def owui_identities() -> list[dict[str, Any]]:
    """Reconcile intent for Open WebUI (M6.3b users collection).

    One entry per user row: email is the join key (always
    ``owui_email(username)``, never the row's own email — D-16') and is_admin
    mirrors admin-group membership. ``owui_id`` is Open WebUI's own id when the
    account was created by us, which is the key the mirror prefers (D-18); an
    empty string means "we do not know it", which is not the same as "there is no
    account".
    Empty list when Postgres is unavailable — the reconcile loop treats that
    as "no identity truth this run" and skips user writes (users' desired set
    empty means OWUI accounts only show as drift, never deletions).
    """
    if not _pg_ready():
        return []
    rows = load_users()
    return [
        {
            # Always derived, never the row's own email (D-16'); see owui_email.
            "email": owui_email(row["username"]),
            "name": str(row.get("display_name") or row["username"]),
            "is_admin": ADMIN_GROUP in (row.get("groups") or []),
            "disabled": bool(row.get("disabled")),
            "owui_id": str(row.get("owui_id") or ""),
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
    """Flip the nginx gate snippets (empty = open, same as pre-M6.2).

    Written twice on purpose: the live copy under /run that nginx includes,
    and a persistent copy under /var/lib that the boot seeder restores before
    nginx starts — otherwise every reboot began with the edge gate open until
    this worker finished waiting for Postgres.
    """
    gate = GATE_SNIPPET if enabled else ""
    admin = ADMIN_SNIPPET if enabled else ""
    _write_conf(GATE_CONF, gate)
    _write_conf(ADMIN_CONF, admin)
    _write_conf(GATE_PERSIST, gate)
    _write_conf(ADMIN_PERSIST, admin)


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


def _collect_former_admins(cur, keep: str) -> list[dict[str, Any]]:
    """Admin rows other than ``keep``, with what an OWUI cleanup needs (D-20a).

    Must run **before** the group joins change — afterwards the query has
    already forgotten who was an admin, and a missed straggler is exactly the
    account this rule exists to catch.
    """
    cur.execute(
        """
        SELECT username, owui_id FROM users
        WHERE username IN (SELECT username FROM user_groups WHERE name = %s)
          AND username <> %s
        ORDER BY username
        """,
        (ADMIN_GROUP, keep),
    )
    return [
        {
            "username": str(row[0]),
            "owui_id": str(row[1] or ""),
            "email": owui_email(str(row[0])),
        }
        for row in cur.fetchall()
    ]


def create_admin(
    username: str,
    salt_hex: str,
    verifier_hex: str,
    display_name: str = "",
) -> dict[str, Any]:
    """Create the first admin, or consume a reinstall's credential reset. Synchronous.

    Takes the ``(salt, verifier)`` the browser derived, never a password — see
    :func:`srp_load_user` for why the plaintext path is gone rather than being
    deprecated. Waits bounded for Postgres, then writes the row, seeds the system
    groups and (on a reset) removes the former admins in **one transaction**. Only
    after commit: session secret, gate flip, nginx reload, admin roster.
    Raises ValueError with a user-facing message on failure.

    ``owui_cleanup`` in the result names the identities whose Open WebUI accounts
    must follow their deleted CP rows (D-20a). HTTP calls cannot happen inside a
    transaction, so the caller makes them after commit — and the CP rows are gone
    whichever way those calls go, which is the point: a former admin must not be
    able to sign back in.

    Note what cannot be enforced here any more: a minimum password length. The
    server never sees the password, so that policy now lives in the setup form.
    That is a real weakening and is recorded as such in DESIGN.md §9 rather than
    being papered over.
    """
    username = normalize_username(username)
    problem = username_error(username)
    if problem:
        raise ValueError(problem)
    name = str(display_name or "").strip() or username
    problem = display_name_error(name)
    if problem:
        raise ValueError(problem)
    try:
        salt, verifier = srp.enroll(srp.SERVER_GROUP, salt_hex, verifier_hex)
    except srp.SrpError as exc:
        raise ValueError(f"The sign-in data from your browser was not valid: {exc}") from exc

    reset = reset_marker_present()
    salt_text = salt.hex()
    verifier_text = f"{verifier:x}"

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

    stragglers: list[dict[str, Any]] = []
    try:
        with db.cp_transaction() as conn:
            with conn.cursor() as cur:
                ensure_system_groups(cur)
                if reset:
                    # D-20a: enrolling a *different* username must not leave the
                    # old admin alive as an ordinary user holding a working
                    # verifier. Joins follow the row (ON DELETE CASCADE).
                    stragglers = _collect_former_admins(cur, username)
                    for gone in stragglers:
                        cur.execute(
                            "DELETE FROM users WHERE username = %s", (gone["username"],)
                        )
                        audit.record(
                            audit.ACTOR_SYSTEM,
                            gone["username"],
                            "user.delete",
                            {"via": "reinstall-reset-marker"},
                            cur=cur,
                        )
                cur.execute(
                    """
                    INSERT INTO users (username, display_name, email, disabled,
                                       srp_salt, srp_verifier)
                    VALUES (%s, %s, '', FALSE, %s, %s)
                    ON CONFLICT (username) DO UPDATE
                      SET display_name = EXCLUDED.display_name,
                          disabled = FALSE,
                          srp_salt = EXCLUDED.srp_salt,
                          srp_verifier = EXCLUDED.srp_verifier,
                          password_reset_required = FALSE,
                          token_generation = users.token_generation + 1
                    """,
                    (username, name, salt_text, verifier_text),
                )
                for group in (ADMIN_GROUP, BASE_GROUP):
                    cur.execute(
                        """
                        INSERT INTO user_groups (username, name)
                        VALUES (%s, %s) ON CONFLICT (username, name) DO NOTHING
                        """,
                        (username, group),
                    )
                audit.record(
                    audit.ACTOR_SYSTEM,
                    username,
                    "user.password_reset" if reset else "user.create",
                    {"groups": [ADMIN_GROUP, BASE_GROUP]},
                    cur=cur,
                )
                # D-22: name the first-admin row so "the account that opened this
                # appliance" stays identifiable after a rename, a new default, or
                # a reset that enrolls a different username. Written in this
                # transaction so the marker and the row commit together; overwrite
                # because consuming a reset marker makes the incoming account the
                # appliance's first admin, replacing rows this same transaction
                # just deleted (D-20a).
                cur.execute(
                    """
                    INSERT INTO cp_meta (key, value) VALUES (%s, %s::jsonb)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                    """,
                    (FIRST_ADMIN_KEY, json.dumps({"username": username})),
                )
    except Exception as exc:  # noqa: BLE001 — rolled back by the context manager
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
    publish_admin_roster()
    return {
        "state": "ready",
        "username": username,
        "reset": reset,
        "owui_cleanup": stragglers,
    }


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
