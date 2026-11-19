"""Deep live probes + a shared failure-counting tracker (M9 P4).

P4's contract: no status field may claim healthy while the service is not
serving, and a wedged daemon gets restarted by its supervisor. The pieces:

* ``probe_*()`` functions — one deep check each, returning ``(ok, detail)``.
  Deep means "the service answered at its own protocol", never TCP alone:
  Postgres answers pg_isready + SELECT 1, Valkey answers PING, HTTP services
  answer their documented health route. Callers must not fall back to a bare
  TCP check — a listening socket says nothing about the process behind it.

* ``Tracker`` — per-service failure accounting shared by every supervisor.
  One success recovers; ``fail_limit`` consecutive failures flips unhealthy.
  States: "starting" (never succeeded yet), "ready", "unhealthy". Snapshots
  carry the last error and its ISO timestamp so a status panel can say *why*.

Probes are deliberately cheap (<2 s) — they run on the supervisor tick.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_FAIL_LIMIT = 4          # consecutive failing ticks before unhealthy
DEFAULT_TIMEOUT_SEC = 2.0


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- probes ------------------------------------------------------------------


def probe_tcp(host: str, port: int, timeout: float = DEFAULT_TIMEOUT_SEC) -> tuple[bool, str]:
    """Bare socket check. Only for callers that own the semantics (port-open
    gating in the run path); never as a health fallback."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, ""
    except OSError as exc:
        return False, f"tcp {host}:{port}: {exc.__class__.__name__}"


def probe_http(
    url: str,
    *,
    key: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """GET a health route; 2xx is alive. A 503 body matters (LiteLLM's
    readiness returns one when its DB is unreachable), so read and keep it."""
    req = urllib.request.Request(url)
    if key:
        req.headers["Authorization"] = f"Bearer {key}"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = (response.read() or b"")[:200]
            return True, body.decode("utf-8", "replace").strip()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (exc.read() or b"")[:200].decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 — the body is best-effort context
            pass
        return False, f"http {exc.code} from {url}" + (f": {detail}" if detail else "")
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return False, f"http {url}: {reason}"


def probe_postgres(
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> tuple[bool, str]:
    """pg_isready plus a real SELECT 1 on the app connection. The first catches
    the server wedged before accepting sessions; the second catches a server
    that accepts sessions but cannot answer its own protocol (auth, lockup)."""
    from db import pg_isready, _connect  # lazy: db.py owns env/credential handling

    if not pg_isready():
        return False, "pg_isready: no response"
    try:
        # SELECT 1 must authenticate the same way every other consumer does —
        # a passwordless connect here fails against TCP auth (fe_sendauth) and
        # the supervisor would then recreate a perfectly healthy server.
        conn = _connect("postgres")
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — any failure is a failed probe
        return False, f"SELECT 1: {str(exc)[:200]}"
    return True, ""


def probe_valkey(
    host: str = "127.0.0.1",
    port: int = 6379,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> tuple[bool, str]:
    """A RESP PING over the wire (no client dependency). Catches a server that
    accepts TCP but cannot serve — LOADING, blocked persistence, OOM state."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(b"PING\r\n")
            data = sock.recv(64)
        if data.startswith(b"+PONG"):
            return True, ""
        return False, f"valkey PING: unexpected reply {data[:32]!r}"
    except OSError as exc:
        return False, f"valkey PING: {exc.__class__.__name__}"


def probe_docker_health(
    container: str,
    timeout: float = DEFAULT_TIMEOUT_SEC,
) -> Optional[bool]:
    """The container image's own HEALTHCHECK verdict, or None when the image
    does not define one. Used only where an image publishes a high-quality
    check (Open WebUI's /health); never as a guess."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Health.Status}}", container],
            capture_output=True, text=True, timeout=timeout * 3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    status = (result.stdout or "").strip()
    if result.returncode != 0 or not status or status == "<no value>":
        return None
    return status.lower() == "healthy"


def probe_lemond(timeout: float = DEFAULT_TIMEOUT_SEC) -> tuple[bool, str]:
    """lemond's deep check: GET /v1/health (key-gated; the CP already holds
    LEMONADE_API_KEY). Healthy means {status: ok} — the server answered its
    own protocol with every loaded backend accounted for."""
    from hardware import lemonade_api_key  # lazy: env-file handling lives there

    key = lemonade_api_key()
    if not key:
        return False, "no LEMONADE_API_KEY configured"
    url = "http://127.0.0.1:13305/v1/health"
    ok, detail = probe_http(url, key=key, timeout=timeout)
    if not ok:
        return False, detail
    try:
        payload = json.loads(detail or "{}")
    except ValueError:
        payload = {}
    if isinstance(payload, dict) and payload.get("status") == "ok":
        return True, ""
    return False, f"/v1/health body: {detail[:120]}"


# --- tracker -----------------------------------------------------------------


class Tracker:
    """Failure accounting for one service across supervisor ticks.

    One success is enough to recover (a healthy answer ends an incident);
    ``fail_limit`` consecutive failures flips the state to unhealthy. The
    snapshot never fabricates a healthy verdict — it reports what the last
    probe saw, or "starting" if nothing has succeeded yet.
    """

    def __init__(self, *, fail_limit: int = DEFAULT_FAIL_LIMIT) -> None:
        self._fail_limit = max(1, fail_limit)
        self._state = "starting"
        self._fails = 0
        self._last_error: Optional[str] = None
        self._checked_at: Optional[str] = None

    def record(self, ok: bool, detail: str = "") -> None:
        self._checked_at = _iso_now()
        if ok:
            self._fails = 0
            self._state = "ready"
            self._last_error = None
            return
        self._fails += 1
        if detail:
            self._last_error = str(detail)[:500]
        if self._state != "unhealthy":
            # A failure ends the ready streak immediately. Keeping "ready"
            # here hid the first failures behind snapshot()'s masking (a
            # wedged service reported itself healthy with a stale error for
            # fail_limit ticks) — "starting" is honest: not confirmed well.
            self._state = (
                "unhealthy" if self._fails >= self._fail_limit else "starting"
            )

    def note_unhealthy(self, detail: str) -> None:
        """Forced verdict for a service the supervisor knows is down (a
        container that exited), without spending ticks on probes."""
        self._checked_at = _iso_now()
        self._state = "unhealthy"
        if detail:
            self._last_error = str(detail)[:500]

    def mark_starting(self) -> None:
        """A start attempt resets the accounting to an honest 'starting' —
        the old unhealthy verdict described a process that no longer exists.
        The last error stays visible for one incident's context."""
        self._state = "starting"
        self._fails = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "last_error": self._last_error,
            "checked_at": self._checked_at,
            "failed_ticks": self._fails if self._state != "ready" else 0,
        }
