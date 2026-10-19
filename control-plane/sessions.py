"""Server-side session state in Valkey (M6.6 D2).

The signed cookie proves *who* you are; this module decides whether that identity
is still **live**. Splitting it this way buys the two things a stateless cookie
cannot do: an idle timeout that slides while a tab is in use, and revocation that
takes effect on the next request instead of whenever the cookie ages out.

Two properties are load-bearing:

* **Fail closed.** Every read/write raises ``SessionStoreUnavailable`` when
  Valkey cannot be reached. The gate answers 503 rather than serving an
  authenticated surface it was unable to check. (Logout is the one deliberate
  exception — see ``drop_quietly``.)
* **Keys die with the boot.** The key carries the boot id, so an appliance reboot
  signs everyone out even though ``valkey-server --save 60 1`` restores its
  snapshot. That is an accepted tradeoff, not a goal; the alternatives are
  recorded in ``proposals/M6/SESSION-AND-ERROR-CONTRACT-M66.md`` (D2).

No Valkey in unit tests: the client is imported lazily and mocked, so ``make
test`` needs no Valkey, no Docker, no Postgres (AGENTS.md).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

# Sliding window: each authenticated request re-extends the idle TTL, but never
# past the absolute ceiling measured from issuance. A session therefore survives
# an afternoon of reading but cannot live forever just because the tab stayed open.
SESSION_INACTIVITY_SECONDS = 3600  # 1h idle
SESSION_ABSOLUTE_SECONDS = 36000  # 10h from login, however active

# Socket timeouts stay short: this runs inside nginx `auth_request`, on the path
# of every gated request. A stalled Valkey must surface as a fast 503, not as a
# browser spinner.
SESSION_KEY_PREFIX = "lemonseed:sess"
SOCKET_TIMEOUT_SECONDS = 1.0
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
UNKNOWN_BOOT_ID = "unknown-boot"


class SessionStoreUnavailable(RuntimeError):
    """Valkey could not be reached; the caller must fail closed."""


_client: Any = None
_client_lock = threading.Lock()
_boot_id: Optional[str] = None


def remaining_seconds(now: float, issued_at: float) -> int:
    """Headroom under the absolute ceiling, floored at 0."""
    return max(0, int(issued_at + SESSION_ABSOLUTE_SECONDS - now))


def sliding_ttl(now: float, issued_at: float) -> int:
    """TTL for one touch: the idle allowance, clipped by the absolute ceiling.

    ``0`` means the ceiling has passed and the session is dead.
    """
    return min(SESSION_INACTIVITY_SECONDS, remaining_seconds(now, issued_at))


def _boot_namespace() -> str:
    """Boot id, cached. Unreadable → a fixed bucket (still fail-closed)."""
    global _boot_id
    if _boot_id is None:
        try:
            _boot_id = BOOT_ID_PATH.read_text(encoding="utf-8").strip() or UNKNOWN_BOOT_ID
        except OSError:
            _boot_id = UNKNOWN_BOOT_ID
    return _boot_id


def _get_client() -> Any:
    global _client
    with _client_lock:
        if _client is not None:
            return _client
        # Lazy import keeps the redis dependency off the unit-test path; the
        # appliance gets it from nixos/modules/control-plane.nix.
        import redis  # noqa: PLC0415

        _client = redis.Redis(
            host=os.environ.get("LEMONSEED_VALKEY_HOST", "127.0.0.1"),
            port=int(os.environ.get("LEMONSEED_VALKEY_PORT", "6379")),
            db=0,
            decode_responses=True,
            socket_timeout=SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        )
        return _client


def _forget_client() -> None:
    """Drop the cached client so the next call reconnects."""
    global _client
    with _client_lock:
        _client = None


def client() -> Any:
    """The shared Valkey client, for callers that need keys of their own.

    Today that is only the login rate limiter in ``auth.py``. Note the different
    contract: session reads here raise ``SessionStoreUnavailable`` because the
    gate must fail closed, whereas a *policy* counter that cannot be reached
    should not lock a whole household out of an appliance. Callers own that
    decision; this function just hands out the connection and lets lazy import
    keep ``redis`` off the unit-test path.
    """
    return _get_client()


def forget_client() -> None:
    """Public mirror of :func:`_forget_client` for best-effort callers."""
    _forget_client()


def key(sid: str) -> str:
    return f"{SESSION_KEY_PREFIX}:{_boot_namespace()}:{sid}"


def create(sid: str, record: dict[str, Any], now: Optional[float] = None) -> bool:
    """Store a fresh session. False when its ceiling is already behind us."""
    moment = time.time() if now is None else now
    ttl = sliding_ttl(moment, float(record.get("iat", moment)))
    if ttl <= 0:
        return False
    payload = json.dumps(record, separators=(",", ":"))
    try:
        _get_client().set(key(sid), payload, ex=ttl)
    except Exception as exc:  # noqa: BLE001 — third-party client boundary
        _forget_client()
        raise SessionStoreUnavailable(str(exc)) from exc
    return True


def read(sid: str) -> Optional[dict[str, Any]]:
    """The live record for ``sid``, or None if there is none."""
    try:
        raw = _get_client().get(key(sid))
    except Exception as exc:  # noqa: BLE001 — third-party client boundary
        _forget_client()
        raise SessionStoreUnavailable(str(exc)) from exc
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        # Unparseable is not "unreachable": treat it as no session.
        return None
    return record if isinstance(record, dict) else None


def refresh(sid: str, issued_at: float, now: Optional[float] = None) -> bool:
    """Slide the idle window. False means the absolute ceiling passed."""
    moment = time.time() if now is None else now
    ttl = sliding_ttl(moment, issued_at)
    if ttl <= 0:
        drop_quietly(sid)
        return False
    try:
        _get_client().expire(key(sid), ttl)
    except Exception as exc:  # noqa: BLE001 — third-party client boundary
        _forget_client()
        raise SessionStoreUnavailable(str(exc)) from exc
    return True


def drop(sid: str) -> bool:
    """Delete a session, raising ``SessionStoreUnavailable`` on connection loss."""
    try:
        return bool(_get_client().delete(key(sid)))
    except Exception as exc:  # noqa: BLE001 — third-party client boundary
        _forget_client()
        raise SessionStoreUnavailable(str(exc)) from exc


def drop_quietly(sid: str) -> bool:
    """Best-effort delete for logout and expiry cleanup. Never raises.

    Losing the key is not a security regression here: the caller's cookie is
    cleared regardless, and an orphaned key expires on its own within the idle
    window. Failing a logout because the data plane is unhappy would be worse.
    """
    try:
        return bool(_get_client().delete(key(sid)))
    except Exception:  # noqa: BLE001 — best effort by design
        _forget_client()
        return False


def ping() -> bool:
    """Whether the store is reachable. For status reporting only."""
    try:
        return bool(_get_client().ping())
    except Exception:  # noqa: BLE001 — reporting must not fail the caller
        _forget_client()
        return False


_STATUS_CACHE_SEC = 10.0
_status: Optional[tuple[str, float]] = None


def store_status(cache_sec: float = _STATUS_CACHE_SEC) -> str:
    """``"ok"`` / ``"unavailable"`` for /api/status, cached briefly.

    A dead Valkey makes ``ping()`` block for the full socket timeout, and the
    shell polls /api/status every few seconds; caching the answer keeps a broken
    data plane from adding a second of latency to every poll. Staleness is
    bounded by the TTL and the consequence is a status dot, not a decision —
    the gate still fails closed on every request.
    """
    global _status
    now = time.monotonic()
    if _status is not None and now - _status[1] < cache_sec:
        return _status[0]
    value = "ok" if ping() else "unavailable"
    _status = (value, now)
    return value


def reset_status_cache() -> None:
    """Drop the cached store status (tests, or after a known data-plane change)."""
    global _status
    _status = None
