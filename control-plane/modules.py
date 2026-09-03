"""Open WebUI Docker module lifecycle (M6.1+): pull, run, health, blue/green."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.request import urlopen

from db import SECRETS_MODULES, _module_credentials, _write_module_secret
from hardware import lemonade_api_key

# Pin a concrete release tag — never :latest / :main in appliance code.
# Rebase this pin to the newest upstream stable after pull + QEMU/hardware smoke.
OWUI_IMAGE_DEFAULT = "ghcr.io/open-webui/open-webui:v0.11.0"
OWUI_IMAGE = os.environ.get("LEMONSEED_OWUI_IMAGE", OWUI_IMAGE_DEFAULT)
MODULE_ID = "open-webui"
UPSTREAM_FILE = Path("/run/lemonseed/owui-upstream.conf")
# nginx serves the Lemonseed shell (not the OWUI upstream) for document requests
# while this flag is absent — so a direct navigation to / before OWUI is up shows
# the "Getting Ready" interstitial instead of a 502 (M6.2).
OWUI_READY_FLAG = Path("/run/lemonseed/owui-ready")
SLOT_PORTS = (18080, 18081)
CONTAINER_PREFIX = "lemonseed-owui"
LEMON_NETWORK = "lemon"
POSTGRES_SERVICE = "lemonseed-postgres"
# lemond listens on 127.0.0.1 only. Containers reach it via nginx on the host
# gateway (host.docker.internal → lemon bridge IP :80), not :13305 directly.
LEMONADE_BASE = "http://host.docker.internal/_lemonseed/lemond/v1"
# OWUI is a root SPA — iframe must load path /, not /chat/.
OWUI_EMBED_PATH = "/?embed=1"

# M6.3a: appliance identity for all models (OWUI merges into chat params).
LEMONSEED_SYSTEM_PROMPT = (
    "You are Lemonseed, a helpful AI running on this household appliance. "
    "Be clear, practical, and privacy-conscious. When the user's name is known, "
    "address them as {{ USER_NAME }}. Today's date is {{ CURRENT_DATE }}."
)

# OWUI v0.11 defaults to native tool calling and injects Knowledge/Files tools.
# With no seeded KBs, small models call query_knowledge_bases / query_knowledge_files
# and then apologize that nothing was found. Prefer legacy completion (older OWUI
# behavior) until we ship curated on-appliance sources (DESIGN backlog).
DEFAULT_MODEL_PARAMS = {
    "system": LEMONSEED_SYSTEM_PROMPT,
    "function_calling": "legacy",
}
DEFAULT_MODEL_METADATA = {
    "capabilities": {
        "builtin_tools": False,
        "web_search": False,
        "image_generation": False,
        "code_interpreter": False,
    },
    "builtinTools": {
        "knowledge": False,
        "files": False,
        "web_search": False,
        "image_generation": False,
        "code_interpreter": False,
        "channels": False,
        "tasks": False,
        "automations": False,
    },
}



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
    # Mark OWUI ready so nginx routes document requests to it. write_upstream is
    # only called after http_ready_stable() passes, so the flag is accurate.
    try:
        OWUI_READY_FLAG.write_text("1\n", encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 — non-fatal; shell falls back to 502
        print(f"owui-ready flag: {exc}", flush=True)


def reload_nginx() -> bool:
    """Reload the systemd-managed nginx so /run/lemonseed/owui-upstream.conf applies.

    On NixOS, bare `nginx -s reload` often misses the unit's pid file and is a
    no-op for the live gateway — prefer systemctl, then SIGHUP MainPID.
    """
    for cmd in (
        ["systemctl", "reload", "nginx.service"],
        ["systemctl", "reload", "nginx"],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if result.returncode == 0:
                return True
            detail = (result.stderr or result.stdout or "").strip()[:200]
            if detail:
                print(f"nginx reload ({' '.join(cmd)}): {detail}", flush=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"nginx reload ({' '.join(cmd)}): {exc}", flush=True)

    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", "nginx.service"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        pid_s = (result.stdout or "").strip()
        pid = int(pid_s) if pid_s.isdigit() else 0
        if pid > 1:
            os.kill(pid, signal.SIGHUP)
            print(f"nginx reload: sent SIGHUP to pid {pid}", flush=True)
            return True
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"nginx reload (SIGHUP): {exc}", flush=True)

    try:
        result = subprocess.run(
            ["nginx", "-s", "reload"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0:
            return True
        detail = (result.stderr or result.stdout or "").strip()[:200]
        print(f"nginx -s reload: {detail or result.returncode}", flush=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"nginx -s reload: {exc}", flush=True)
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


def _container_env_map(name: str) -> dict[str, str]:
    """Parse docker Config.Env into a key→value map."""
    result = _docker(
        ["inspect", "-f", "{{json .Config.Env}}", name],
        timeout=15,
    )
    if result.returncode != 0:
        return {}
    try:
        entries = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    if not isinstance(entries, list):
        return out
    for item in entries:
        if not isinstance(item, str) or "=" not in item:
            continue
        key, value = item.split("=", 1)
        out[key] = value
    return out


# Keys that must match the secrets file or we blue/green recreate OWUI.
# lemonseed-update refreshes SquashFS/CP; without this check a healthy container
# keeps stale Docker Env forever (DEFAULT_MODEL_PARAMS etc.).
_POLICY_ENV_KEYS = (
    "ENABLE_OLLAMA_API",
    "OPENAI_API_BASE_URL",
    "OPENAI_API_BASE_URLS",
    "ENABLE_PERSISTENT_CONFIG",
    "ENABLE_VERSION_UPDATE_CHECK",
    "OFFLINE_MODE",
    "WEBUI_NAME",
    "DEFAULT_MODEL_PARAMS",
    "DEFAULT_MODEL_METADATA",
    "ENABLE_WEB_SEARCH",
    "ENABLE_IMAGE_GENERATION",
    "ENABLE_CHANNELS",
)


def _policy_env_ok(name: str, desired: dict[str, str]) -> bool:
    """True when the running container Env matches Lemonseed policy knobs."""
    current = _container_env_map(name)
    if not current:
        return False
    for key in _POLICY_ENV_KEYS:
        if key not in desired:
            continue
        if current.get(key) != desired[key]:
            return False
    return True


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
    # M6.3a policy: autocomplete off, automations on, workspace off, notes/memory
    # on; web search / image gen / community off until sidecars land; channels
    # deferred until we seed a shared “everyone” channel.
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
        "ENABLE_VERSION_UPDATE_CHECK": "False",
        "ENABLE_AUTOCOMPLETE_GENERATION": "False",
        "ENABLE_AUTOMATIONS": "True",
        "USER_PERMISSIONS_FEATURES_AUTOMATIONS": "True",
        "ENABLE_NOTES": "True",
        "USER_PERMISSIONS_FEATURES_NOTES": "True",
        "ENABLE_MEMORIES": "True",
        "ENABLE_MEMORY_SYSTEM_CONTEXT": "True",
        "USER_PERMISSIONS_FEATURES_MEMORIES": "True",
        "ENABLE_CHANNELS": "False",
        "USER_PERMISSIONS_FEATURES_CHANNELS": "False",
        "ENABLE_WEB_SEARCH": "False",
        "ENABLE_IMAGE_GENERATION": "False",
        "ENABLE_IMAGE_EDIT": "False",
        "ENABLE_COMMUNITY_SHARING": "False",
        "USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS": "False",
        "USER_PERMISSIONS_WORKSPACE_KNOWLEDGE_ACCESS": "False",
        "USER_PERMISSIONS_WORKSPACE_PROMPTS_ACCESS": "False",
        "USER_PERMISSIONS_WORKSPACE_TOOLS_ACCESS": "False",
        "USER_PERMISSIONS_WORKSPACE_SKILLS_ACCESS": "False",
        "DEFAULT_MODEL_METADATA": json.dumps(DEFAULT_MODEL_METADATA),
        "DEFAULT_MODEL_PARAMS": json.dumps(DEFAULT_MODEL_PARAMS),
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
    run_args = [
        "run",
        "-d",
        "--name",
        name,
        "--restart",
        "no",
        "--network",
        LEMON_NETWORK,
        "--add-host",
        "host.docker.internal:host-gateway",
        "-p",
        f"127.0.0.1:{port}:8080",
        "--env-file",
        str(env_path),
        OWUI_IMAGE,
    ]
    result = _docker(run_args, timeout=180)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "docker run failed").strip()
        # Retry once after forced remove (TCG / race can leave a named container).
        if "already in use" in err.lower():
            _stop_container(name)
            result = _docker(run_args, timeout=180)
            if result.returncode != 0:
                return False, (result.stderr or result.stdout or err).strip()
            return True, name
        return False, err
    return True, name


def _image_present() -> bool:
    return _docker(["image", "inspect", OWUI_IMAGE], timeout=30).returncode == 0


def _pull_image() -> tuple[bool, str]:
    if _image_present():
        return True, "present"
    # QEMU smoke preloads OWUI via virtio-fs. Prefer waiting over a doomed DNS pull.
    wait_s = int(os.environ.get("LEMONSEED_OWUI_WAIT_LOCAL_SECONDS", "0") or "0")
    if wait_s > 0:
        return False, "waiting for local image preload"
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
        env_ok = _policy_env_ok(active_name, env)

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

        # Healthy HTTP but stale env (policy bump after lemonseed-update) → recreate.
        # Mark starting immediately so the shell hides the old iframe before cutover.
        if not force_update and active_ready and not env_ok:
            force_update = True
            _state = {
                "state": "starting",
                "image": OWUI_IMAGE,
                "port": active,
                "container": active_name,
                "path": OWUI_EMBED_PATH,
                "reason": "Updating chat interface settings",
            }

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
            if detail.startswith("waiting"):
                _state = {
                    "state": "waiting",
                    "reason": detail,
                    "image": OWUI_IMAGE,
                }
            else:
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
        ready_budget = int(os.environ.get("LEMONSEED_OWUI_READY_SECONDS", "240") or "240")
        deadline = time.monotonic() + max(60, ready_budget)
        while time.monotonic() < deadline:
            if http_ready_stable(candidate, attempts=3, delay=2.0):
                break
            time.sleep(2)
        else:
            # Under TCG the container may still be booting — keep it and retry.
            if _container_running(_container_name(candidate)):
                _state = {
                    "state": "starting",
                    "image": OWUI_IMAGE,
                    "port": candidate,
                    "container": _container_name(candidate),
                    "path": OWUI_EMBED_PATH,
                    "reason": "Open WebUI health-check still pending",
                }
                return dict(_state)
            _state = {
                "state": "error",
                "error": "Open WebUI health-check timed out",
                "port": candidate,
            }
            return dict(_state)

        write_upstream(candidate)
        if not reload_nginx():
            # Keep the old slot running — cutting over without a reload leaves
            # nginx pointed at a dead port (shell stuck on "Opening chat…").
            _state = {
                "state": "error",
                "error": "Chat upstream updated but nginx reload failed",
                "port": candidate,
                "container": _container_name(candidate),
                "path": OWUI_EMBED_PATH,
            }
            return dict(_state)

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


# Docker --restart no: CP owns heal. Contiguous probe failures before recreate.
HEALTH_FAIL_LIMIT = 5
SUPERVISE_INTERVAL_SEC = 10
_health_fail_count = 0


def supervise_modules_once() -> dict[str, Any]:
    """One supervisor tick for Lemonseed-managed OWUI.

    With ``--restart no``, Docker will not revive a crashed container. We:
    - start (via ensure_open_webui) when the active slot is not running;
    - after HEALTH_FAIL_LIMIT contiguous failed HTTP probes, force-recreate;
    - otherwise call ensure_open_webui so policy Env drift still blue/greens.
    """
    global _state, _health_fail_count

    active = _active_port_from_upstream()
    name = _container_name(active)
    running = _container_running(name)
    ready = http_ready(active, timeout=1.5) if running else False

    # nginx serves the shell (not the OWUI upstream) for document requests while
    # OWUI is down, so a direct / navigation shows the interstitial, not a 502.
    if ready:
        try:
            OWUI_READY_FLAG.write_text("1\n", encoding="utf-8")
        except OSError:  # noqa: BLE001
            pass
    else:
        OWUI_READY_FLAG.unlink(missing_ok=True)

    if running and ready:
        _health_fail_count = 0
        return ensure_open_webui(force_update=False)

    if not running:
        prev = _health_fail_count
        _health_fail_count = 0
        if prev:
            print("owui supervise: container exited; restarting", flush=True)
        else:
            print("owui supervise: container not running; starting", flush=True)
        return ensure_open_webui(force_update=False)

    # Process is up but not serving (hang, half-boot, bad upstream).
    _health_fail_count += 1
    _state = {
        "state": "starting",
        "image": OWUI_IMAGE,
        "port": active,
        "container": name,
        "path": OWUI_EMBED_PATH,
        "reason": "Chat is temporarily unavailable. Trying again…",
    }
    if _health_fail_count < HEALTH_FAIL_LIMIT:
        return dict(_state)

    print(
        f"owui supervise: {HEALTH_FAIL_LIMIT} contiguous health failures; recreating",
        flush=True,
    )
    _health_fail_count = 0
    return ensure_open_webui(force_update=True)


def ensure_module_worker() -> None:
    """Long-lived supervisor: keep OWUI up after boot and after crashes."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return

    def _run() -> None:
        while True:
            try:
                supervise_modules_once()
            except Exception as exc:  # noqa: BLE001 — never kill the supervisor
                print(f"owui supervise: {exc}", flush=True)
            time.sleep(SUPERVISE_INTERVAL_SEC)

    _worker = threading.Thread(target=_run, name="lemonseed-owui", daemon=True)
    _worker.start()


