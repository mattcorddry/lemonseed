"""Account primitives: the only code allowed to write ``users`` (M7 P3, D-32).

Every primitive is the same shape — validate → guard → CP transaction → Open
WebUI → audit row → notify — and the order is not arbitrary:

* **Guards run inside the transaction** (D-2), because a UI that hides a button
  is not a guard. A direct API call must hit the same wall.
* **The CP row is the truth and Open WebUI is a mirror** (D-18). The CP row is
  written first, so an Open WebUI failure afterwards is drift the reconcile loop
  repairs; the reverse order would leave the portal pointing at an account that
  does not exist.

Sign-in itself lives in ``auth``/``srp``; this module is management. Nothing here
can store a password: the server holds ``v = g^x mod N``, and no code path in the
control plane accepts a password at all. "An account was created that nobody can
sign in to" is impossible only if every write goes through this file — hence
D-32's rule that route handlers do not touch ``users`` themselves.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from typing import Any, Callable, Optional

import audit
import auth
import db
import modules
import srp

# Events passed to observers (D-32). Strings, not enums: a future module should
# be able to subscribe to an event this file does not know about yet.
EVENT_CREATED = "user.created"
EVENT_RENAMED = "user.renamed"
EVENT_EMAIL_CHANGED = "user.email_changed"
EVENT_PASSWORD_ROTATED = "user.password_rotated"
EVENT_PASSWORD_RESET = "user.password_reset"
EVENT_DISABLED = "user.disabled"
EVENT_ENABLED = "user.enabled"
EVENT_DELETED = "user.deleted"
EVENT_GROUPS_CHANGED = "user.groups_changed"

# cp_meta key naming the first-admin row (D-22). Defined in ``auth`` because
# ``create_admin`` writes it inside its own transaction; see the note there for
# why a reserved identity is identified by row and never by username.
FIRST_ADMIN_KEY = auth.FIRST_ADMIN_KEY

# Same shape as usernames (D-33) but 32 characters: group names travel in the
# X-Forwarded-Groups header, so they stay in the same portable charset.
GROUP_NAME_RE = re.compile(r"[a-z][a-z0-9_.-]{1,31}")

# A portal account may not be deleted/disabled out from under an appliance that
# would then have nobody who can administer it.
_LAST_ADMIN = "this is the last enabled admin account, and the appliance must " \
              "always keep one"
_SELF_ACTION = "an admin account cannot do that to itself"

_observers: list[Callable[[str, dict[str, Any]], None]] = []
_observers_lock = threading.Lock()


# --------------------------------------------------------------------------
# Observers (D-32)
# --------------------------------------------------------------------------


def register_observer(fn: Callable[[str, dict[str, Any]], None]) -> None:
    """Subscribe to account events, called after the transaction commits.

    The whole extension point for a module that must keep its own database in
    step with the account list. No plugin framework, no discovery, no config.
    """
    with _observers_lock:
        if fn not in _observers:
            _observers.append(fn)


def notify(event: str, payload: dict[str, Any]) -> None:
    """Fire an event to every observer. Never raises, never rolls anything back.

    Runs after commit, so an observer cannot see a rolled-back account. A handler
    that raises is logged and skipped: someone else's broken sync must not fail
    the operation the operator just performed.
    """
    with _observers_lock:
        handlers = list(_observers)
    for handler in handlers:
        try:
            handler(event, dict(payload))
        except Exception as exc:  # noqa: BLE001 — observer boundary, by design
            print(f"usermgmt observer {getattr(handler, '__name__', handler)}"
                  f" {event}: {exc}", flush=True)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def list_users() -> list[dict[str, Any]]:
    """All accounts with groups, flags and last sign-in. ``[]`` if PG is down."""
    return auth.load_users()


def get(username: str) -> Optional[dict[str, Any]]:
    """One account as the portal sees it, or ``None``. Never includes secrets."""
    name = auth.normalize_username(username)
    return next((row for row in list_users() if row["username"] == name), None)


def first_admin() -> str:
    """Username of the first-admin row, or ``""`` when none was recorded (D-22)."""
    value = db.cp_get_meta(FIRST_ADMIN_KEY)
    if isinstance(value, dict):
        return str(value.get("username") or "")
    if isinstance(value, str):
        return value
    return ""


# --------------------------------------------------------------------------
# Guards (D-2). All of these run with the write transaction open.
# --------------------------------------------------------------------------


def _lock_admin_group(cur) -> None:
    """Serialize admin-group membership changes on the ``admin`` group row.

    Without this, two admins demoting each other concurrently both see "one admin
    will remain", both commit, and the appliance is left with nobody who can
    administer it. Locking that one row makes such changes serial in the database
    rather than racing in Python, and costs nothing at household scale.
    """
    cur.execute("SELECT 1 FROM groups WHERE name = %s FOR UPDATE", (auth.ADMIN_GROUP,))


def _enabled_admin_count(cur) -> int:
    cur.execute(
        """
        SELECT COUNT(*) FROM user_groups ug
        JOIN users u ON u.username = ug.username AND u.disabled = FALSE
        WHERE ug.name = %s
        """,
        (auth.ADMIN_GROUP,),
    )
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _in_group(cur, username: str, group: str) -> bool:
    cur.execute(
        "SELECT 1 FROM user_groups WHERE username = %s AND name = %s", (username, group)
    )
    return cur.fetchone() is not None


def _current_groups(cur, username: str) -> list[str]:
    cur.execute("SELECT name FROM user_groups WHERE username = %s", (username,))
    return [str(row[0]) for row in cur.fetchall()]


def _row(cur, username: str) -> Optional[tuple]:
    cur.execute(
        """
        SELECT display_name, email, disabled, srp_salt, srp_verifier,
               password_reset_required, owui_id, token_generation
        FROM users WHERE username = %s
        """,
        (username,),
    )
    return cur.fetchone()


def _require_target(cur, username: str) -> tuple:
    row = _row(cur, username)
    if row is None:
        raise ValueError(f"account '{username}' does not exist")
    return row


def _require_group_exists(cur, name: str) -> None:
    cur.execute("SELECT 1 FROM groups WHERE name = %s", (name,))
    if cur.fetchone() is None:
        raise ValueError(f"group '{name}' does not exist")


def _require_not_reserved(username: str) -> None:
    """Refuse to manage a name this appliance reserves (D-22, D-33).

    The service account is not a portal user at all — it is filtered out of the
    reconcile loop's observations and protected by two guards in the Open WebUI
    manifest — but a portal that could delete it by typing its name would be one
    typo from an unrecoverable box.
    """
    if auth.is_reserved_username(username):
        raise ValueError(f"account '{username}' is a system identity")


# --------------------------------------------------------------------------
# Open WebUI mirror
#
# Best-effort *by direction*, not by neglect: a failed push is reported to the
# caller and repaired by the reconcile loop, because the CP row is the truth
# (D-18). The one place it is not best-effort is ``delete``, where a refusal must
# stop the deletion (see below).
# --------------------------------------------------------------------------


def _owui_client() -> Any:
    try:
        return modules.owui_admin_client()
    except Exception as exc:  # noqa: BLE001 — mirror boundary
        print(f"usermgmt: owui client: {exc}", flush=True)
        return None


def _owui_find(client: Any, email: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Look up an Open WebUI account by identity email → ``(found, error)``.

    The two must be distinguishable: "no such account" is the normal case, and
    mistaking a failed call for it is how you overwrite a real account.
    """
    result = client.users_all()
    if not result.get("ok"):
        return None, f"Open WebUI could not list its accounts: {result.get('error')}"
    data = result.get("data")
    items = data.get("users") if isinstance(data, dict) else data
    for item in items or []:
        if isinstance(item, dict) and str(item.get("email") or "").lower() == email:
            return {"id": str(item.get("id") or ""), "name": str(item.get("name") or "")}, None
    return None, None


