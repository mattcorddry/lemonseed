"""Open WebUI Docker module lifecycle (M6.1): pull, run, health, blue/green."""

from __future__ import annotations

import os
import secrets
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.request import urlopen

from db import SECRETS_MODULES, _module_credentials, _write_module_secret
from hardware import lemonade_api_key

OWUI_IMAGE = os.environ.get(
    "LEMONSEED_OWUI_IMAGE",
    "ghcr.io/open-webui/open-webui:v0.6.32",
)
MODULE_ID = "open-webui"
UPSTREAM_FILE = Path("/run/lemonseed/owui-upstream.conf")
SLOT_PORTS = (18080, 18081)
CONTAINER_PREFIX = "lemonseed-owui"
LEMON_NETWORK = "lemon"
POSTGRES_SERVICE = "lemonseed-postgres"
# lemond listens on 127.0.0.1 only. Containers reach it via nginx on the host
# gateway (host.docker.internal → lemon bridge IP :80), not :13305 directly.
LEMONADE_BASE = "http://host.docker.internal/_lemonseed/lemond/v1"
# OWUI is a root SPA — iframe must load path /, not /chat/.
OWUI_EMBED_PATH = "/?embed=1"



def _module_database_url(url: str) -> str:
    """Point module DB URLs at the Postgres service on the lemon network."""
    out = url
    for host in ("127.0.0.1", "localhost", "::1", "host.docker.internal"):
        out = out.replace(f"@{host}:", f"@{POSTGRES_SERVICE}:")
    return out


_lock = threading.Lock()
_state: Optional[dict[str, Any]] = None
_worker: Optional[threading.Thread] = None


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


def _active_port_from_upstream() -> int:
    try:
        text = UPSTREAM_FILE.read_text(encoding="utf-8")
    except OSError:
        return SLOT_PORTS[0]
    for port in SLOT_PORTS:
        if f":{port}" in text:
            return port
    return SLOT_PORTS[0]


def _other_port(port: int) -> int:
    return SLOT_PORTS[1] if port == SLOT_PORTS[0] else SLOT_PORTS[0]


def _container_name(port: int) -> str:
    return f"{CONTAINER_PREFIX}-{port}"


def write_upstream(port: int) -> None:
    UPSTREAM_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = f"upstream lemonseed_owui {{\n  server 127.0.0.1:{port};\n}}\n"
    tmp = UPSTREAM_FILE.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(UPSTREAM_FILE)


