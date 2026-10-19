"""Append-only audit trail for admin-class actions (M7 P3).

USERS-AND-GROUPS.md §11 decided the store: PostgreSQL is the record of truth
because the consumer is a query ("who did what to this account, when", joinable
against accounts), not a log tail. journald would have given us free retention
but no joins, no per-account filter, and a read path that needs root on NixOS.

One journal line accompanies every row. That copy is not the record of truth —
it is the ~2 lines that buy an independent trail surviving a wiped database,
readable from a root shell with no control plane running.

Retention is application SQL on the existing slow policy tick (§D-36), not a
migration: ``test_only_additive_migration_sql`` bans ``DELETE`` from *migration*
statements, and a runtime sweep is not migration DDL.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

import db
from owui_policy import mask_detail

AUDIT_RETENTION_DAYS = 365

# ``actor`` values that are not a session. ``system`` is an unauthenticated but
# privileged path (the reinstall reset marker); ``cp`` is work the control plane
# started itself (a reconcile-created OWUI account).
ACTOR_SYSTEM = "system"
ACTOR_CP = "cp"

_INSERT = (
    "INSERT INTO audit_log (actor, target, action, detail)"
    " VALUES (%s, %s, %s, %s::jsonb)"
)

_SELECT_RECENT = (
    "SELECT id, at, actor, target, action, detail FROM audit_log"
    " WHERE (%s = '' OR target = %s)"
    " ORDER BY id DESC LIMIT %s"
)


def record(
    actor: str,
    target: str,
    action: str,
    detail: Optional[dict[str, Any]] = None,
    cur: Any = None,
) -> bool:
    """Write one audit event; ``True`` when the row landed.

    ``detail`` carries **names of things changed, never values**, and is masked
    through the same policy as the config drift report (D-37) — an audit log
    that faithfully records the API key someone pasted into a field is worse
    than no audit log. Do not put a password, verifier, salt, OTP, token or key
    in it, even masked: their presence in a request is not a fact worth keeping.

    Pass ``cur`` to write inside the caller's transaction, so an operation and
    its audit row commit together or not at all. Without it, opens its own
    connection.

    Never raises. An audit write that fails must not undo the change the
    operator made; it logs loudly instead, because an audit trail with a hole
    in it is a truth problem, not a cosmetic one.
    """
    actor = str(actor or "").strip() or ACTOR_SYSTEM
    target = str(target or "")
    action = str(action or "")
    payload: Optional[str] = None
    if detail:
        try:
            payload = json.dumps(mask_detail(detail), default=str, sort_keys=True)
        except Exception as exc:  # noqa: BLE001 — logging boundary, by design
            # A detail that cannot be encoded (a cycle, or a type whose __repr__
            # raises) must not cost the operator the whole event.
            payload = json.dumps({"unloggable_detail": str(exc)})
    print(f"lemonseed-audit actor={actor} target={target or '-'} action={action}"
          f" detail={payload or '{}'}", flush=True)
    try:
        if cur is not None:
            cur.execute(_INSERT, (actor, target, action, payload))
        else:
            conn = db.cp_connect()
            try:
                with conn.cursor() as own:
                    own.execute(_INSERT, (actor, target, action, payload))
            finally:
                conn.close()
    except Exception as exc:  # noqa: BLE001 — logging boundary, by design
        print(f"lemonseed-audit COULD NOT RECORD actor={actor} target={target or '-'}"
              f" action={action}: {exc}", flush=True)
        return False
    return True


def recent(limit: int = 200, target: str = "") -> list[dict[str, Any]]:
    """Newest-first audit rows, optionally for one ``target``. ``[]`` if unavailable.

    Empty list means "no audit trail readable this run", which the portal shows
    as such rather than as "nothing happened" — the two read the same on screen
    and only one of them is a lie the operator can act on.
    """
    try:
        conn = db.cp_connect()
    except Exception:  # noqa: BLE001 — read boundary
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(_SELECT_RECENT, (target, target, int(limit)))
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001 — read boundary
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    events: list[dict[str, Any]] = []
    for row_id, at, actor, row_target, action, detail in rows:
        events.append(
            {
                "id": int(row_id),
                "at": at.isoformat() if at else "",
                "actor": str(actor),
                "target": str(row_target or ""),
                "action": str(action),
                "detail": detail if isinstance(detail, dict) else {},
            }
        )
    return events


def sweep() -> int:
    """Delete audit rows older than ``AUDIT_RETENTION_DAYS``; return how many.

    Bounds growth without a second daemon. ``make_interval`` rather than
    interpolating the number into the SQL so the statement stays a constant.
    """
    if not db.postgres_admin_dsn() or not db.pg_isready():
        return 0
    try:
        conn = db.cp_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM audit_log WHERE at < NOW() - make_interval(days => %s)",
                    (AUDIT_RETENTION_DAYS,),
                )
                return int(cur.rowcount or 0)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — background sweep boundary
        print(f"lemonseed-audit sweep failed: {exc}", flush=True)
        return 0


# The audit table lives in Postgres, so retention belongs to the data-plane
# supervisor, which ticks whether or not any module is installed. The interval
# is long because the DELETE is a full-table scan and nothing user-facing waits
# on it.
SWEEP_INTERVAL_SEC = 6 * 3600.0

# Negative start so the first tick after Postgres accepts connections sweeps.
_sweep_at = -SWEEP_INTERVAL_SEC


def maybe_sweep() -> int:
    """Retention sweep at most once per ``SWEEP_INTERVAL_SEC``.

    An unreachable database does *not* consume the slot: a box that boots before
    Postgres is ready would otherwise wait another six hours for a sweep that
    was never attempted.
    """
    global _sweep_at
    now = time.monotonic()
    if now - _sweep_at < SWEEP_INTERVAL_SEC:
        return 0
    if not db.postgres_admin_dsn() or not db.pg_isready():
        return 0
    _sweep_at = now
    deleted = sweep()
    if deleted:
        print(
            f"lemonseed-audit: swept {deleted} rows past {AUDIT_RETENTION_DAYS}d retention",
            flush=True,
        )
    return deleted