def _owui_delete(client: Any, user_id: str) -> Optional[str]:
    """Delete one Open WebUI account; ``None`` on success, else why not.

    Open WebUI refuses to delete the account it considers first
    (``users.py:990-1000``), and that refusal must reach the operator as words.
    Reporting success anyway is the worse failure: the portal would show a
    vanished person whose conversations are still live under a working login.
    """
    if not user_id:
        return None
    result = client.user_delete(user_id)
    if result.get("ok"):
        return None
    return f"Open WebUI refused to delete the account: {result.get('error')}"


def _owui_provision(
    username: str, display_name: str, is_admin: bool
) -> tuple[Optional[str], dict[str, Any]]:
    """Create the Open WebUI account for a portal user. Never raises.

    ``(owui_id, status)``. When the account could not be created, ``owui_id`` is
    ``None`` and ``status`` says why in words the portal can repeat; the CP row
    still stands, and the reconcile loop creates the Open WebUI account later
    (D-18). A household member who can sign in to the portal now but not yet to
    the module is a better outcome than a member who cannot be created at all.
    """
    email = auth.owui_email(username)
    client = _owui_client()
    if client is None:
        return None, {"ok": False, "reason": "Open WebUI is not running yet"}
    existing, error = _owui_find(client, email)
    if error:
        return None, {"ok": False, "reason": error}
    if existing is not None:
        # D-16a: never adopt. The alternative hands this person the previous
        # person's conversations — the specific harm this rule exists to prevent,
        # and it happens silently.
        refusal = _owui_delete(client, existing["id"])
        if refusal:
            return None, {"ok": False, "reason": refusal}
    created = client.user_create(
        {
            "email": email,
            "name": display_name or username,
            "role": "admin" if is_admin else "user",
            # No OWUI password path exists on this appliance (trusted-header auth
            # refuses password changes), so this value can never match anything
            # and is never handed to anyone. Same as UsersHandler.apply().
            "password": secrets.token_urlsafe(16),
        }
    )
    if not created.get("ok"):
        return None, {
            "ok": False,
            "reason": f"Open WebUI could not create the account: {created.get('error')}",
        }
    data = created.get("data")
    owui_id = str(data.get("id") or "") if isinstance(data, dict) else ""
    if not owui_id:
        # A create that does not report the id cannot be updated or deleted
        # later, so treat it as not created rather than as success.
        return None, {
            "ok": False,
            "reason": "Open WebUI created the account but did not report its id",
        }
    return owui_id, {"ok": True, "reason": ""}


