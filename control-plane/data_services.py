"""Health and lifecycle for shared PostgreSQL and Valkey (M6.0a).

CP is the control plane: the supervisor below continuously reconciles intent
(containers running + ports serving) with reality, the same way modules.py
owns Open WebUI. The systemd oneshot units (data-services.nix) are a best-
effort boot accelerator only — if their pull/run fails (e.g. DNS not ready
when network-online.target is reached), the supervisor heals it on a later
tick instead of the data plane staying dead for the boot.
"""

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
_provision_worker: Optional[threading.Thread] = None

# Intent: the exact docker run args the data plane wants. Must match
# nixos/modules/data-services.nix so containers created here are compatible
# with the boot-time start scripts (and vice versa).
DATA_ROOT = Path("/var/lib/lemonseed/data")
POSTGRES_ENV = DATA_ROOT / "secrets" / "postgres.env"
LEMON_NETWORK = "lemon"
POSTGRES_IMAGE = "pgvector/pgvector:pg16"
VALKEY_IMAGE = "valkey/valkey:8-alpine"
POSTGRES_CONTAINER = "lemonseed-postgres"
VALKEY_CONTAINER = "lemonseed-valkey"

# One shared registry-reachability gate for all data-plane pulls, using the
# same cached DNS + TCP 443 probe pattern as modules.py. Probes the image's
# own registry prefix when scoped (e.g. ghcr.io), else Docker Hub's registry.
_registry_host = POSTGRES_IMAGE.split("/")[0] if "/" in POSTGRES_IMAGE else "registry-1.docker.io"
_PULL_FAIL_COOLDOWN_SEC = 60
_net_check_at = 0.0
_net_ok = False


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def valkey_ready() -> bool:
    return _port_open("127.0.0.1", 6379)


def postgres_snapshot() -> dict[str, Any]:
    snap = dict(_containers.get(POSTGRES_CONTAINER) or {})
    ready = pg_isready()
    snap["state"] = "ready" if ready else (snap.get("state") or "starting")
    snap["host"] = "127.0.0.1:5432"
    snap["container"] = POSTGRES_CONTAINER
    return snap


def valkey_snapshot() -> dict[str, Any]:
    snap = dict(_containers.get(VALKEY_CONTAINER) or {})
    ready = valkey_ready()
    snap["state"] = "ready" if ready else (snap.get("state") or "starting")
    snap["host"] = "127.0.0.1:6379"
    snap["container"] = VALKEY_CONTAINER
    return snap


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
    """Keep trying DB provisioning until ready — never give up on a boot.

    Postgres can come up late (cold pull, TCG, registry hiccup), so this is an
    indefinite throttled loop rather than a deadline. Cheap once ready: it
    returns on the first tick after provisioning succeeds.
    """
    global _provision_worker
    if _provision_worker is not None and _provision_worker.is_alive():
        return

    def _run() -> None:
        while True:
            try:
                if ensure_provisioned().get("state") == "ready":
                    return
            except Exception as exc:  # noqa: BLE001 — never kill the worker
                print(f"data provision: {exc}", flush=True)
            time.sleep(3)

    _provision_worker = threading.Thread(
        target=_run, name="lemonseed-db-provision", daemon=True
    )
    _provision_worker.start()


# ---------------------------------------------------------------------------
# Container supervision (CP-owned, like modules.py for OWUI)
# ---------------------------------------------------------------------------

SUPERVISE_INTERVAL_SEC = 10
# A container that is running but whose port stays closed this long is
# considered wedged (crash-loop, half-boot) and gets recreated.
HEALTH_GRACE_SEC = 90

_sup_worker: Optional[threading.Thread] = None
_containers: dict[str, dict[str, Any]] = {
    POSTGRES_CONTAINER: {"state": "starting"},
    VALKEY_CONTAINER: {"state": "starting"},
}


