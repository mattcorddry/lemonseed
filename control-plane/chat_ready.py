"""Background catalog pull/load and chat-ready state for the portal."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from urllib.request import Request, urlopen

from catalog import plan_model
from hardware import collect_hardware, largest_gpu_index, lemonade_api_key, persist_hardware

CHAT_READY_FILE = Path("/var/lib/lemonseed/chat-ready.json")
SETUP_COMPLETE_FILE = Path("/etc/appliance-setup-complete")

# Spawn lock is only for starting the worker thread — never hold it across pull/load.
_spawn_lock = threading.Lock()
# Prepare lock serializes pull/load; acquire non-blocking so HTTP never waits on it.
_prepare_lock = threading.Lock()
_worker: Optional[threading.Thread] = None
_state_lock = threading.Lock()


def _default_state() -> dict[str, Any]:
    return {
        "state": "idle",
        "progress": 0,
        "message": "",
        "model_id": None,
        "display_name": None,
        "reason": None,
        "error": None,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def read_chat_ready() -> dict[str, Any]:
    try:
        raw = CHAT_READY_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return _default_state()


def write_chat_ready(payload: dict[str, Any]) -> None:
    payload = dict(payload)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with _state_lock:
        try:
            CHAT_READY_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = CHAT_READY_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o640)
            tmp.replace(CHAT_READY_FILE)
        except OSError as exc:
            print(f"chat-ready persist: {exc}", flush=True)


def chat_snapshot() -> dict[str, Any]:
    """Portal/API view of chat readiness; reconcile file state with lemond load."""
    state = read_chat_ready()
    model_id = state.get("model_id")
    if (
        state.get("state") == "ready"
        and isinstance(model_id, str)
        and model_id
        and not _model_is_loaded(model_id)
    ):
        # Stale ready after lemond restart — kick reload without blocking callers.
        state = {
            **state,
            "state": "preparing",
            "progress": 70,
            "message": f"Reloading {state.get('display_name') or model_id}…",
            "error": None,
        }
        write_chat_ready(state)
        ensure_chat_worker()
    return state


def _run_lemonade(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    key = lemonade_api_key()
    env = os.environ.copy()
    if key:
        env["LEMONADE_API_KEY"] = key
    return subprocess.run(
        ["lemonade", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _health() -> dict[str, Any]:
    key = lemonade_api_key()
    if not key:
        return {}
    try:
        req = Request(
            "http://127.0.0.1:13305/v1/health",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urlopen(req, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _loaded_model_names(health: Optional[dict[str, Any]] = None) -> set[str]:
    """Names currently resident in lemond (not merely downloaded)."""
    data = health if health is not None else _health()
    names: set[str] = set()
    single = data.get("model_loaded")
    if isinstance(single, str) and single:
        names.add(single)
    multi = data.get("all_models_loaded")
    if isinstance(multi, list):
        for entry in multi:
            if isinstance(entry, str) and entry:
                names.add(entry)
            elif isinstance(entry, dict):
                for key in ("model_name", "model", "id", "name"):
                    value = entry.get(key)
                    if isinstance(value, str) and value:
                        names.add(value)
                        break
    return names


def _model_is_loaded(model_id: str) -> bool:
    return model_id in _loaded_model_names()


def _hardware_ref(hardware: dict[str, Any]) -> dict[str, Any]:
    """Point at /api/hardware rather than duplicating GPU inventory."""
    return {
        "gpu_index": largest_gpu_index(hardware),
        "ram_gb": hardware.get("ram_gb"),
    }


def prepare_local_chat(*, force: bool = False) -> dict[str, Any]:
    """Plan, pull, and load the catalog model. Safe to call from a worker thread.

    Never call this from an HTTP handler — it can take minutes. HTTP paths only
    read chat_snapshot() / ensure_chat_worker().
    """
    if not _prepare_lock.acquire(blocking=False):
        # Another prepare is in flight; leave HTTP free to poll state.
        return read_chat_ready()

    try:
        hardware = collect_hardware()
        persist_hardware(hardware)
        plan = plan_model(hardware)
        href = _hardware_ref(hardware)
        if plan.get("state") != "selected" or not plan.get("selection"):
            payload = {
                "state": "not-ready",
                "progress": 0,
                "message": "Local chat is not ready",
                "model_id": None,
                "display_name": None,
                "reason": plan.get("reason"),
                "error": None,
                "plan": plan,
                "hardware_ref": href,
            }
            write_chat_ready(payload)
            return payload

        selection = plan["selection"]
        model_id = str(selection.get("id") or "")
        display = str(selection.get("display_name") or model_id)

        # File state alone is not enough — lemond forgets loads across restart.
        if not force and _model_is_loaded(model_id):
            payload = {
                "state": "ready",
                "progress": 100,
                "message": "Local chat ready",
                "model_id": model_id,
                "display_name": display,
                "reason": plan.get("reason"),
                "error": None,
                "plan": plan,
                "hardware_ref": href,
            }
            write_chat_ready(payload)
            return payload

        write_chat_ready(
            {
                "state": "preparing",
                "progress": 5,
                "message": f"Preparing {display}…",
                "model_id": model_id,
                "display_name": display,
                "reason": plan.get("reason"),
                "error": None,
                "plan": plan,
                "hardware_ref": href,
            }
        )

        try:
            write_chat_ready(
                {
                    "state": "preparing",
                    "progress": 20,
                    "message": f"Downloading {display}…",
                    "model_id": model_id,
                    "display_name": display,
                    "reason": plan.get("reason"),
                    "error": None,
                    "plan": plan,
                    "hardware_ref": href,
                }
            )
            pull = _run_lemonade(["pull", model_id], timeout=3600)
            if pull.returncode != 0:
                detail = (pull.stderr or pull.stdout or "").strip()[:500]
                raise RuntimeError(f"lemonade pull failed: {detail or pull.returncode}")

            write_chat_ready(
                {
                    "state": "preparing",
                    "progress": 75,
                    "message": f"Loading {display} onto GPU…",
                    "model_id": model_id,
                    "display_name": display,
                    "reason": plan.get("reason"),
                    "error": None,
                    "plan": plan,
                    "hardware_ref": href,
                }
            )
            # Pin so max_loaded_models=1 does not evict the appliance default.
            load = _run_lemonade(
                ["load", model_id, "--llamacpp", "cuda", "--pinned"],
                timeout=600,
            )
            if load.returncode != 0:
                detail = (load.stderr or load.stdout or "").strip()[:500]
                raise RuntimeError(f"lemonade load failed: {detail or load.returncode}")

            if not _model_is_loaded(model_id):
                raise RuntimeError(
                    f"lemonade load returned success but {model_id} is not in /v1/health"
                )

            payload = {
                "state": "ready",
                "progress": 100,
                "message": "Local chat ready",
                "model_id": model_id,
                "display_name": display,
                "reason": plan.get("reason"),
                "error": None,
                "plan": plan,
                "hardware_ref": href,
            }
            write_chat_ready(payload)
            return payload
        except Exception as exc:  # noqa: BLE001
            payload = {
                "state": "error",
                "progress": 0,
                "message": "Local chat failed to prepare",
                "model_id": model_id,
                "display_name": display,
                "reason": plan.get("reason"),
                "error": str(exc),
                "plan": plan,
                "hardware_ref": href,
            }
            write_chat_ready(payload)
            return payload
    finally:
        _prepare_lock.release()


def ensure_chat_worker() -> None:
    """Kick a background prepare pass; returns immediately (never waits on pull/load)."""
    global _worker
    if not SETUP_COMPLETE_FILE.is_file():
        return
    with _spawn_lock:
        if _worker is not None and _worker.is_alive():
            return

        def _run() -> None:
            # Wait briefly for lemond after boot / setup.
            time.sleep(2)
            try:
                prepare_local_chat(force=False)
            except Exception as exc:  # noqa: BLE001
                print(f"chat worker: {exc}", flush=True)

        _worker = threading.Thread(target=_run, name="lemonseed-chat-ready", daemon=True)
        _worker.start()