def _store_owui_id(username: str, owui_id: str) -> None:
    """Record the Open WebUI id captured at create time (D-31: capture, never adopt).

    A separate statement after the CP row, deliberately: reserving the username
    first is what makes a duplicate create fail *before* any Open WebUI call, and
    the alternative ordering has a concurrent duplicate delete a live account on
    its way to failing. A failure here only costs a NULL id, which the reconcile
    loop tolerates (it matches the account by its derived identity email).
    """
    try:
        conn = db.cp_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET owui_id = %s WHERE username = %s AND owui_id IS NULL",
                    (owui_id, username),
                )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — bookkeeping; the account is real
        print(f"usermgmt owui_id {username}: {exc}", flush=True)


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------


def create(
    username: str,
    display_name: str,
    email: str,
    salt_hex: str,
    verifier_hex: str,
    *,
    is_admin: bool = False,
    actor: str = "",
) -> dict[str, Any]:
    """Create a household account from a browser-derived SRP enrollment.

    The caller supplies ``(salt, verifier)`` computed in the browser; a password
    never reaches this process. That is why creating an account has no "generate
    a temporary password" step, and why the admin-reset flow (D-10) looks the way
    it does.

    Returns ``{"state", "user", "owui"}``. ``owui`` is reported rather than
    raised — see :func:`_owui_provision`.

    The new account is created with ``password_reset_required`` set, which is
    what makes the first sign-in a rotation (D-11). It has to be: the admin's
    browser derived this verifier, so the admin *knows* the credential and could
    otherwise sign in as this person and read their conversations (D-13). Setup's
    ``auth.create_admin`` deliberately does the opposite — there the person typed
    their own password, so nobody else holds it.
    """
    username = auth.normalize_username(username)
    problem = auth.username_error(username)
    if problem:
        raise ValueError(problem)
    name = str(display_name or "").strip()
    problem = auth.display_name_error(name)
    if problem:
        raise ValueError(problem)
    email = auth.normalize_email(email)
    problem = auth.email_error(email)
    if problem:
        raise ValueError(problem)
    try:
        salt, verifier = srp.enroll(srp.SERVER_GROUP, salt_hex, verifier_hex)
    except srp.SrpError as exc:
        raise ValueError(f"The sign-in data from your browser was not valid: {exc}") from exc

    groups = [auth.BASE_GROUP, auth.ADMIN_GROUP] if is_admin else [auth.BASE_GROUP]
    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            auth.ensure_system_groups(cur)
            if _row(cur, username) is not None:
                raise ValueError(f"an account named '{username}' already exists")
            cur.execute(
                """
                INSERT INTO users (username, display_name, email, disabled,
                                   srp_salt, srp_verifier, password_reset_required)
                VALUES (%s, %s, %s, FALSE, %s, %s, TRUE)
                """,
                (username, name, email, salt.hex(), f"{verifier:x}"),
            )
            for group in groups:
                cur.execute(
                    """
                    INSERT INTO user_groups (username, name)
                    VALUES (%s, %s) ON CONFLICT (username, name) DO NOTHING
                    """,
                    (username, group),
                )
            audit.record(
                actor or audit.ACTOR_CP, username, "user.create",
                {"groups": groups}, cur=cur,
            )

    owui_id, owui_status = _owui_provision(username, name, is_admin)
    if owui_id:
        _store_owui_id(username, owui_id)

    notify(EVENT_CREATED, {"username": username, "is_admin": is_admin, "actor": actor})
    modules.request_policy_reconcile()
    return {
        "state": "ready",
        "user": get(username) or {"username": username, "display_name": name},
        "owui": owui_status,
    }


