"""Health and lifecycle for shared PostgreSQL and Valkey (M6.0a)."""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from db import pg_isready, provision_data_plane

_provision_lock = threading.Lock()
_provision_state: Optional[dict[str, Any]] = None


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def valkey_ready() -> bool:
    return _port_open("127.0.0.1", 6379)


def postgres_snapshot() -> dict[str, Any]:
    ready = pg_isready()
    return {
        "state": "ready" if ready else "starting",
        "host": "127.0.0.1:5432",
        "container": "lemonseed-postgres",
    }


def valkey_snapshot() -> dict[str, Any]:
    ready = valkey_ready()
    return {
        "state": "ready" if ready else "starting",
        "host": "127.0.0.1:6379",
        "container": "lemonseed-valkey",
    }


def provisioning_snapshot() -> dict[str, Any]:
    global _provision_state
    if _provision_state is not None:
        return dict(_provision_state)
    return {"state": "idle"}


def _sync_json_state_to_db() -> None:
    """Mirror durable JSON state into cp_meta when Postgres is ready."""
    from db import cp_set_meta

    setup_path = Path("/etc/appliance-setup.json")
    if setup_path.is_file():
        try:
            cp_set_meta("setup", json.loads(setup_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    chat_path = Path("/var/lib/lemonseed/chat-ready.json")
    if chat_path.is_file():
        try:
            cp_set_meta("chat_ready", json.loads(chat_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass


def ensure_provisioned() -> dict[str, Any]:
    """Run DB provisioning once Postgres is up; non-blocking for HTTP."""
    global _provision_state
    if not _provision_lock.acquire(blocking=False):
        return provisioning_snapshot()

    try:
        if _provision_state and _provision_state.get("state") == "ready":
            return dict(_provision_state)
        # Retry after a previous error once Postgres is up.
        if not pg_isready():
            _provision_state = {"state": "waiting", "reason": "PostgreSQL not ready"}
            return dict(_provision_state)
        _provision_state = provision_data_plane()
        if _provision_state.get("state") == "ready":
            _sync_json_state_to_db()
        return dict(_provision_state)
    finally:
        _provision_lock.release()


def ensure_provision_worker() -> None:
    def _run() -> None:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            result = ensure_provisioned()
            if result.get("state") == "ready":
                return
            time.sleep(3)
        ensure_provisioned()

    thread = threading.Thread(target=_run, name="lemonseed-db-provision", daemon=True)
    thread.start()


def data_snapshot() -> dict[str, Any]:
    return {
        "postgres": postgres_snapshot(),
        "valkey": valkey_snapshot(),
        "provisioning": provisioning_snapshot(),
    }


def systemd_unit_active(unit: str) -> str:
    try:
        out = subprocess.check_output(
            ["systemctl", "is-active", unit],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).strip()
        return out
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
