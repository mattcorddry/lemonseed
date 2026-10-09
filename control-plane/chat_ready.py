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
from urllib.parse import quote
from urllib.request import Request, urlopen

import catalog
from catalog import plan_model, select_backend
from hardware import MODELS_DIR, collect_hardware, largest_gpu_index, lemonade_api_key, persist_hardware
from lemond_client import get_client
from model_store import ensure_model_space

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

# Consecutive load/verify failures for the SAME model before we stop retrying.
# lemond's backend watchdog restarts a crashing llamacpp process forever; without
# this cap the CP would re-verify and re-queue the same model in a tight loop.
# The count resets whenever a different model is prepared or a verify succeeds.
_MAX_VERIFY_FAILURES = 3
_verify_failures: dict[str, int] = {}
_verify_failures_lock = threading.Lock()

# A model that has been given up on (terminal load/verify failure) is not
# re-loaded until the user makes a new choice or the hardware reference changes.
# Without this gate, every /api/chat poll would respawn the worker and re-issue
# the same failing `lemonade load` forever (lemond's own Router also retries
# internally, so the loop is visible even without our re-queue).
_gave_up_models: dict[str, str] = {}  # model_id -> hardware_ref that failed
_gave_up_lock = threading.Lock()

# Active `lemonade pull` subprocess — killed when the user switches model so
# we don't finish downloading a model the user no longer wants.
_pull_process: Optional[subprocess.Popen] = None
_pull_lock = threading.Lock()
# Set by cancel_active_pull so the worker can distinguish a user-initiated
# cancellation from a genuine pull failure (and skip the retry counter).
_pull_cancelled = threading.Event()


def _gave_up(model_id: str, href: Any) -> bool:
    """True when `model_id` has a terminal failure on this hardware."""
    with _gave_up_lock:
        return _gave_up_models.get(model_id) == repr(href)


def _mark_gave_up(model_id: str, href: Any) -> None:
    with _gave_up_lock:
        _gave_up_models[model_id] = repr(href)


def _check_hardware_change(href: Any) -> None:
    """If the hardware reference changed, clear give-up decisions (a model that
    failed on the old GPU may load fine on the new one)."""
    with _gave_up_lock:
        current = repr(href)
        # If any stored ref differs from current, hardware changed — clear all.
        if any(ref != current for ref in _gave_up_models.values()):
            _gave_up_models.clear()


def _clear_gave_up(model_id: str) -> None:
    with _gave_up_lock:
        _gave_up_models.pop(model_id, None)


def clear_gave_up() -> None:
    """Clear all give-up decisions (called when the user makes a new model choice)."""
    with _gave_up_lock:
        _gave_up_models.clear()


def cancel_active_pull() -> None:
    """Kill the in-flight `lemonade pull` so a model switch takes effect immediately.

    Safe to call when no pull is active (no-op). The worker thread will see the
    non-zero exit code, notice the cancel flag, and exit without counting the
    failure against the retry cap; the next ``ensure_chat_worker`` call (from
    the updated choice) starts a fresh pull for the new model.
    """
    global _pull_process
    _pull_cancelled.set()
    with _pull_lock:
        proc = _pull_process
        _pull_process = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            print("chat-ready: cancelled active model pull (user switched model)", flush=True)
        except OSError:
            pass


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
            "workflow": "Reloading the Large Language Model",
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
    if state == "awaiting-choice":
        return "Choose a model"
    if state in ("not-ready", "error"):
        return "Local chat unavailable"
    return "Preparing the Large Language Model"


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


def _user_choice_active(hardware: dict[str, Any], model_id: str) -> bool:
    """True when the user explicitly chose a model (family or exact id) that
    the current plan resolves to — such a pick must survive lemond restarts."""
    try:
        choice = catalog.read_model_choice()
    except Exception:  # noqa: BLE001
        return False
    family = str(choice.get("family") or "auto")
    model = choice.get("model_id")
    if isinstance(model, str) and model:
        return model == model_id
    if family and family != "auto":
        try:
            plan = plan_model(hardware)
        except Exception:  # noqa: BLE001
            return False
        selection = plan.get("selection") if isinstance(plan, dict) else None
        return bool(selection) and selection.get("id") == model_id
    return False