def set_display_name(username: str, display_name: str, *, actor: str = "") -> dict[str, Any]:
    """Rename the *display* name. The username itself is immutable (D-4).

    ``x`` binds the identity string inside the SRP hash, so changing a username
    invalidates the stored verifier and the account becomes unsignable. What the
    portal offers is the label, and the label must say so.
    """
    username = auth.normalize_username(username)
    name = str(display_name or "").strip()
    problem = auth.display_name_error(name)
    if problem:
        raise ValueError(problem)
    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            row = _require_target(cur, username)
            owui_id = str(row[6] or "")
            cur.execute(
                "UPDATE users SET display_name = %s WHERE username = %s", (name, username)
            )
            audit.record(
                actor or audit.ACTOR_CP, username, "user.rename",
                {"fields": ["display_name"]}, cur=cur,
            )
    # Best-effort push. Nothing selects a user by name, and the trusted name
    # header is read only when Open WebUI creates the account, so a stale copy
    # there is cosmetic (D-17a), never a wrong-permissions bug.
    problem = None
    client = _owui_client()
    if client is not None and owui_id:
        result = client.user_update(owui_id, {"name": name})
        if not result.get("ok"):
            problem = f"Open WebUI kept the old name: {result.get('error')}"
    notify(EVENT_RENAMED, {"username": username, "display_name": name, "actor": actor})
    return {"state": "ready", "username": username, "owui_error": problem}


def set_email(username: str, email: str, *, actor: str = "") -> dict[str, Any]:
    """Set the household address. **No Open WebUI call** (D-16').

    Open WebUI's identity is ``{username}@localhost``, always derived. Mirroring
    a household address into it is the F-1 defect that produced the duplicate
    admin account. The audit row records that the email changed, never the
    address itself (D-37).
    """
    username = auth.normalize_username(username)
    email = auth.normalize_email(email)
    problem = auth.email_error(email)
    if problem:
        raise ValueError(problem)
    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            _require_target(cur, username)
            cur.execute("UPDATE users SET email = %s WHERE username = %s", (email, username))
            audit.record(
                actor or audit.ACTOR_CP, username, "user.email_changed",
                {"fields": ["email"]}, cur=cur,
            )
    notify(EVENT_EMAIL_CHANGED, {"username": username, "actor": actor})
    return {"state": "ready", "username": username}


def set_disabled(username: str, disabled: bool, *, actor: str = "") -> dict[str, Any]:
    """Enable or disable an account. Its sessions die with it."""
    username = auth.normalize_username(username)
    action = "user.disabled" if disabled else "user.enabled"
    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            row = _require_target(cur, username)
            if bool(disabled) == bool(row[2]):
                return {"state": "ready", "username": username, "changed": False}
            _require_not_reserved(username)
            if disabled:
                if actor and actor == username:
                    # This admin's other tab may be mid-demote of the only other
                    # admin. Refusing self-disable makes that sequence impossible
                    # rather than merely unlikely (D-2.3).
                    raise ValueError(_SELF_ACTION)
                _lock_admin_group(cur)
                if _in_group(cur, username, auth.ADMIN_GROUP) and \
                        _enabled_admin_count(cur) <= 1:
                    raise ValueError(f"cannot disable the account: {_LAST_ADMIN}")
            cur.execute(
                "UPDATE users SET disabled = %s WHERE username = %s",
                (bool(disabled), username),
            )
            if disabled:
                # A disabled account must not keep a live session, and must not be
                # able to reauth with a cookie minted before it was stopped.
                cur.execute(
                    "UPDATE users SET token_generation = token_generation + 1"
                    " WHERE username = %s",
                    (username,),
                )
            audit.record(
                actor or audit.ACTOR_CP, username, action,
                {"disabled": bool(disabled)}, cur=cur,
            )
    notify(
        EVENT_DISABLED if disabled else EVENT_ENABLED,
        {"username": username, "actor": actor},
    )
    return {"state": "ready", "username": username, "changed": True}