def modules_snapshot() -> dict[str, Any]:
    global _state
    snap = dict(_state) if _state else {"state": "idle"}
    active = _active_port_from_upstream()
    active_name = _container_name(active)
    running = _container_running(active_name)
    # Always report the live probe — never fake http_ready while nginx 502s.
    probe_ready = http_ready(active, timeout=1.5)
    lifecycle = str(snap.get("state") or "idle")
    # During pull/start/replace, keep the shell on Getting Ready even if the
    # old slot still answers HTTP (policy blue/green after lemonseed-update).
    updating = lifecycle in ("pulling", "starting", "busy", "waiting")
    ready = bool(probe_ready and not updating)
    if updating:
        state = lifecycle
    elif ready:
        state = "ready"
    elif lifecycle == "ready" and running:
        # Container still up but not answering yet (or briefly) — keep lifecycle
        # as ready for ensure_*, but shell must wait on http_ready=False.
        state = "ready"
    elif lifecycle == "ready":
        state = "starting"
    else:
        state = lifecycle
    image = str(snap.get("image") or OWUI_IMAGE)
    tag = image.rsplit(":", 1)[-1] if ":" in image else image
    workflow, task = _owui_progress_copy(state, tag, snap)
    owui = {
        "state": state,
        "image": image,
        "port": snap.get("port", active),
        "path": OWUI_EMBED_PATH,
        "http_ready": ready,
        "workflow": workflow,
        "task": task,
    }
    if snap.get("error"):
        owui["error"] = snap["error"]
    if snap.get("reason") and not ready:
        owui["reason"] = snap["reason"]
    if snap.get("message"):
        owui["message"] = snap["message"]
    return {"open_webui": owui}


def _owui_progress_copy(state: str, tag: str, snap: dict[str, Any]) -> tuple[str, str]:
    if state == "pulling" or snap.get("phase") == "pull":
        return "Configuring chat", f"Pulling the Open WebUI {tag} container"
    if state in ("starting", "busy"):
        return "Configuring chat", f"Starting Open WebUI ({tag})"
    if state == "waiting":
        reason = str(snap.get("reason") or "Waiting for storage services")
        return "Configuring chat", reason
    if state == "error":
        return "Configuring chat", str(snap.get("error") or "Chat interface failed")
    if state == "ready":
        return "Configuring chat", "Chat interface is ready"
    return "Configuring chat", "Preparing the chat interface"
