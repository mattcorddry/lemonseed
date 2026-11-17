"""LiteLLM proxy — the appliance's one OpenAI-compatible gateway (M9 P1).

Core component, same supervision model as Postgres/Valkey: the CP is the
authority, the container is disposable, the config is fully generated. lemond
keeps its native name space; everything downstream (OWUI today, other LAN apps
later) consumes the manifest-owned friendly entities through this proxy.

Config is rendered deterministically from the active lemond model and written
to /var/lib/lemonseed/litellm/config.yaml. The container is recreated whenever
the file's hash changes, so "changing the model" is just a file edit plus the
existing supervisor reconcile — no blue/green, no state.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Optional

import chat_ready
import hardware
import image_pins
from data_services import (
    LEMON_NETWORK,
    _docker,
    _ensure_lemon_network,
    _pull_image,
    _resolve_ref,
)

LITELLM_IMAGE_KEY = "litellm"
LITELLM_IMAGE = image_pins.tag_ref(LITELLM_IMAGE_KEY)
LITELLM_CONTAINER = "lemonseed-litellm"
LITELLM_PORT = 4000
LITELLM_HOST_PORT = "127.0.0.1:4000"

# The path OWUI and other consumers point at (Docker DNS on the lemon network).
LITELLM_BASE = f"http://{LITELLM_CONTAINER}:{LITELLM_PORT}/v1"

DATA_ROOT = Path("/var/lib/lemonseed/litellm")
CONFIG_PATH = DATA_ROOT / "config.yaml"
# The config hash the running container was started with. A marker file (not
# CP memory) because the CP restarts far more often than the container does:
# without it, a model switch while the CP was down would never recreate.
APPLIED_HASH_PATH = DATA_ROOT / "config.sha256"
MASTER_KEY_PATH = DATA_ROOT / "master-key"

# The two manifest-owned model entities. The OWUI model definitions in
# owui-policy.json wrap these names via base_model_id — keep the two files in
# sync (test_litellm.py asserts the policy matches these constants).
ENTITY_PLAIN = "Lemonseed AI"
ENTITY_FILTERED = "Lemonseed AI with Content Filters"
GUARDRAIL_NAME = "lemonseed-harmful-child-safety"
GUARDRAIL_ID = "litellm_content_filter"
GUARDRAIL_CATEGORY = "harmful_child_safety"

# lemond is reached through the same nginx path OWUI uses today, so LiteLLM
# introduces no new host port and no new exposure.
LEMOND_BASE = "http://host.docker.internal/_lemonseed/lemond/v1"

SUPERVISE_INTERVAL_SEC = 10
PULL_TIMEOUT_SEC_LITELLM = 900

_key_lock = threading.Lock()


def _active_model_id() -> str:
    """The lemond model currently (or last) chosen; empty before first choice."""
    try:
        model_id = chat_ready.read_chat_ready().get("model_id")
    except Exception:  # noqa: BLE001 — a broken state file must not kill the tick
        return ""
    return model_id if isinstance(model_id, str) and model_id else ""


def master_key() -> str:
    """The CP-generated LiteLLM master key, generated once and reused.

    Persisted 0600 under /var/lib/lemonseed/litellm/ so the proxy and every
    future consumer (per-user virtual keys, the OpenAI endpoint toggle) share
    one durable secret that survives reboots and re-provisioning.
    """
    with _key_lock:
        try:
            raw = MASTER_KEY_PATH.read_text(encoding="utf-8").strip()
            if raw:
                return raw
        except OSError:
            pass
        key = "sk-lemonseed-" + secrets.token_urlsafe(32)
        try:
            MASTER_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = MASTER_KEY_PATH.with_suffix(".tmp")
            tmp.write_text(key + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(MASTER_KEY_PATH)
        except OSError:
            # The config render below still carries the key; the next tick
            # regenerates and rewrites the file. Never fail a tick over this.
            pass
        return key


def _lemond_api_key() -> str:
    """The API key LiteLLM uses for the lemond backend (as OWUI has today)."""
    return hardware.lemonade_api_key() or "none"


def _entity_block(model_id: str, guarded: bool) -> list[str]:
    lines = [
        f"  - model_name: {ENTITY_FILTERED if guarded else ENTITY_PLAIN}",
        "    litellm_params:",
        f"      model: openai/{model_id}",
        f"      api_base: {LEMOND_BASE}",
        f"      api_key: {_lemond_api_key()}",
    ]
    if guarded:
        lines.append(f"      guardrails: [{GUARDRAIL_NAME}]")
    return lines


def render_config(
    model_id: str,
    filtered_enabled: bool,
    mcp_servers: Optional[list[dict[str, str]]] = None,
) -> str:
    """The complete config.yaml from two inputs: the active lemond model and
    the enabled plugins' MCP contributions.

    Deterministic (unit-tested byte-for-byte): identical inputs render an
    identical file, so the hash fingerprint below is a safe change signal.
    The filtered entity is the same backend plus the guardrail reference —
    a config-only difference, per D33.

    ``mcp_servers`` is the platform-config contribution surface (MODULES.md
    §6): LiteLLM's ``mcp_servers`` block is a *mapping* keyed by server name,
    and LiteLLM rejects keys containing a hyphen at startup — the catalog
    refuses such names before they can reach this render.
    """
    lines = [
        "# generated by lemonseed-control-plane — do not edit",
        "model_list:",
        *_entity_block(model_id, guarded=False),
    ]
    if filtered_enabled:
        lines += _entity_block(model_id, guarded=True)
        lines += [
            "guardrails:",
            f"  - guardrail_name: {GUARDRAIL_NAME}",
            "    litellm_params:",
            f"      guardrail: {GUARDRAIL_ID}",
            "      mode: [pre_call, post_call]",
            "      default_on: true",
            "      categories:",
            "        - category: harmful_child_safety",
            "          action: BLOCK",
            "          severity_threshold: medium",
        ]
    servers = mcp_servers or []
    if servers:
        lines += ["mcp_servers:"]
        for server in servers:
            lines += [
                f"  {server['name']}:",
                f"    url: {server['url']}",
                "    transport: http",
            ]
    else:
        lines += ["mcp_servers: []"]
    lines += [
        "general_settings:",
        "  master_key: " + master_key(),
    ]
    return "\n".join(lines) + "\n"


def write_config(content: str) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


def config_fingerprint() -> str:
    try:
        return hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
    except OSError:
        return ""


def applied_hash() -> str:
    try:
        return APPLIED_HASH_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _set_applied_hash(value: str) -> None:
    try:
        APPLIED_HASH_PATH.write_text(value + "\n", encoding="utf-8")
    except OSError:
        pass


def _litellm_run_args(ref: str) -> list[str]:
    return [
        "run",
        "-d",
        "--name",
        LITELLM_CONTAINER,
        "--restart",
        "unless-stopped",
        "--network",
        LEMON_NETWORK,
        "--add-host",
        "host.docker.internal:host-gateway",
        "-p",
        f"{LITELLM_HOST_PORT}:{LITELLM_PORT}",
        "-v",
        f"{CONFIG_PATH}:/app/config.yaml:ro",
        ref,
        "--config",
        "/app/config.yaml",
        "--port",
        str(LITELLM_PORT),
    ]


_state: dict[str, Any] = {LITELLM_CONTAINER: {"state": "starting"}}
_lock = threading.Lock()


def _snapshot_locked() -> dict[str, Any]:
    snap = dict(_state[LITELLM_CONTAINER])
    snap["container"] = LITELLM_CONTAINER
    snap["endpoint"] = LITELLM_BASE
    return snap


def litellm_ready() -> bool:
    """True once the proxy is running and its port is serving."""
    snap = _state[LITELLM_CONTAINER]
    return snap.get("state") == "ready"


def litellm_snapshot() -> dict[str, Any]:
    with _lock:
        return _snapshot_locked()


def _container_running() -> bool:
    result = _docker(
        ["inspect", "-f", "{{.State.Running}}", LITELLM_CONTAINER], timeout=15
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _container_exists() -> bool:
    return _docker(["inspect", "-f", "{{.Name}}", LITELLM_CONTAINER], timeout=15).returncode == 0


def _port_open(timeout: float = 2.0) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", LITELLM_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _start_litellm() -> None:
    state = _state[LITELLM_CONTAINER]
    if not _ensure_lemon_network():
        state["state"] = "error"
        state["error"] = "Docker network 'lemon' unavailable"
        return
    ok, detail = _pull_image(LITELLM_IMAGE_KEY, "litellm proxy")
    if not ok:
        state["state"] = "waiting"
        state["error"] = detail
        return
    ref = _resolve_ref(LITELLM_IMAGE_KEY)
    result = _docker(_litellm_run_args(ref), timeout=180)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "docker run failed").strip()
        if "already in use" in err.lower():
            _docker(["rm", "-f", LITELLM_CONTAINER], timeout=60)
            result = _docker(_litellm_run_args(ref), timeout=180)
            err = (result.stderr or result.stdout or err).strip()
        state["state"] = "error"
        state["error"] = err[:500]
        return
    state["state"] = "starting"
    state["since"] = time.monotonic()
    state["image"] = ref
    state.pop("error", None)
    _set_applied_hash(config_fingerprint())
    print("litellm supervise: proxy container started", flush=True)


def _module_contributions() -> list[dict[str, str]]:
    """Enabled plugin MCP contributions (§6), best effort: a broken module
    layer degrades to no MCP servers, exactly like a broken litellm module
    degrades the OWUI connection — the chat path must not hard-fail."""
    try:
        from module_manager import litellm_contributions

        return litellm_contributions()
    except Exception as exc:  # noqa: BLE001 — service boundary
        print(f"litellm supervise: module contributions unavailable: {exc}", flush=True)
        return []


def supervise_litellm_once() -> None:
    """One reconcile tick: keep the proxy running under the current config."""
    global _config_hash_seen
    with _lock:
        state = _state[LITELLM_CONTAINER]
        model_id = _active_model_id()
        if not model_id:
            # No model chosen yet — there is no model name to front. Waiting,
            # never erroring: the choice appears on the very first boot.
            state["state"] = "waiting"
            state["reason"] = "no model selected yet"
            return
        state.pop("reason", None)

        # Config is the authority: a model switch (or guardrail toggle, or an
        # installed plugin's MCP contribution) changes the file, and the file
        # change is what the container obeys. Recreate whenever the running
        # container's config is not the current one.
        content = render_config(
            model_id, filtered_enabled=True, mcp_servers=_module_contributions()
        )
        current_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if config_fingerprint() != current_hash:
            try:
                write_config(content)
                # The smoke asserts this line in the serial log to prove an
                # installed plugin's MCP contribution really reached the
                # rendered config — and that removing it un-registers.
                print(
                    "litellm supervise: wrote config"
                    f" ({len(_module_contributions())} mcp server(s))",
                    flush=True,
                )
            except OSError as exc:
                state["state"] = "error"
                state["error"] = f"cannot write config: {exc}"
                return
        if _container_exists() and applied_hash() != current_hash:
            print("litellm supervise: config changed; recreating", flush=True)
            _docker(["rm", "-f", LITELLM_CONTAINER], timeout=60)

        running = _container_running()
        if running and _port_open():
            state["state"] = "ready"
            state.pop("error", None)
            return
        if running:
            # Tolerate the first minutes of a cold start (TCG, first boot).
            since = float(state.get("since") or 0.0)
            if time.monotonic() - since < 90:
                state["state"] = "starting"
                return
            print("litellm supervise: not serving after grace; recreating", flush=True)
            _docker(["rm", "-f", LITELLM_CONTAINER], timeout=60)
        if _container_exists():
            if _docker(["start", LITELLM_CONTAINER], timeout=120).returncode != 0:
                _docker(["rm", "-f", LITELLM_CONTAINER], timeout=60)
                _start_litellm()
        else:
            _start_litellm()


_lit_worker: Optional[threading.Thread] = None


def ensure_litellm_worker() -> None:
    """Long-lived supervisor thread, same shape as the data-plane worker."""
    global _lit_worker
    if _lit_worker is not None and _lit_worker.is_alive():
        return

    def _run() -> None:
        while True:
            try:
                supervise_litellm_once()
            except Exception as exc:  # noqa: BLE001 — never kill the supervisor
                print(f"litellm supervise: {exc}", flush=True)
            time.sleep(SUPERVISE_INTERVAL_SEC)

    _lit_worker = threading.Thread(target=_run, name="lemonseed-litellm", daemon=True)
    _lit_worker.start()