def set_groups(username: str, groups: list[str], *, actor: str = "") -> dict[str, Any]:
    """Replace a user's group memberships. Open WebUI needs no call.

    Group and role headers are re-synced by Open WebUI on *every* sign-in
    (``auths.py:764``, ``:770``) — that is the shipped mechanism, so a write here
    would be a second source of truth for one fact.
    """
    username = auth.normalize_username(username)
    wanted = sorted({str(g).strip() for g in (groups or []) if str(g).strip()})
    if auth.BASE_GROUP not in wanted:
        # D-3: base membership is a stored row, not an implication, so the write
        # layer keeps it present rather than trusting a UI not to forget it.
        wanted.append(auth.BASE_GROUP)
    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            _require_target(cur, username)
            for group in wanted:
                _require_group_exists(cur, group)
            current = _current_groups(cur, username)
            demoted = auth.ADMIN_GROUP in current and auth.ADMIN_GROUP not in wanted
            if demoted or auth.ADMIN_GROUP in wanted:
                _lock_admin_group(cur)
            for group in wanted:
                cur.execute(
                    """
                    INSERT INTO user_groups (username, name)
                    VALUES (%s, %s) ON CONFLICT (username, name) DO NOTHING
                    """,
                    (username, group),
                )
            for group in set(current) - set(wanted):
                cur.execute(
                    "DELETE FROM user_groups WHERE username = %s AND name = %s",
                    (username, group),
                )
            if demoted and _enabled_admin_count(cur) < 1:
                raise ValueError(f"cannot remove '{auth.ADMIN_GROUP}': {_LAST_ADMIN}")
            audit.record(
                actor or audit.ACTOR_CP, username, "user.groups_changed",
                {"groups": wanted}, cur=cur,
            )
    notify(EVENT_GROUPS_CHANGED, {"username": username, "groups": wanted, "actor": actor})
    modules.request_policy_reconcile()
    return {"state": "ready", "username": username, "groups": wanted}


def rotate_password(
    username: str,
    old_verifier_hex: str,
    new_salt_hex: str,
    new_verifier_hex: str,
    *,
    actor: str = "",
) -> dict[str, Any]:
    """Change your own password, given a validated SRP proof of the old one.

    The caller validates ``M1`` **before** arriving here, so the old password is
    already proven. This then writes the new pair with
    ``WHERE … AND srp_verifier = %s`` so a second tab that rotated first fails
    cleanly instead of clobbering (D-5).

    Rollback is structural, not a compensating action (D-6): nothing is written
    before the old proof validates, the write is one statement in one transaction,
    and there is no delete-then-create step anywhere. Any failure leaves the old
    pair byte-identical, so nobody can be locked out with a null password. Do not
    "improve" this by clearing the verifier first.

    Rotation bumps ``token_generation`` (D-7): every *other* session for the
    account dies on its next request. The gate already turns that into a 302 to
    the sign-in page (``gateway.nix`` ``@auth_unauthorized``), so "change my
    password everywhere, then keep me signed in here" needs no new machinery.
    """
    username = auth.normalize_username(username)
    try:
        salt, verifier = srp.enroll(srp.SERVER_GROUP, new_salt_hex, new_verifier_hex)
    except srp.SrpError as exc:
        raise ValueError(f"The new sign-in data from your browser was not valid: {exc}") from exc

    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            row = _require_target(cur, username)
            if row[2]:
                raise ValueError("account is disabled")
            if str(row[4] or "") != str(old_verifier_hex or ""):
                # The verifier moved under us between the handshake and now —
                # another tab rotated, or an admin reset. Fail without writing.
                raise ValueError("your password changed while you were doing that; try again")
            cur.execute(
                """
                UPDATE users
                   SET srp_salt = %s, srp_verifier = %s,
                       password_reset_required = FALSE,
                       token_generation = token_generation + 1
                 WHERE username = %s AND srp_verifier = %s
                """,
                (salt.hex(), f"{verifier:x}", username, old_verifier_hex),
            )
            if cur.rowcount != 1:
                raise ValueError("your password changed while you were doing that; try again")
            audit.record(
                actor or username, username, "user.password_rotated",
                {"fields": ["srp_verifier"]}, cur=cur,
            )
    notify(EVENT_PASSWORD_ROTATED, {"username": username, "actor": actor or username})
    return {"state": "ready", "username": username}


