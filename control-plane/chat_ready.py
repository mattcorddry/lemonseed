"""Background catalog pull/load and chat-ready state for the portal."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from catalog import plan_model, select_backend
from hardware import collect_hardware, largest_gpu_index, lemonade_api_key, persist_hardware

CHAT_READY_FILE = Path("/var/lib/lemonseed/chat-ready.json")
SETUP_COMPLETE_FILE = Path("/etc/appliance-setup-complete")
# Written by the amdgpu-kernel-params service just before it reboots to
# apply the GPU memory split; deleted by CP at startup (stale after boot).
REBOOT_PENDING_FILE = Path("/var/lib/lemonseed/reboot-pending.json")

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
        "workflow": "Getting ready",
        "task": "",
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
    if _reboot_pending():
        # The box is about to reboot (GPU memory split). Show that instead of
        # chat progress — the portal keeps polling until the box is back.
        state = {
            "state": "rebooting",
            "progress": 0,
            "message": "Rebooting to apply GPU memory settings…",
            "workflow": "Rebooting",
            "task": "Rebooting to apply GPU memory settings…",
            "model_id": None,
            "display_name": None,
            "reason": None,
            "error": None,
        }
    model_id = state.get("model_id")
    if (
        state.get("state") == "ready"
        and isinstance(model_id, str)
        and model_id
        and not _model_is_loaded(model_id)
    ):
        # Stale ready after lemond restart — kick reload without blocking callers.
        label = state.get("display_name") or model_id
        state = {
            **state,
            "state": "preparing",
            "progress": 70,
            "message": f"Reloading {label}…",
            "workflow": "Downloading an initial LLM",
            "task": f"Reloading {label}",
            "error": None,
        }
        write_chat_ready(state)
        ensure_chat_worker()
    # Ensure structured progress fields for the shell bubble.
    if "workflow" not in state:
        state["workflow"] = _workflow_for_state(str(state.get("state") or "idle"))
    if "task" not in state:
        state["task"] = str(state.get("message") or "")
    return state


def _workflow_for_state(state: str) -> str:
    if state in ("ready",):
        return "Ready"
    if state in ("not-ready", "error"):
        return "Local chat unavailable"
    return "Downloading an initial LLM"


def _reboot_pending() -> bool:
    """True while the kernel-params service has queued a reboot (it writes
    this marker right before `systemctl reboot`). The CP deletes it at
    startup, so a stale marker never survives an actual reboot."""
    try:
        return REBOOT_PENDING_FILE.is_file()
    except OSError:
        return False


def _clear_reboot_pending() -> None:
    try:
        REBOOT_PENDING_FILE.unlink()
    except OSError:
        pass


def clear_reboot_pending() -> None:
    """Drop a stale reboot marker (CP startup, tests)."""
    _clear_reboot_pending()


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


def _verify_completion(model_id: str) -> tuple[bool, str]:
    """Minimal chat completion to prove the loaded model answers."""
    key = lemonade_api_key()
    if not key:
        return False, "Lemonade API key missing"
    body = json.dumps(
        {
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
            "stream": False,
        }
    ).encode("utf-8")
    try:
        req = Request(
            "http://127.0.0.1:13305/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(req, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices") if isinstance(payload, dict) else None
        if isinstance(choices, list) and choices:
            return True, "ok"
        return False, "empty completion response"
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return False, str(exc)


def _hardware_ref(hardware: dict[str, Any]) -> dict[str, Any]:
    """Point at /api/hardware rather than duplicating GPU inventory."""
    return {
        "gpu_index": largest_gpu_index(hardware),
        "ram_gb": hardware.get("ram_gb"),
    }


def _progress_payload(
    *,
    state: str,
    progress: int,
    message: str,
    workflow: str,
    task: str,
    model_id: Optional[str],
    display: Optional[str],
    reason: Any,
    error: Any,
    plan: Any,
    href: Any,
) -> dict[str, Any]:
    return {
        "state": state,
        "progress": progress,
        "message": message,
        "workflow": workflow,
        "task": task,
        "model_id": model_id,
        "display_name": display,
        "reason": reason,
        "error": error,
        "plan": plan,
        "hardware_ref": href,
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
            payload = _progress_payload(
                state="not-ready",
                progress=0,
                message="Local chat is not ready",
                workflow="Local chat unavailable",
                task=str(plan.get("reason") or "No suitable model for this hardware"),
                model_id=None,
                display=None,
                reason=plan.get("reason"),
                error=None,
                plan=plan,
                href=href,
            )
            write_chat_ready(payload)
            return payload

        selection = plan["selection"]
        model_id = str(selection.get("id") or "")
        display = str(selection.get("display_name") or model_id)
        workflow = "Downloading an initial LLM"

        # File state alone is not enough — lemond forgets loads across restart.
        if not force and _model_is_loaded(model_id):
            ok, detail = _verify_completion(model_id)
            if ok:
                payload = _progress_payload(
                    state="ready",
                    progress=100,
                    message="Local chat ready",
                    workflow="Ready",
                    task=f"{display} is ready",
                    model_id=model_id,
                    display=display,
                    reason=plan.get("reason"),
                    error=None,
                    plan=plan,
                    href=href,
                )
                write_chat_ready(payload)
                return payload
            print(f"chat-ready verify retry: {detail}", flush=True)

        write_chat_ready(
            _progress_payload(
                state="preparing",
                progress=5,
                message=f"Preparing {display}…",
                workflow=workflow,
                task=f"Preparing {display}",
                model_id=model_id,
                display=display,
                reason=plan.get("reason"),
                error=None,
                plan=plan,
                href=href,
            )
        )

        try:
            write_chat_ready(
                _progress_payload(
                    state="preparing",
                    progress=20,
                    message=f"Downloading {display}…",
                    workflow=workflow,
                    task=f"Downloading {model_id}",
                    model_id=model_id,
                    display=display,
                    reason=plan.get("reason"),
                    error=None,
                    plan=plan,
                    href=href,
                )
            )
            pull = _run_lemonade(["pull", model_id], timeout=3600)
            if pull.returncode != 0:
                detail = (pull.stderr or pull.stdout or "").strip()[:500]
                raise RuntimeError(f"lemonade pull failed: {detail or pull.returncode}")

            write_chat_ready(
                _progress_payload(
                    state="preparing",
                    progress=75,
                    message=f"Loading {display} onto GPU…",
                    workflow=workflow,
                    task=f"Loading {model_id} onto the GPU",
                    model_id=model_id,
                    display=display,
                    reason=plan.get("reason"),
                    error=None,
                    plan=plan,
                    href=href,
                )
            )
            load_backend = select_backend(hardware)
            load = _run_lemonade(
                ["load", model_id, "--llamacpp", load_backend, "--pinned"],
                timeout=600,
            )
            if load.returncode != 0:
                detail = (load.stderr or load.stdout or "").strip()[:500]
                raise RuntimeError(f"lemonade load failed: {detail or load.returncode}")

            if not _model_is_loaded(model_id):
                raise RuntimeError(
                    f"lemonade load returned success but {model_id} is not in /v1/health"
                )

            write_chat_ready(
                _progress_payload(
                    state="preparing",
                    progress=90,
                    message=f"Verifying {display}…",
                    workflow=workflow,
                    task=f"Verifying {model_id}",
                    model_id=model_id,
                    display=display,
                    reason=plan.get("reason"),
                    error=None,
                    plan=plan,
                    href=href,
                )
            )
            ok, detail = _verify_completion(model_id)
            if not ok:
                raise RuntimeError(f"model verification failed: {detail}")

            payload = _progress_payload(
                state="ready",
                progress=100,
                message="Local chat ready",
                workflow="Ready",
                task=f"{display} is ready",
                model_id=model_id,
                display=display,
                reason=plan.get("reason"),
                error=None,
                plan=plan,
                href=href,
            )
            write_chat_ready(payload)
            return payload
        except Exception as exc:  # noqa: BLE001
            payload = _progress_payload(
                state="error",
                progress=0,
                message="Local chat failed to prepare",
                workflow="Local chat unavailable",
                task=str(exc),
                model_id=model_id,
                display=display,
                reason=plan.get("reason"),
                error=str(exc),
                plan=plan,
                href=href,
            )
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