def _docker(args: list[str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(
            args=["docker", *args],
            returncode=127,
            stdout="",
            stderr=str(exc),
        )


def _container_running(name: str) -> bool:
    result = _docker(["inspect", "-f", "{{.State.Running}}", name], timeout=15)
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _container_exists(name: str) -> bool:
    result = _docker(["inspect", "-f", "{{.Name}}", name], timeout=15)
    return result.returncode == 0


def _image_present(image: str) -> bool:
    return _docker(["image", "inspect", image], timeout=30).returncode == 0


def _network_reachable() -> bool:
    """Best-effort registry check (DNS + TCP 443), cached like modules.py.

    True stays cached until a re-check fails; False is retried after the
    cooldown so the box self-heals when connectivity returns.
    """
    global _net_check_at, _net_ok
    now = time.monotonic()
    if _net_ok and now - _net_check_at < _PULL_FAIL_COOLDOWN_SEC:
        return True
    if not _net_ok and now - _net_check_at < _PULL_FAIL_COOLDOWN_SEC:
        return False
    try:
        host = socket.gethostbyname(_registry_host)
        with socket.create_connection((host, 443), timeout=5):
            pass
        _net_ok = True
    except (OSError, socket.gaierror):
        _net_ok = False
    _net_check_at = now
    return _net_ok


def _pull_image(image: str, label: str) -> tuple[bool, str]:
    if _image_present(image):
        return True, "present"
    if not _network_reachable():
        probe = _docker(["pull", image], timeout=30)
        if probe.returncode != 0:
            detail = (probe.stderr or probe.stdout or "").strip().splitlines()
            print(
                f"data pull: {label} registry unreachable; docker probe: "
                f"{(detail[-1] if detail else 'no output')[:300]}",
                flush=True,
            )
        return (
            False,
            "Cannot reach the container registry — check this appliance's internet connection. "
            "Retrying automatically.",
        )
    result = _docker(["pull", image], timeout=600)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "docker pull failed").strip()
    return True, "pulled"


def _ensure_lemon_network() -> bool:
    if _docker(["network", "inspect", LEMON_NETWORK], timeout=30).returncode == 0:
        return True
    created = _docker(
        ["network", "create", "--driver", "bridge", LEMON_NETWORK], timeout=30
    )
    return created.returncode == 0


def _postgres_run_args() -> list[str]:
    args = [
        "run",
        "-d",
        "--name",
        POSTGRES_CONTAINER,
        "--restart",
        "unless-stopped",
        "--network",
        LEMON_NETWORK,
        "-p",
        "127.0.0.1:5432:5432",
        "-v",
        f"{DATA_ROOT}/postgres:/var/lib/postgresql/data",
    ]
    # Env file is written by lemonseed-data-init before the containers start.
    if POSTGRES_ENV.is_file():
        args += ["--env-file", str(POSTGRES_ENV)]
    args.append(POSTGRES_IMAGE)
    return args


def _valkey_run_args() -> list[str]:
    return [
        "run",
        "-d",
        "--name",
        VALKEY_CONTAINER,
        "--restart",
        "unless-stopped",
        "--network",
        LEMON_NETWORK,
        "-p",
        "127.0.0.1:6379:6379",
        "-v",
        f"{DATA_ROOT}/valkey:/data",
        VALKEY_IMAGE,
        "valkey-server",
        "--save",
        "60",
        "1",
        "--loglevel",
        "warning",
    ]


def _start_container(name: str, image: str, label: str, run_args: list[str]) -> None:
    state = _containers[name]
    # Reuse a stopped container exactly as the boot start scripts do.
    if _container_exists(name):
        if _docker(["start", name], timeout=120).returncode == 0:
            state["state"] = "starting"
            state["since"] = time.monotonic()
            state.pop("error", None)
            return
        _docker(["rm", "-f", name], timeout=60)
    if not _ensure_lemon_network():
        state["state"] = "error"
        state["error"] = "Docker network 'lemon' unavailable"
        return
    ok, detail = _pull_image(image, label)
    if not ok:
        state["state"] = "waiting"
        state["error"] = detail
        return
    result = _docker(run_args, timeout=180)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "docker run failed").strip()
        if "already in use" in err.lower():
            _docker(["rm", "-f", name], timeout=60)
            result = _docker(run_args, timeout=180)
            if result.returncode != 0:
                err = (result.stderr or result.stdout or err).strip()
        state["state"] = "error"
        state["error"] = err[:500]
        return
    state["state"] = "starting"
    state["since"] = time.monotonic()
    state.pop("error", None)
    print(f"data supervise: {label} container started", flush=True)


def _supervise_one(name: str, image: str, label: str, port: int, run_args: list[str]) -> None:
    state = _containers[name]
    running = _container_running(name)
    if running and _port_open("127.0.0.1", port, timeout=2.0):
        state["state"] = "ready"
        state.pop("error", None)
        return
    if running:
        # Tolerate the first minutes of a cold start (Postgres init, TCG).
        since = float(state.get("since") or 0.0)
        if time.monotonic() - since < HEALTH_GRACE_SEC:
            state["state"] = "starting"
            return
        print(f"data supervise: {label} not serving after grace; recreating", flush=True)
        _docker(["rm", "-f", name], timeout=60)
    if not _container_exists(name):
        _start_container(name, image, label, run_args)
    else:
        # Stopped or exited container: start it again.
        if _docker(["start", name], timeout=120).returncode != 0:
            _docker(["rm", "-f", name], timeout=60)
            _start_container(name, image, label, run_args)


def supervise_data_once() -> None:
    """One reconciliation tick: keep postgres + valkey running and serving."""
    _supervise_one(
        POSTGRES_CONTAINER, POSTGRES_IMAGE, "postgres", 5432, _postgres_run_args()
    )
    _supervise_one(
        VALKEY_CONTAINER, VALKEY_IMAGE, "valkey", 6379, _valkey_run_args()
    )


def ensure_data_worker() -> None:
    """Long-lived supervisor: heal the data plane after boot and after crashes."""
    global _sup_worker
    if _sup_worker is not None and _sup_worker.is_alive():
        return

    def _run() -> None:
        while True:
            try:
                supervise_data_once()
            except Exception as exc:  # noqa: BLE001 — never kill the supervisor
                print(f"data supervise: {exc}", flush=True)
            time.sleep(SUPERVISE_INTERVAL_SEC)

    _sup_worker = threading.Thread(target=_run, name="lemonseed-data", daemon=True)
    _sup_worker.start()


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