def _auto_plan_stale(hardware: dict[str, Any]) -> bool:
    """True when no user model choice is stored and the automatic plan now
    picks a different model than the one lemond has loaded. Used to re-plan
    after a hardware upgrade; user choices always win."""
    try:
        choice = catalog.read_model_choice()
    except Exception:  # noqa: BLE001
        return False
    if str(choice.get("family") or "auto") != "auto":
        return False
    if isinstance(choice.get("model_id"), str) and choice.get("model_id"):
        return False
    try:
        plan = plan_model(hardware)
    except Exception:  # noqa: BLE001
        return False
    selection = plan.get("selection") if isinstance(plan, dict) else None
    if not isinstance(selection, dict):
        return False
    current = read_chat_ready().get("model_id")
    planned = selection.get("id")
    return bool(current) and current != planned


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


def _run_lemonade_pull(model_id: str) -> subprocess.CompletedProcess[str]:
    """Run `lemonade pull` with the Popen tracked so a model switch can kill it."""
    global _pull_process
    key = lemonade_api_key()
    env = os.environ.copy()
    if key:
        env["LEMONADE_API_KEY"] = key
    proc = subprocess.Popen(
        ["lemonade", "pull", model_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    with _pull_lock:
        _pull_process = proc
    try:
        stdout, stderr = proc.communicate(timeout=3600)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise RuntimeError(f"lemonade pull timed out after 3600s")
    finally:
        with _pull_lock:
            if _pull_process is proc:
                _pull_process = None
    return subprocess.CompletedProcess(
        args=["lemonade", "pull", model_id],
        returncode=proc.returncode or 0,
        stdout=stdout,
        stderr=stderr,
    )


def _was_pull_cancelled() -> bool:
    """True when the pull was killed by cancel_active_pull (user switched model)."""
    return _pull_cancelled.is_set()


def _choice_changed(model_id: str) -> bool:
    """True when the persisted model choice no longer resolves to *model_id*."""
    try:
        choice = catalog.read_model_choice()
    except Exception:  # noqa: BLE001
        return False
    family = str(choice.get("family") or "auto")
    chosen_model = choice.get("model_id")
    if isinstance(chosen_model, str) and chosen_model:
        return chosen_model != model_id
    # Family-level choice: re-plan and see if we still land on the same model.
    try:
        hardware = collect_hardware()
        plan = plan_model(hardware)
    except Exception:  # noqa: BLE001
        return False
    selection = plan.get("selection") if isinstance(plan, dict) else None
    if not isinstance(selection, dict):
        return True  # can't confirm — treat as changed (safe: skip the load)
    return str(selection.get("id") or "") != model_id


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


def _model_is_downloaded(model_id: str) -> bool:
    """True when lemond reports the model's weights already on disk.

    ``GET /v1/models/{id}`` carries a ``downloaded`` flag, which is the cheap way
    to skip ``lemonade pull`` after a reboot. Pulling a cached model transfers no
    bytes but still re-hashes the whole file (~90 s for a 7 GB GGUF), which the
    portal reported as a download that was never happening. Fails closed: when
    lemond is unreachable we take the slow but certain path.
    """
    key = lemonade_api_key()
    if not key:
        return False
    try:
        req = Request(
            f"http://127.0.0.1:13305/v1/models/{quote(model_id, safe='')}",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urlopen(req, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False
    return isinstance(payload, dict) and bool(payload.get("downloaded"))


def _unload_other_models(model_id: str, health: Optional[dict[str, Any]] = None) -> None:
    """Unload any resident models other than `model_id`.

    lemond refuses to load a new pinned model while another pinned model is
    resident, so an intentional model switch must evict the old one first.
    """
    for name in sorted(_loaded_model_names(health)):
        if name == model_id:
            continue
        result = _run_lemonade(["unload", name], timeout=300)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[:300]
            print(f"chat-ready unload {name}: {detail or result.returncode}", flush=True)


def _record_verify_failure(model_id: str) -> int:
    """Count consecutive load/verify failures for `model_id`; reset others."""
    with _verify_failures_lock:
        if model_id in _verify_failures:
            _verify_failures[model_id] += 1
        else:
            _verify_failures.clear()
            _verify_failures[model_id] = 1
        return _verify_failures[model_id]


def _record_load_failure(model_id: str) -> int:
    """Count consecutive load failures (pull/load/health) for `model_id`."""
    with _verify_failures_lock:
        if model_id in _verify_failures:
            _verify_failures[model_id] += 1
        else:
            _verify_failures.clear()
            _verify_failures[model_id] = 1
        return _verify_failures[model_id]


def _record_verify_success(model_id: str) -> None:
    with _verify_failures_lock:
        _verify_failures.pop(model_id, None)


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
        href = _hardware_ref(hardware)
        # M6.5 picker-first gate: never download a model until the user has
        # made a choice (including an explicit "Automatic"). A fresh box has
        # no choice file — show the picker instead of auto-downloading the
        # default family.
        if not force and not catalog.choice_exists():
            payload = _progress_payload(
                state="awaiting-choice",
                progress=0,
                message="Choose a model to get started",
                workflow="Choose a model",
                task="Pick a model family in Settings to start the download.",
                model_id=None,
                display=None,
                reason=None,
                error=None,
                plan=None,
                href=href,
            )
            write_chat_ready(payload)
            return payload
        plan = plan_model(hardware)
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
        # A hardware change invalidates prior give-up decisions (a model that
        # failed on the old GPU may load fine on the new one).
        _check_hardware_change(href)
        # Terminal gate: a model that already failed deterministically on this
        # hardware is not re-loaded on every poll. Cleared when the user makes
        # a new model choice (stub.py) or the hardware reference changes.
        if not force and _gave_up(model_id, href):
            payload = _progress_payload(
                state="error",
                progress=0,
                message="Local chat failed to prepare",
                workflow="Local chat unavailable",
                task=f"{display} failed to load on this hardware. Try a different model family in Settings.",
                model_id=model_id,
                display=display,
                reason=plan.get("reason"),
                error=f"{display} previously failed to load; not retrying automatically",
                plan=plan,
                href=href,
            )
            write_chat_ready(payload)
            return payload
        # User-facing label: "Google Gemma" (maker + family name) instead of
        # the raw catalog id, with the model size appended when known.
        family_id = str(plan.get("family") or selection.get("family") or "")
        family_name = catalog.family_label(family_id)
        friendly = family_name or display
        weights_gb = selection.get("weights_gb")
        size_note = f" (~{float(weights_gb):g} GB)" if weights_gb else ""
        # A user-picked model (M6.5) is an intentional override — never let the
        # automatic planner re-plan it away after a lemond restart.
        user_chosen = _user_choice_active(hardware, model_id)
        workflow = "Downloading the Large Language Model"

        # File state alone is not enough — lemond forgets loads across restart.
        if not force and _model_is_loaded(model_id) and (user_chosen or not _auto_plan_stale(hardware)):
            ok, detail = _verify_completion(model_id)
            if ok:
                _record_verify_success(model_id)
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

        # Weights outlive lemond's load table, so consult the catalog before
        # announcing a download: a cached model only needs to be loaded.
        on_disk = _model_is_downloaded(model_id)
        if on_disk:
            workflow = "Preparing the Large Language Model"

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
            if on_disk:
                step_message = f"{friendly} is already on disk{size_note} — loading it…"
                step_task = f"Loading {model_id} onto the GPU"
            else:
                # Coarse milestones around `lemonade pull`. lemond exposes no
                # download-progress API (every candidate progress route 404s),
                # so we can only report the size; live rate belongs to the NET
                # throughput meter in the shell toolbar (DESIGN.md §11 backlog).
                step_message = f"Downloading {friendly} for your hardware{size_note}…"
                step_task = f"{model_id} ({float(weights_gb):g} GB)" if weights_gb else model_id
            write_chat_ready(
                _progress_payload(
                    state="preparing",
                    progress=20,
                    message=step_message,
                    workflow=workflow,
                    task=step_task,
                    model_id=model_id,
                    display=display,
                    reason=plan.get("reason"),
                    error=None,
                    plan=plan,
                    href=href,
                )
            )
            if not on_disk:
                # Guard against filling the user-data volume: verify there is room
                # for this download before starting it. Evicts LRU models to make
                # room (always keeping the most-recent prior model as a fallback);
                # raises RuntimeError with a user-facing message when the model
                # cannot fit while preserving that fallback.
                if weights_gb:
                    ensure_model_space(
                        MODELS_DIR,
                        needed_bytes=int(float(weights_gb) * (1024 ** 3)),
                        target_name=model_id,
                        client=get_client(),
                    )
                _pull_cancelled.clear()
                pull = _run_lemonade_pull(model_id)
                if pull.returncode != 0:
                    detail = (pull.stderr or pull.stdout or "").strip()[:500]
                    # A cancelled pull (user switched model) is not a real failure —
                    # don't count it against the retry cap; the new choice will
                    # spawn a fresh worker for the correct model.
                    if _was_pull_cancelled():
                        raise RuntimeError("pull cancelled (model switched)")
                    failures = _record_load_failure(model_id)
                    if failures >= _MAX_VERIFY_FAILURES:
                        _mark_gave_up(model_id, href)
                    raise RuntimeError(f"lemonade pull failed: {detail or pull.returncode}")
                # The user may have switched model while the pull was in flight and
                # it happened to finish before we could kill it. Re-check the
                # choice so we don't load a model the user no longer wants.
                if _choice_changed(model_id):
                    print(f"chat-ready: choice changed during pull; skipping load of {model_id}", flush=True)
                    raise RuntimeError("model choice changed during download")

            write_chat_ready(
                _progress_payload(
                    state="preparing",
                    progress=75,
                    message=f"Loading {friendly} onto the GPU…",
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
            # A model switch must evict the previously loaded (pinned) model
            # first — lemond refuses a new pinned load while one is resident.
            _unload_other_models(model_id)

            load_backend = select_backend(hardware)
            load_cmd = ["load", model_id, "--llamacpp", load_backend, "--pinned"]
            load = _run_lemonade(load_cmd, timeout=600)
            if load.returncode != 0 and on_disk:
                # The catalog says the weights are on disk but lemond refused to
                # load them (truncated or corrupted snapshot). One `pull` re-hashes
                # and re-fetches only what is missing; then retry the load once.
                print(f"chat-ready: cached {model_id} would not load; pulling to repair", flush=True)
                workflow = "Downloading the Large Language Model"
                write_chat_ready(
                    _progress_payload(
                        state="preparing",
                        progress=50,
                        message=f"Repairing the cached copy of {friendly}…",
                        workflow=workflow,
                        task=f"Repairing {model_id}",
                        model_id=model_id,
                        display=display,
                        reason=plan.get("reason"),
                        error=None,
                        plan=plan,
                        href=href,
                    )
                )
                repair = _run_lemonade_pull(model_id)
                if repair.returncode != 0:
                    detail = (repair.stderr or repair.stdout or "").strip()[:500]
                    raise RuntimeError(
                        f"lemonade repair pull failed: {detail or repair.returncode}"
                    )
                load = _run_lemonade(load_cmd, timeout=600)
            if load.returncode != 0:
                detail = (load.stderr or load.stdout or "").strip()[:500]
                failures = _record_load_failure(model_id)
                if failures >= _MAX_VERIFY_FAILURES:
                    _mark_gave_up(model_id, href)
                raise RuntimeError(f"lemonade load failed: {detail or load.returncode}")

            if not _model_is_loaded(model_id):
                failures = _record_load_failure(model_id)
                if failures >= _MAX_VERIFY_FAILURES:
                    _mark_gave_up(model_id, href)
                raise RuntimeError(
                    f"lemonade load returned success but {model_id} is not in /v1/health"
                )

            write_chat_ready(
                _progress_payload(
                    state="preparing",
                    progress=90,
                    message=f"Verifying {friendly}…",
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
                failures = _record_verify_failure(model_id)
                if failures >= _MAX_VERIFY_FAILURES:
                    # The model loads but cannot serve — e.g. a backend crash
                    # loop (llamacpp aborting after each inference). Stop
                    # retrying; the user can pick a different model.
                    _mark_gave_up(model_id, href)
                    raise RuntimeError(
                        f"{display} keeps failing to load on this hardware "
                        f"({detail}). Try a different model family in Settings."
                    )
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
            # A transient pull/load/verify failure is recoverable — the worker
            # may retry on the next poll. A terminal failure (cap reached) is
            # marked above; the gate at the top of prepare_local_chat() then
            # stops re-issuing loads until the user picks a different model or
            # the hardware changes.
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
            # A just-cancelled worker is about to exit (its pull was killed);
            # give it a moment to release the prepare lock, then replace it.
            if _pull_cancelled.is_set():
                deadline = time.monotonic() + 3.0
                while _worker.is_alive() and time.monotonic() < deadline:
                    time.sleep(0.1)
                if _worker.is_alive():
                    return  # old worker is stuck; don't double-start
            else:
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