def admin_reset_password(
    target: str, salt_hex: str, verifier_hex: str, *, actor: str = ""
) -> dict[str, Any]:
    """Set someone else's credential from a verifier their replacement derives from.

    There is no server-side equivalent of "set a password": the server holds
    ``v = g^x mod N`` and cannot derive one from a password it never sees. So the
    admin's browser generates the one-time password, derives its verifier locally,
    and posts only the verifier (D-10). The OTP itself never transits the network
    — that is the whole reason the flow is shaped this way, and the reason there
    must never be a server-side entropy fallback (D-9).

    Two consequences to keep honest, both accepted by review:

    * The admin knows the OTP and can sign in as that user until they rotate.
      Forced rotation bounds the window but does not remove the capability (D-13).
    * ``password_reset_required`` blocks the account from getting a session at all
      until it rotates (D-11), so no half-authenticated state exists for the
      portal, the APIs or Open WebUI to forget about.
    """
    target = auth.normalize_username(target)
    try:
        salt, verifier = srp.enroll(srp.SERVER_GROUP, salt_hex, verifier_hex)
    except srp.SrpError as exc:
        raise ValueError(f"The sign-in data from your browser was not valid: {exc}") from exc
    if not actor:
        raise ValueError("an admin reset has to say who performed it")

    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            row = _require_target(cur, target)
            if row[2]:
                raise ValueError(f"account '{target}' is disabled; enable it first")
            cur.execute(
                """
                UPDATE users
                   SET srp_salt = %s, srp_verifier = %s,
                       password_reset_required = TRUE,
                       token_generation = token_generation + 1
                 WHERE username = %s
                """,
                (salt.hex(), f"{verifier:x}", target),
            )
            audit.record(
                actor, target, "user.password_reset",
                {"rotate_required": True}, cur=cur,
            )
    notify(EVENT_PASSWORD_RESET, {"username": target, "actor": actor})
    return {"state": "ready", "username": target, "rotate_required": True}


def record_login(username: str) -> None:
    """Stamp ``last_login`` on a successful sign-in. One statement, no background job.

    Runs after the session cookie is issued and never propagates a failure: an
    informational column that cannot be written is not a reason to refuse somebody
    their own sign-in.
    """
    username = auth.normalize_username(username)
    try:
        conn = db.cp_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET last_login = NOW() WHERE username = %s", (username,)
                )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — bookkeeping must not fail a sign-in
        print(f"usermgmt last_login {username}: {exc}", flush=True)


def delete(username: str, *, actor: str = "") -> dict[str, Any]:
    """Delete an account and its Open WebUI account (D-16a, D-23).

    Immediate and data-destroying; the portal asks for a typed confirmation, as it
    does for the wipe-everything action.

    **Open WebUI is deleted first**, which is the one place this module does not
    treat the mirror as best-effort. A refusal there must leave the CP row intact:
    a person with conversations still in a module but no portal account is the
    worst of both worlds, and it is exactly the state a later account could
    "adopt" (D-16a's forbidden case). The ``user_groups`` joins follow the row via
    ``ON DELETE CASCADE``.

    The reconcile loop is forbidden from deleting — ``owui_client.user_delete``'s
    own docstring says so. This is its only call site.
    """
    username = auth.normalize_username(username)
    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            row = _require_target(cur, username)
            owui_id = str(row[6] or "")
            _require_not_reserved(username)
            _check_deletable(cur, username, actor)

    # Outside the transaction deliberately: an HTTP call must not hold a database
    # transaction open, and a slow Open WebUI must not become a lock.
    client = _owui_client()
    if client is None:
        raise ValueError(
            "Open WebUI is not reachable, so nothing was deleted. Deleting the"
            " portal account alone would leave their conversations behind under"
            " an account nobody can sign in to."
        )
    refusal = _owui_delete(client, owui_id)
    if refusal:
        raise ValueError(refusal)

    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            # Re-check after the round trip: while Open WebUI was thinking the
            # account could have been disabled or made the last admin.
            _require_target(cur, username)
            _check_deletable(cur, username, actor)
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
            audit.record(
                actor or audit.ACTOR_CP, username, "user.delete",
                {"owui_account_deleted": bool(owui_id)}, cur=cur,
            )
    notify(EVENT_DELETED, {"username": username, "actor": actor})
    modules.request_policy_reconcile()
    return {"state": "ready", "username": username, "owui": "deleted"}