def reload_nginx() -> bool:
    try:
        result = subprocess.run(
            ["nginx", "-s", "reload"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        # Fallback when nginx binary not on PATH (systemd unit context).
        try:
            result = subprocess.run(
                ["systemctl", "reload", "nginx.service"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False


def http_ready(port: int, timeout: float = 3.0) -> bool:
    """True when OWUI can serve SPA assets (not just accept TCP on /).

    A bare GET / often succeeds while uvicorn is still wiring routes; the browser
    then gets nginx 502 on /_app/version.json and static files. Probe that path.
    """
    try:
        with urlopen(f"http://127.0.0.1:{port}/_app/version.json", timeout=timeout) as resp:
            if resp.status != 200:
                return False
            body = resp.read(256)
            return bool(body)
    except Exception:  # noqa: BLE001
        return False


def http_ready_stable(port: int, *, attempts: int = 3, delay: float = 2.0, timeout: float = 3.0) -> bool:
    """Require consecutive successes so we never hand the UI a half-booted OWUI."""
    for i in range(attempts):
        if not http_ready(port, timeout=timeout):
            return False
        if i + 1 < attempts:
            time.sleep(delay)
    return True


def _container_running(name: str) -> bool:
    result = _docker(
        ["inspect", "-f", "{{.State.Running}}", name],
        timeout=15,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _container_env_contains(name: str, needle: str) -> bool:
    result = _docker(
        ["inspect", "-f", "{{json .Config.Env}}", name],
        timeout=15,
    )
    return result.returncode == 0 and needle in (result.stdout or "")


def enrich_open_webui_secrets() -> dict[str, str]:
    """Merge Lemonade + OWUI knobs into the module secret file."""
    existing = _module_credentials(MODULE_ID)
    database_url = existing.get("DATABASE_URL", "")
    if not database_url:
        return {}

    api_key = lemonade_api_key()
    secret_key = existing.get("WEBUI_SECRET_KEY") or secrets.token_urlsafe(32)
    # OWUI is on the lemon network; use the Postgres service DNS name.
    database_url = _module_database_url(database_url)
    # OpenAI-compatible Lemonade only. ENABLE_OLLAMA_API is a ConfigVar — pair
    # with ENABLE_PERSISTENT_CONFIG=False so env wins over DB defaults.
    payload = {
        **existing,
        "DATABASE_URL": database_url,
        "WEBUI_AUTH": "False",
        "ENABLE_PERSISTENT_CONFIG": "False",
        "ENABLE_OLLAMA_API": "False",
        "OLLAMA_BASE_URL": "",
        "OLLAMA_BASE_URLS": "",
        "ENABLE_OPENAI_API": "True",
        "OPENAI_API_BASE_URL": LEMONADE_BASE,
        "OPENAI_API_BASE_URLS": LEMONADE_BASE,
        "OPENAI_API_KEY": api_key or existing.get("OPENAI_API_KEY", ""),
        "OPENAI_API_KEYS": api_key or existing.get("OPENAI_API_KEY", ""),
        "WEBUI_SECRET_KEY": secret_key,
        "WEBUI_NAME": "Lemonseed",
        "OFFLINE_MODE": "True",
    }
    _write_module_secret(MODULE_ID, payload)
    return payload


def _env_file_path() -> Path:
    return SECRETS_MODULES / f"{MODULE_ID}.env"


def _stop_container(name: str) -> None:
    _docker(["rm", "-f", name], timeout=60)


def ensure_lemon_network() -> tuple[bool, str]:
    inspect = _docker(["network", "inspect", LEMON_NETWORK], timeout=30)
    if inspect.returncode == 0:
        return True, "exists"
    create = _docker(["network", "create", "--driver", "bridge", LEMON_NETWORK], timeout=30)
    if create.returncode != 0:
        return False, (create.stderr or create.stdout or "network create failed").strip()
    return True, "created"


def _run_container(port: int, env_path: Path) -> tuple[bool, str]:
    name = _container_name(port)
    _stop_container(name)
    ok, detail = ensure_lemon_network()
    if not ok:
        return False, detail
    result = _docker(
        [
            "run",
            "-d",
            "--name",
            name,
            "--restart",
            "unless-stopped",
            "--network",
            LEMON_NETWORK,
            "--add-host",
            "host.docker.internal:host-gateway",
            "-p",
            f"127.0.0.1:{port}:8080",
            "--env-file",
            str(env_path),
            OWUI_IMAGE,
        ],
        timeout=180,
    )
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "docker run failed").strip()
    return True, name


def _pull_image() -> tuple[bool, str]:
    result = _docker(["pull", OWUI_IMAGE], timeout=600)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "docker pull failed").strip()
    return True, "pulled"


def ensure_open_webui(*, force_update: bool = False) -> dict[str, Any]:
    """Ensure a healthy OWUI container is active and nginx points at it."""
    global _state
    if not _lock.acquire(blocking=False):
        return dict(_state or {"state": "busy"})

    try:
        from data_services import ensure_provisioned

        prov = ensure_provisioned()
        if prov.get("state") != "ready":
            _state = {
                "state": "waiting",
                "reason": "data plane not provisioned",
                "provisioning": prov.get("state"),
            }
            return dict(_state)

        env = enrich_open_webui_secrets()
        if not env.get("DATABASE_URL"):
            _state = {"state": "waiting", "reason": "open-webui DATABASE_URL missing"}
            return dict(_state)
        if not env.get("OPENAI_API_KEY"):
            _state = {"state": "waiting", "reason": "Lemonade API key missing"}
            return dict(_state)

        env_path = _env_file_path()
        active = _active_port_from_upstream()
        active_name = _container_name(active)
        running = _container_running(active_name)
        active_ready = http_ready(active)
        env_ok = _container_env_contains(active_name, "ENABLE_OLLAMA_API=False") and (
            _container_env_contains(active_name, "OPENAI_API_BASE_URL=" + LEMONADE_BASE)
            or _container_env_contains(active_name, f"OPENAI_API_BASE_URLS={LEMONADE_BASE}")
        )

        # Sticky ready: serving HTTP with expected env — never touch it.
        if not force_update and active_ready and env_ok:
            _state = {
                "state": "ready",
                "image": OWUI_IMAGE,
                "port": active,
                "container": active_name,
                "path": OWUI_EMBED_PATH,
            }
            return dict(_state)

        # Healthy HTTP but stale env (e.g. Ollama still enabled) → recreate.
        if not force_update and active_ready and not env_ok:
            force_update = True

        # Container still up: tolerate probe blips; never rm -f a live instance.
        if not force_update and running:
            if _state and _state.get("state") == "ready":
                _state = {
                    "state": "ready",
                    "image": OWUI_IMAGE,
                    "port": active,
                    "container": active_name,
                    "path": OWUI_EMBED_PATH,
                }
                return dict(_state)
            _state = {
                "state": "starting",
                "image": OWUI_IMAGE,
                "port": active,
                "container": active_name,
                "path": OWUI_EMBED_PATH,
            }
            return dict(_state)

        _state = {"state": "pulling", "image": OWUI_IMAGE}
        ok, detail = _pull_image()
        if not ok:
            _state = {"state": "error", "error": detail, "phase": "pull"}
            return dict(_state)

        # Blue/green only when deliberately replacing a healthy slot.
        candidate = _other_port(active) if force_update and active_ready else active

        _state = {"state": "starting", "port": candidate, "image": OWUI_IMAGE}
        ok, detail = _run_container(candidate, env_path)
        if not ok:
            _state = {"state": "error", "error": detail, "phase": "run"}
            return dict(_state)

        # Wait until OWUI is stably answering 2xx/3xx — first accept can be a
        # half-booted process that resets connections (500 / connection reset).
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if http_ready_stable(candidate, attempts=3, delay=2.0):
                break
            time.sleep(2)
        else:
            _state = {
                "state": "error",
                "error": "Open WebUI health-check timed out",
                "port": candidate,
            }
            return dict(_state)

        write_upstream(candidate)
        reload_nginx()

        old = _other_port(candidate)
        if old != candidate and _container_running(_container_name(old)):
            _stop_container(_container_name(old))

        _state = {
            "state": "ready",
            "image": OWUI_IMAGE,
            "port": candidate,
            "container": _container_name(candidate),
            "path": OWUI_EMBED_PATH,
        }
        return dict(_state)
    except Exception as exc:  # noqa: BLE001
        _state = {"state": "error", "error": str(exc)}
        return dict(_state)
    finally:
        _lock.release()


def open_webui_is_ready() -> bool:
    if _state and _state.get("state") == "ready":
        return True
    active = _active_port_from_upstream()
    return _container_running(_container_name(active)) and http_ready(active, timeout=1.5)


def ensure_module_worker() -> None:
    global _worker
    if open_webui_is_ready():
        return
    if _worker is not None and _worker.is_alive():
        return

    def _run() -> None:
        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            result = ensure_open_webui()
            if result.get("state") == "ready":
                return
            time.sleep(5)
        ensure_open_webui()

    _worker = threading.Thread(target=_run, name="lemonseed-owui", daemon=True)
    _worker.start()


def modules_snapshot() -> dict[str, Any]:
    global _state
    snap = dict(_state) if _state else {"state": "idle"}
    active = _active_port_from_upstream()
    active_name = _container_name(active)
    running = _container_running(active_name)
    # Always report the live probe — never fake http_ready while nginx 502s.
    ready = http_ready(active, timeout=1.5)
    state = str(snap.get("state") or "idle")
    if ready:
        state = "ready"
    elif state == "ready" and running:
        # Container still up but not answering yet (or briefly) — keep lifecycle
        # as ready for ensure_*, but shell must wait on http_ready=False.
        state = "ready"
    elif state == "ready":
        state = "starting"
    owui = {
        "state": state,
        "image": OWUI_IMAGE,
        "port": snap.get("port", active),
        "path": OWUI_EMBED_PATH,
        "http_ready": ready,
    }
    if snap.get("error"):
        owui["error"] = snap["error"]
    if snap.get("reason") and not ready:
        owui["reason"] = snap["reason"]
    if snap.get("message"):
        owui["message"] = snap["message"]
    return {"open_webui": owui}