def _check_deletable(cur, username: str, actor: str) -> None:
    """The guards that make an account undeletable (D-2.2, D-2.3, D-22)."""
    if actor and actor == username:
        raise ValueError(_SELF_ACTION)
    if username == first_admin():
        # D-22: identified by row, not by name. Deleting the account that opened
        # the appliance would leave a box nobody can get back into.
        raise ValueError("the first admin account cannot be deleted; disable it instead")
    _lock_admin_group(cur)
    if _in_group(cur, username, auth.ADMIN_GROUP) and _enabled_admin_count(cur) <= 1:
        raise ValueError(f"cannot delete the account: {_LAST_ADMIN}")


def cleanup_owui_accounts(
    identities: list[dict[str, Any]], *, actor: str = ""
) -> list[dict[str, Any]]:
    """Delete Open WebUI accounts whose CP rows are already gone (D-20a).

    Used by the reinstall credential reset, which deletes differently-named former
    admin rows in its own transaction and then owes Open WebUI the same deletion.
    Returns the refusals so the caller can surface them: an account Open WebUI will
    not delete is drift to fix by hand, and it is reported, never dropped. The CP
    rows stay deleted whichever way this goes — a former admin must not be able to
    sign back in.
    """
    problems: list[dict[str, Any]] = []
    if not identities:
        return problems
    client = _owui_client()
    if client is None:
        return [
            {"username": str(item.get("username") or ""),
             "error": "Open WebUI is not running yet"}
            for item in identities
        ]
    for item in identities:
        username = str(item.get("username") or "")
        owui_id = str(item.get("owui_id") or "")
        email = str(item.get("email") or "")
        if not owui_id and email:
            found, error = _owui_find(client, email)
            if error:
                problems.append({"username": username, "error": error})
                continue
            owui_id = str((found or {}).get("id") or "")
        refusal = _owui_delete(client, owui_id)
        if refusal:
            problems.append({"username": username, "error": refusal})
            audit.record(actor or audit.ACTOR_SYSTEM, username,
                         "user.delete_refused", {"error": refusal})
        else:
            audit.record(actor or audit.ACTOR_SYSTEM, username,
                         "user.owui_account_deleted", {})
    return problems


# --------------------------------------------------------------------------
# Group primitives (P4). System groups are immutable apart from membership
# (D-2.1), and authz level derives only from system-group membership (D-1).
# --------------------------------------------------------------------------


def list_groups() -> list[dict[str, Any]]:
    """Groups with their system flag, attributes and member rosters."""
    if not auth.pg_ready():
        return []
    try:
        conn = db.cp_connect()
    except Exception:  # noqa: BLE001 — read boundary
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name, system FROM groups ORDER BY name")
            rows = cur.fetchall()
            cur.execute("SELECT username, name FROM user_groups ORDER BY username, name")
            members: dict[str, list[str]] = {}
            for username, name in cur.fetchall():
                members.setdefault(str(name), []).append(str(username))
            cur.execute("SELECT group_name, key, value FROM group_attributes ORDER BY group_name, key")
            attributes: dict[str, dict[str, Any]] = {}
            for group_name, key, value in cur.fetchall():
                attributes.setdefault(str(group_name), {})[str(key)] = value
    except Exception:  # noqa: BLE001 — read boundary
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return [
        {
            "name": str(name),
            "system": bool(is_system),
            "members": members.get(str(name), []),
            "attributes": attributes.get(str(name), {}),
        }
        for name, is_system in rows
    ]


def create_group(name: str, *, actor: str = "") -> dict[str, Any]:
    """Create a custom group. System groups are never created here (D-2.1)."""
    group = str(name or "").strip().lower()
    if not GROUP_NAME_RE.fullmatch(group):
        raise ValueError(
            "group name must be 2-32 characters: a letter first, then letters,"
            " digits, '_' '.' or '-', and may not end in '.' or '-'"
        )
    if group.endswith((".", "-")):
        raise ValueError("group name may not end in '.' or '-'")
    if group in (auth.ADMIN_GROUP, auth.BASE_GROUP):
        raise ValueError(f"group '{group}' is a system group")
    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            auth.ensure_system_groups(cur)
            cur.execute(
                "INSERT INTO groups (name, system) VALUES (%s, FALSE)"
                " ON CONFLICT (name) DO NOTHING",
                (group,),
            )
            if cur.rowcount != 1:
                raise ValueError(f"group '{group}' already exists")
            audit.record(actor or audit.ACTOR_CP, group, "group.create", {}, cur=cur)
    return {"state": "ready", "group": group}


def delete_group(name: str, *, actor: str = "") -> dict[str, Any]:
    """Delete a custom group, its attributes and its memberships (D-2.1)."""
    group = str(name or "").strip().lower()
    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT system FROM groups WHERE name = %s", (group,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"group '{group}' does not exist")
            if row[0]:
                raise ValueError(f"group '{group}' is a system group and cannot be deleted")
            # No ON DELETE CASCADE on group_attributes: the additive-only
            # migration guard bans the word, so the write layer owns the cleanup
            # the cascade would have done.
            cur.execute("DELETE FROM group_attributes WHERE group_name = %s", (group,))
            cur.execute("DELETE FROM user_groups WHERE name = %s", (group,))
            cur.execute("DELETE FROM groups WHERE name = %s", (group,))
            audit.record(actor or audit.ACTOR_CP, group, "group.delete", {}, cur=cur)
    return {"state": "ready", "group": group}


def add_member(group: str, username: str, *, actor: str = "") -> dict[str, Any]:
    """Add a member. Admin membership is changed on the account, not here.

    Keeping the two apart means one place enforces the last-admin guard instead of
    two that can each forget it.
    """
    return _membership(group, username, True, actor)


def remove_member(group: str, username: str, *, actor: str = "") -> dict[str, Any]:
    return _membership(group, username, False, actor)


def _membership(group: str, username: str, add: bool, actor: str) -> dict[str, Any]:
    group = str(group or "").strip().lower()
    username = auth.normalize_username(username)
    if group == auth.ADMIN_GROUP:
        raise ValueError(
            "admin membership is changed on the account, not through the group"
        )
    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            _require_target(cur, username)
            _require_group_exists(cur, group)
            if add:
                cur.execute(
                    """
                    INSERT INTO user_groups (username, name)
                    VALUES (%s, %s) ON CONFLICT (username, name) DO NOTHING
                    """,
                    (username, group),
                )
            else:
                cur.execute(
                    "DELETE FROM user_groups WHERE username = %s AND name = %s",
                    (username, group),
                )
            audit.record(
                actor or audit.ACTOR_CP, username,
                "group.member_add" if add else "group.member_remove",
                {"group": group}, cur=cur,
            )
    notify(EVENT_GROUPS_CHANGED, {"username": username, "group": group, "actor": actor})
    return {"state": "ready", "group": group, "username": username}


# --------------------------------------------------------------------------
# group_attributes (D-30). Key types are declared in code, never inferred.
# --------------------------------------------------------------------------


def _validate_bool(value: Any) -> Optional[str]:
    if not isinstance(value, bool):
        return "attribute must be a JSON boolean"
    return None


# Known attribute keys → validator. An unknown key is a rejected write, not a
# silently-typed blob: JSONB will store anything, which is exactly why the
# *meaning* of a key has to live somewhere other than the row (D-3). Growing the
# set of value types is a new entry here, not a new schema version (D-30).
ATTRIBUTE_KEYS: dict[str, Callable[[Any], Optional[str]]] = {
    "content_filter": _validate_bool,
}


def set_group_attribute(
    group: str, key: str, value: Any, *, actor: str = ""
) -> dict[str, Any]:
    """Set one group attribute. Unknown keys and wrong types are refused (D-3)."""
    group = str(group or "").strip().lower()
    key = str(key or "").strip().lower()
    validator = ATTRIBUTE_KEYS.get(key)
    if validator is None:
        raise ValueError(
            f"attribute '{key}' is not known; the set is {sorted(ATTRIBUTE_KEYS)}"
        )
    problem = validator(value)
    if problem:
        raise ValueError(problem)
    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            _require_group_exists(cur, group)
            cur.execute(
                """
                INSERT INTO group_attributes (group_name, key, value)
                VALUES (%s, %s, %s::jsonb)
                ON CONFLICT (group_name, key) DO UPDATE SET value = EXCLUDED.value
                """,
                (group, key, json.dumps(value)),
            )
            audit.record(
                actor or audit.ACTOR_CP, group, "group.attribute_set",
                {"key": key}, cur=cur,
            )
    return {"state": "ready", "group": group, "key": key}


def delete_group_attribute(group: str, key: str, *, actor: str = "") -> dict[str, Any]:
    group = str(group or "").strip().lower()
    key = str(key or "").strip().lower()
    with db.cp_transaction() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM group_attributes WHERE group_name = %s AND key = %s",
                (group, key),
            )
            audit.record(
                actor or audit.ACTOR_CP, group, "group.attribute_remove",
                {"key": key}, cur=cur,
            )
    return {"state": "ready", "group": group, "key": key}
