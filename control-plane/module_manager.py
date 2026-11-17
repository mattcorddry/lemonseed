"""Module lifecycle — install, enable, disable, remove, hooks, supervision
(M9 P2).

``modules_catalog.py`` says what a module is; this file says how the control
plane runs one. The lifecycle is the one the CP already knows how to operate:
pull → run → supervise on the data-services pattern, with the state machine
from MODULES.md §7 stored in the CP Postgres ``modules`` table.

Four rules this file exists to keep:

* **One operation at a time.** ``_op_lock`` is held for the whole window —
  install (which may pull a multi-hundred-MB image), enable, disable, remove —
  and the supervisor skips every tick while it is held, so a heal can never
  interleave with the operation the admin just asked for.
* **The container is disposable, the CP is the authority.** Containers run
  with ``--restart no`` and no blue/green: a new digest, changed settings or
  changed run args recreate the container (stop old → run new). The run
  fingerprint (``run_hash``) is how the supervision tick sees the drift
  without comparing process state.
* **No silent hook failures.** A non-zero hook exit fails the operation and
  the state records it — the AGENTS service-boundary rule applies to hooks.
* **A pull failure is a wait, never a verdict.** The registry is an external
  condition, not an appliance failure: a module whose bring-up is blocked on
  it goes WAITING and the supervision tick retries automatically, exactly the
  way the data plane and the Open WebUI module behave (§7 install step 6).
  BROKEN is reserved for what an admin must actually fix.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

import audit
import db
import image_pins
import modules_catalog
from data_services import (
    LEMON_NETWORK,
    POSTGRES_CONTAINER,
    VALKEY_CONTAINER,
    _docker,
    _ensure_lemon_network,
    _pull_image,
    _resolve_ref,
)

PENDING = "pending"
WAITING = "waiting"
INSTALLED = "installed"
ENABLED = "enabled"
BROKEN = "broken"

DATA_ROOT = Path("/var/lib/lemonseed/modules")
SUPERVISE_INTERVAL_SEC = 10
# Contiguous failing ticks before the supervisor stops trying and marks the
# module broken (the same heal budget Open WebUI's supervisor uses).
HEALTH_FAIL_LIMIT = 5
READY_SECONDS = "LEMONSEED_MODULE_READY_SECONDS"
DATA_PLANE_SECONDS = "LEMONSEED_MODULE_DATA_SECONDS"
# The destructive half of remove (Preserve data unchecked) is typed-confirmed,
# like the Open WebUI admin reset — a speed bump, not a secret.
PURGE_CONFIRM = "purge"

VERSION_FILE = Path("/run/appliance/version")


class ModuleError(RuntimeError):
    """An operation cannot proceed. Surfaced to the admin, never swallowed."""


def appliance_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# Catalog access
# ---------------------------------------------------------------------------


def _catalog() -> dict[str, Any]:
    return modules_catalog.load_catalog(appliance_version=appliance_version())


def _spec_for(module_id: str, loaded: Optional[dict[str, Any]] = None) -> Optional[dict[str, Any]]:
    loaded = loaded or _catalog()
    return (loaded.get("modules") or {}).get(module_id)


def _container_name(module_id: str) -> str:
    """Derivable from the id: the catalog validates docker.container against
    it, so teardown never needs a spec that may no longer exist."""
    return f"lemonseed-{module_id}"


def _module_volume(module_id: str) -> Path:
    return DATA_ROOT / module_id


def hook_timeout(spec: Optional[dict[str, Any]], module_id: str = "") -> int:
    """The per-module hook timeout, clamped to the catalog's range."""
    declared = ((spec or {}).get("hooks") or {}).get("script_timeout")
    timeout = int(declared) if isinstance(declared, int) else modules_catalog.HOOK_TIMEOUT_DEFAULT_SEC
    return max(
        modules_catalog.HOOK_TIMEOUT_MIN_SEC,
        min(modules_catalog.HOOK_TIMEOUT_MAX_SEC, timeout),
    )


# ---------------------------------------------------------------------------
# Container bring-up
# ---------------------------------------------------------------------------


def container_args(spec: dict[str, Any], env_path: Path, ref: str) -> list[str]:
    """The ``docker run`` argv for a module container.

    Fixed args first (the supervisor owns restart and the network), then the
    spec's declaration, then the reference. ``run_args`` are re-audited here
    even though the catalog build already rejected them: the runtime reader is
    what turns a spec into a process, and defense in depth on the daemon is
    cheap.
    """
    docker = spec["docker"]
    modules_catalog.audit_run_args(docker.get("run_args") or [], f"module {spec['id']!r}:")
    args = [
        "run",
        "-d",
        "--name",
        docker["container"],
        "--restart",
        "no",
        "--network",
        LEMON_NETWORK,
        "--add-host",
        "host.docker.internal:host-gateway",
    ]
    for port in docker.get("ports") or []:
        args += ["-p", port]
    for volume in docker.get("volumes") or []:
        args += ["-v", volume]
    args += ["--env-file", str(env_path)]
    args += list(docker.get("run_args") or [])
    args.append(ref)
    return args


def _container_running(name: str) -> bool:
    result = _docker(["inspect", "-f", "{{.State.Running}}", name], timeout=15)
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def _container_exists(name: str) -> bool:
    return _docker(["inspect", "-f", "{{.Name}}", name], timeout=15).returncode == 0


def probe_url(spec: dict[str, Any]) -> Optional[str]:
    probe = (spec.get("docker") or {}).get("probe")
    if not probe:
        return None
    return f"http://127.0.0.1:{probe['port']}{probe.get('path', '/')}"


def _probe_ok(spec: dict[str, Any], timeout: float = 2.0) -> bool:
    url = probe_url(spec)
    if url is None:
        return True  # a module with no probe is "running" once docker says so
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _wait_probe(spec: dict[str, Any]) -> bool:
    budget = int(os.environ.get(READY_SECONDS, "240") or "240")
    deadline = time.monotonic() + max(10, budget)
    while time.monotonic() < deadline:
        if _probe_ok(spec):
            return True
        time.sleep(2)
    return False


def _stop_container(name: str) -> None:
    _docker(["rm", "-f", name], timeout=60)


def _container_env_path(module_id: str) -> Path:
    """The derived docker ``--env-file``: the module env with host-scoped URLs
    rewritten to the lemon-network service names — the same rewrite Open
    WebUI's container gets (``modules._module_database_url``)."""
    from db import SECRETS_MODULES, _module_credentials

    rewritten: dict[str, str] = {}
    for key, value in _module_credentials(module_id).items():
        if key == "DATABASE_URL":
            for host in ("127.0.0.1", "localhost", "::1"):
                value = value.replace(f"@{host}:", f"@{POSTGRES_CONTAINER}:")
        elif key == "VALKEY_URL":
            for host in ("127.0.0.1", "localhost", "::1"):
                value = value.replace(f"{host}:", f"{VALKEY_CONTAINER}:")
        rewritten[key] = value
    path = SECRETS_MODULES / f"{module_id}.container.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    lines = [f"{key}={value}" for key, value in rewritten.items()]
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o640)
    tmp.replace(path)
    return path


def run_fingerprint(spec: dict[str, Any], env_path: Path) -> str:
    """The identity of the container we intend to run.

    The pin's digest (not whatever reference is locally runnable — the pin is
    the authority an appliance update moves, and the fingerprint must move
    with it), the full run argv, and the exact env bytes. A drift in any of
    the three is a recreate on the next supervision tick, which is how "new
    upstream digest, changed settings, changed run args" (§7) happens without
    the supervisor comparing process state.
    """
    payload = json.dumps(
        {
            "image": image_pins.digest_ref(spec["docker"]["image"]),
            "args": container_args(spec, env_path, image_pins.digest_ref(spec["docker"]["image"])),
            "env": env_path.read_text(encoding="utf-8") if env_path.is_file() else "",
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _run_container(spec: dict[str, Any], env_path: Path) -> tuple[bool, str]:
    name = spec["docker"]["container"]
    _stop_container(name)
    if not _ensure_lemon_network():
        return False, "Docker network 'lemon' unavailable"
    args = container_args(spec, env_path, _resolve_ref(spec["docker"]["image"]))
    result = _docker(args, timeout=180)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "docker run failed").strip()
        # Retry once after a forced remove (a race can leave a named container).
        if "already in use" in err.lower():
            _stop_container(name)
            result = _docker(args, timeout=180)
            if result.returncode != 0:
                return False, (result.stderr or result.stdout or err).strip()
            return True, name
        return False, err
    return True, name


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def run_hook(
    module_id: str,
    event: str,
    spec: Optional[dict[str, Any]] = None,
    payload: Optional[dict[str, Any]] = None,
) -> tuple[bool, str]:
    """Run one host-side hook (§7): root, from the image path, with the
    module env file's contents in the environment and the JSON event payload
    on stdin. A module ships only the hooks it wants, so a missing script is
    not a failure.
    """
    script = modules_catalog.module_root() / module_id / "hooks" / f"{event}.sh"
    if not script.is_file():
        return True, ""
    from db import _module_credentials

    env = os.environ.copy()
    env.update(_module_credentials(module_id))
    env["LEMONSEED_MODULE_ID"] = module_id
    env["LEMONSEED_MODULE_DIR"] = str(_module_volume(module_id))
    env["LEMONSEED_EVENT"] = event
    timeout = hook_timeout(spec, module_id)
    stdin = json.dumps(payload) + "\n" if payload is not None else ""
    try:
        done = subprocess.run(
            ["/bin/sh", str(script)],
            env=env,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    except OSError as exc:
        return False, str(exc)
    if done.returncode != 0:
        tail = ((done.stderr or "") + "\n" + (done.stdout or "")).strip()[-400:]
        return False, f"exit {done.returncode}: {tail}"
    return True, (done.stdout or "").strip()


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

_op_lock = threading.Lock()
_op_progress: Optional[dict[str, str]] = None
_op_progress_lock = threading.Lock()
_health_fail_count: dict[str, int] = {}
_health_fail_error: dict[str, str] = {}
_sup_worker: Optional[threading.Thread] = None


def _op_state() -> Optional[dict[str, str]]:
    with _op_progress_lock:
        return dict(_op_progress) if _op_progress else None


def _set_op_progress(module_id: str, action: str) -> None:
    with _op_progress_lock:
        global _op_progress
        _op_progress = {"id": module_id, "action": action, "at": _iso_now()}


def _clear_op_progress() -> None:
    with _op_progress_lock:
        global _op_progress
        _op_progress = None


def _start_op(
    action: str,
    module_id: str,
    body: Callable[[], dict[str, Any]],
) -> dict[str, Any]:
    """Start an async module operation: acquire the op lock, mark pending,
    then hand the real work to a daemon thread that releases the lock.

    The lock is acquired here, not in the thread, so the window between "the
    admin asked" and "the worker started" cannot be interleaved by a
    supervision tick that assumed nothing was happening.
    """
    if not _op_lock.acquire(blocking=False):
        return {"accepted": False, "reason": "another module operation is in progress"}
    _set_op_progress(module_id, action)
    db.set_module_state(module_id, PENDING, error=None)

    def worker() -> None:
        try:
            body()
        except Exception as exc:  # noqa: BLE001 — the row records it, the portal shows it
            detail = str(exc)[:500]
            db.set_module_state(module_id, BROKEN, error=detail)
            print(f"module {action}: {module_id}: {detail}", flush=True)
        finally:
            _clear_op_progress()
            _op_lock.release()

    try:
        threading.Thread(
            target=worker, name=f"lemonseed-module-{action}", daemon=True
        ).start()
    except OSError as exc:
        _clear_op_progress()
        _op_lock.release()
        return {"accepted": False, "reason": f"could not start the operation: {exc}"}
    return {"accepted": True, "id": module_id, "action": action}


def _install_order(catalog_modules: dict[str, Any], module_id: str) -> list[str]:
    """The target plus its transitive module dependencies, topologically.

    Core services are provisioned before any module by the caller, so they
    are not nodes here (same rule as ``modules_catalog.dependency_order``).
    """
    wanted: dict[str, set[str]] = {}

    def collect(current: str) -> None:
        if current in wanted:
            return
        spec = catalog_modules.get(current)
        if spec is None:
            raise ModuleError(
                f"module {current!r} is in the dependency graph but not the catalog"
            )
        deps = set((spec.get("dependencies") or {}).get("modules") or [])
        deps.discard(current)
        wanted[current] = deps
        for dep in sorted(deps):
            collect(dep)

    collect(module_id)
    order: list[str] = []
    ready = sorted(mid for mid, deps in wanted.items() if not deps)
    while ready:
        mid = ready.pop(0)
        order.append(mid)
        for other, deps in sorted(wanted.items()):
            if mid in deps:
                deps.discard(mid)
                if not deps and other not in order:
                    ready.append(other)
        ready.sort()
    if len(order) != len(wanted):
        stuck = sorted(set(wanted) - set(order))
        raise ModuleError(
            "module dependency cycle: " + " -> ".join(stuck + [stuck[0]])
        )
    return order


def _declares_litellm(spec: Optional[dict[str, Any]]) -> bool:
    deps = ((spec or {}).get("dependencies") or {}).get("core") or []
    return "litellm" in deps


def _wait_data_plane(
    specs: Optional[list[dict[str, Any]]] = None,
) -> tuple[bool, str]:
    """Core services first (§7 install step 2): Postgres/Valkey must be
    serving before DB provisioning. LiteLLM is a gate only when the chain
    being installed declares it (``dependencies.core: litellm``) — the proxy
    only starts once a model is chosen, so an appliance that has not picked a
    model yet is a normal state, not a failure, and a chat-dependent chain
    waits for it the same way it waits for the registry. lemond is accepted as
    present — the engine is systemd-managed and the model load is the chat
    worker's concern, not a module install's.

    Returns (``True, ""``) or (``False, reason``): a wait is a wait, never a
    verdict — the row records the reason and the supervision tick retries."""
    from data_services import ensure_provision_worker, ensure_provisioned

    ensure_provision_worker()
    budget = int(os.environ.get(DATA_PLANE_SECONDS, "300") or "300")
    deadline = time.monotonic() + max(10, budget)
    while time.monotonic() < deadline:
        if ensure_provisioned().get("state") == "ready":
            break
        time.sleep(2)
    else:
        return False, "the data plane did not become ready"
    if not any(_declares_litellm(s) for s in (specs or [])):
        return True, ""
    try:
        import litellm  # noqa: PLC0415 — lazy: litellm supervises through data_services
    except ImportError:
        return True, ""
    # Its own budget: a chat-dependent chain should not inherit whatever was
    # left of the Postgres/Valkey window.
    deadline = time.monotonic() + max(10, budget)
    while time.monotonic() < deadline:
        if litellm.litellm_ready():
            return True, ""
        time.sleep(2)
    return False, "the LiteLLM proxy is not ready"


def _pull_step(
    module_id: str, spec: dict[str, Any], *, keep_state: Optional[str] = None
) -> str:
    """Pull the pinned container image (§7 install step 6, data-services
    semantics): the digest if present, the tag fallback when a digest stops
    resolving. Returns ``"ok"`` or ``"wait"``.

    A pull failure is a wait, never a verdict: the registry is an external
    condition, so the row records the reason and the supervision tick retries
    the bring-up automatically — exactly the way the data plane and the Open
    WebUI module behave. ``keep_state`` is the row state to keep when the
    module is still serving or merely parked (an enabled module whose recreate
    is pending): the recorded error note is what the admin sees. Without it
    the row moves to WAITING — the bring-up is blocked, the tick retries.
    """
    ok, detail = _pull_image(spec["docker"]["image"], spec.get("name") or module_id)
    if ok:
        return "ok"
    db.set_module_state(module_id, keep_state if keep_state else WAITING, error=detail)
    return "wait"


def _record_enabled(module_id: str, spec: dict[str, Any], env_path: Path) -> None:
    """Idempotent success write: an already-enabled row just gets its run
    fingerprint re-asserted, so a heal never invents state."""
    db.set_module_state(
        module_id,
        ENABLED,
        version=spec["version"],
        error=None,
        run_hash=run_fingerprint(spec, env_path),
    )


def _bring_up_fresh(
    module_id: str,
    spec: dict[str, Any],
    env_path: Path,
    *,
    keep_state: Optional[str] = None,
) -> tuple[str, str]:
    """Pull, run fresh, wait for the health probe, run the start hook, record
    the run fingerprint and ENABLED (§7 install steps 6-7).

    Returns ``(status, detail)`` with status ``"ok"`` (the module is enabled),
    ``"wait"`` (a pull is blocked on the registry — the row records WAITING,
    or keeps ``keep_state`` with the reason, and the supervision tick retries)
    or ``"fail"`` with the recorded error (the caller decides the state; the
    operation path raises, the supervision path counts it against the heal
    budget).
    """
    if not _ensure_lemon_network():
        return "fail", "Docker network 'lemon' unavailable"
    if _pull_step(module_id, spec, keep_state=keep_state) != "ok":
        return "wait", "waiting for the container registry"
    ok, detail = _run_container(spec, env_path)
    if not ok:
        return "fail", f"could not start the module container: {detail}"
    if not _wait_probe(spec):
        return "fail", "the module's health probe did not pass"
    ok, detail = run_hook(module_id, "start", spec)
    if not ok:
        return "fail", f"hooks/start.sh failed: {detail}"
    _record_enabled(module_id, spec, env_path)
    return "ok", ""


def _install_one(module_id: str, spec: dict[str, Any], *, actor: str) -> str:
    """Install one module (§7 install steps 3-7): provision its data plane,
    materialize settings, run the install hook, bring the container up.

    Dependencies are provisioned before a module is (§7 install step 2), so
    they are installed by the caller's topological order, not here. Returns
    ``"ok"`` or ``"wait"``; a real failure raises (the operation records
    BROKEN).
    """
    volume = _module_volume(module_id)
    volume.mkdir(parents=True, exist_ok=True)
    if (spec.get("data") or {}).get("postgres"):
        db.provision_module_db(module_id, spec["data"]["postgres"]["db_name"])
    valkey = (spec.get("data") or {}).get("valkey")
    if valkey:
        db.write_module_env(
            module_id,
            {
                "VALKEY_URL": f"redis://127.0.0.1:6379/{valkey['db']}",
                "VALKEY_DB": str(valkey["db"]),
            },
        )
    _apply_settings_env(module_id, spec)
    ok, detail = run_hook(module_id, "install", spec)
    if not ok:
        raise ModuleError(f"hooks/install.sh failed: {detail}")
    status, detail = _bring_up_fresh(module_id, spec, _container_env_path(module_id))
    if status == "ok":
        audit.record(
            actor or audit.ACTOR_CP,
            module_id,
            "module.install",
            {"version": spec["version"]},
        )
        print(f"module install: {module_id} enabled", flush=True)
        return "ok"
    if status == "wait":
        return "wait"
    raise ModuleError(detail)


def _apply_settings_env(module_id: str, spec: dict[str, Any]) -> None:
    """Materialize declared settings into the env file (§7 install step 4):
    user-set values from the row win over declared defaults."""
    declared = {entry["name"]: entry for entry in spec.get("settings") or []}
    if not declared:
        return
    row = db.module_row(module_id) or {}
    user = dict(row.get("settings") or {})
    updates: dict[str, str] = {}
    for name, entry in declared.items():
        value = user[name] if name in user else entry.get("default")
        if value is None:
            continue
        updates[name] = _setting_env_value(entry, value)
    if updates:
        db.write_module_env(module_id, updates)


def _setting_env_value(entry: dict[str, Any], value: Any) -> str:
    kind = entry["type"]
    if kind == "bool":
        return "True" if value in (True, "True", "true", 1) else "False"
    if kind == "int":
        return str(int(value))
    return str(value)


def _dependents(module_id: str, rows: Optional[list[dict[str, Any]]] = None) -> list[str]:
    """Installed-or-worse modules that declare this one (§7 remove)."""
    states = {row["id"]: row["state"] for row in (rows if rows is not None else db.module_rows())}
    live = (PENDING, INSTALLED, ENABLED, BROKEN)
    loaded = _catalog()
    out: list[str] = []
    for other_id, spec in loaded["modules"].items():
        if other_id == module_id:
            continue
        deps = (spec.get("dependencies") or {}).get("modules") or []
        if module_id in deps and states.get(other_id) in live:
            out.append(other_id)
    return sorted(out)


def _refuse(reason: str) -> dict[str, Any]:
    return {"accepted": False, "reason": reason}


def request_install(module_id: str, *, actor: str = "") -> dict[str, Any]:
    """Async install (§7): validate cheaply, then 202 and walk away."""
    loaded = _catalog()
    spec = loaded["modules"].get(module_id)
    if spec is None:
        problem = next((p for p in loaded["problems"] if f"{module_id!r}" in p), None)
        return _refuse(problem or "not in the module catalog")
    reason = modules_catalog.compat_reason(spec, appliance_version())
    if reason:
        return _refuse(f"cannot install: {reason}")
    row = db.module_row(module_id)
    if row and row["state"] in (ENABLED, PENDING):
        return _refuse("already installed")
    try:
        _install_order(loaded["modules"], module_id)  # cycles refuse here, loudly
    except ModuleError as exc:
        return _refuse(str(exc))
    return _start_op("install", module_id, lambda: _do_install(module_id, actor=actor))


def _do_install(module_id: str, *, actor: str = "") -> dict[str, Any]:
    """Install the target and any dependencies it declares (§7 install steps
    2-7, topologically). A dependency that is not installed at all is
    installed first; one that exists but is disabled or broken is left alone —
    the design installs dependencies that were never installed, and the
    admin's own enable/disable decisions are not overridden."""
    loaded = _catalog()
    spec = loaded["modules"].get(module_id)
    if spec is None:
        raise ModuleError("module is no longer in the catalog")
    order = _install_order(loaded["modules"], module_id)
    ok, reason = _wait_data_plane(
        [loaded["modules"][dep] for dep in order[:-1]] + [spec]
    )
    if not ok:
        # The core services (or a chat path the chain declares) are not up
        # yet: a wait, never a verdict — the tick retries the whole bring-up.
        db.set_module_state(module_id, WAITING, error=reason)
        return {"state": "wait", "id": module_id}
    for dep in order[:-1]:
        if not db.module_row(dep) and _install_one(
            dep, loaded["modules"][dep], actor=actor
        ) == "wait":
            # The chain stalled on the registry: the target joins the wait
            # so the supervision tick retries the whole bring-up.
            dep_row = db.module_row(dep) or {}
            db.set_module_state(
                module_id, WAITING, error=dep_row.get("error") or f"waiting for {dep}"
            )
            return {"state": "wait", "id": module_id}
    status = _install_one(module_id, spec, actor=actor)
    if status == "wait":
        return {"state": "wait", "id": module_id}
    return {"state": "enabled", "id": module_id}


def request_enable(module_id: str, *, actor: str = "") -> dict[str, Any]:
    row = db.module_row(module_id)
    if not row:
        return _refuse("not installed")
    if row["state"] == ENABLED:
        return _refuse("already enabled")
    if not _spec_for(module_id):
        return _refuse("module is no longer in the catalog")
    return _start_op("enable", module_id, lambda: _do_enable(module_id, actor=actor))


def _do_enable(module_id: str, *, actor: str = "") -> dict[str, Any]:
    """Re-run the container with the module's current settings (§7): the env
    file is rewritten from the row, then pull → run → probe → start hook.

    While a pull is blocked on the registry the module keeps whatever it was:
    an enabled module whose recreate is pending keeps serving the old image
    with the reason recorded, a stopped one waits."""
    spec = _spec_for(module_id)
    if spec is None:
        raise ModuleError("module is no longer in the catalog")
    ok, reason = _wait_data_plane([spec])
    if not ok:
        # Same rule as install: the core services (or the chat path this
        # module declares) are not up yet — a wait, never a verdict.
        db.set_module_state(module_id, WAITING, error=reason)
        return {"state": "wait", "id": module_id}
    _apply_settings_env(module_id, spec)
    was_running = _container_running(_container_name(module_id))
    status, detail = _bring_up_fresh(
        module_id,
        spec,
        _container_env_path(module_id),
        keep_state=ENABLED if was_running else None,
    )
    if status == "ok":
        audit.record(actor or audit.ACTOR_CP, module_id, "module.enable", {})
        return {"state": "enabled", "id": module_id}
    if status == "wait":
        return {"state": "wait", "id": module_id}
    raise ModuleError(detail)


def request_disable(module_id: str, *, actor: str = "") -> dict[str, Any]:
    row = db.module_row(module_id)
    if not row:
        return _refuse("not installed")
    if row["state"] not in (ENABLED, WAITING):
        return _refuse("not enabled")
    return _start_op("disable", module_id, lambda: _do_disable(module_id, actor=actor))


def _do_disable(module_id: str, *, actor: str = "") -> dict[str, Any]:
    """Stop + remove the container, then the stop hook (§7). The hook runs
    after the teardown, by the design's own order: down, then cleanup.

    Also parks a WAITING module (a bring-up blocked on the registry): the
    supervision tick stops retrying, and the next enable takes over."""
    spec = _spec_for(module_id)
    _stop_container(_container_name(module_id))
    if spec is not None:
        ok, detail = run_hook(module_id, "stop", spec)
        if not ok:
            # The container is already down — a failed hook must not leave the
            # state claiming enabled. Record it, finish the disable, and let
            # the next enable retry the hook.
            db.set_module_state(module_id, INSTALLED, error=f"hooks/stop.sh failed: {detail}")
            audit.record(actor or audit.ACTOR_CP, module_id, "module.disable", {"hook_error": detail})
            print(f"module disable: {module_id}: hooks/stop.sh failed: {detail}", flush=True)
            return
    db.set_module_state(module_id, INSTALLED, error=None)
    audit.record(actor or audit.ACTOR_CP, module_id, "module.disable", {})


def request_remove(
    module_id: str, *, preserve: bool, confirm: str = "", actor: str = ""
) -> dict[str, Any]:
    """Async remove (§7): the Preserve data checkbox decides the data's fate.

    ``preserve=True`` keeps the data volume and the Postgres database, so a
    reinstall is lossless. ``preserve=False`` purges both — the destructive
    path, typed-confirmed like the Open WebUI admin reset.
    """
    row = db.module_row(module_id)
    if not row:
        return _refuse("not installed")
    if row["state"] == PENDING:
        return _refuse("another operation is in progress")
    dependents = _dependents(module_id)
    if dependents:
        return _refuse(
            "installed module(s) depend on this one: " + ", ".join(dependents)
        )
    if not preserve and (
        not isinstance(confirm, str) or confirm.strip().lower() != PURGE_CONFIRM
    ):
        return _refuse(
            f"typing {PURGE_CONFIRM!r} confirms deleting this module's data"
        )
    return _start_op(
        "remove",
        module_id,
        lambda: _do_remove(module_id, preserve=preserve, actor=actor),
    )


def _do_remove(module_id: str, *, preserve: bool, actor: str = "") -> dict[str, Any]:
    spec = _spec_for(module_id)
    _stop_container(_container_name(module_id))
    if spec is not None:
        ok, detail = run_hook(module_id, "remove", spec)
        if not ok:
            # The design's failure semantics: remove → installed with the
            # error and a retry. The container is down either way.
            db.set_module_state(
                module_id, INSTALLED, error=f"hooks/remove.sh failed: {detail}"
            )
            audit.record(
                actor or audit.ACTOR_CP,
                module_id,
                "module.remove",
                {"preserve": preserve, "hook_error": detail},
            )
            return
    if not preserve:
        try:
            db.drop_module_db(module_id)
        except Exception as exc:  # noqa: BLE001 — the purge reports, never lies
            db.set_module_state(module_id, BROKEN, error=f"could not drop the database: {exc}")
            raise
        try:
            volume = _module_volume(module_id)
            if volume.exists():
                shutil.rmtree(volume)
        except OSError as exc:
            db.set_module_state(module_id, BROKEN, error=f"could not delete the data volume: {exc}")
            raise
        try:
            from db import SECRETS_MODULES

            (SECRETS_MODULES / f"{module_id}.env").unlink(missing_ok=True)
            (SECRETS_MODULES / f"{module_id}.container.env").unlink(missing_ok=True)
        except OSError as exc:
            print(f"module remove: {module_id}: env cleanup failed: {exc}", flush=True)
    db.drop_module_row(module_id)
    audit.record(
        actor or audit.ACTOR_CP,
        module_id,
        "module.remove",
        {"preserve": bool(preserve)},
    )
    print(f"module remove: {module_id} removed (preserve={bool(preserve)})", flush=True)


def save_settings(
    module_id: str, values: dict[str, Any], *, actor: str = ""
) -> dict[str, Any]:
    """Sync settings write (§8): validated against the declared schema, stored
    on the row, merged into the env file. The env change is what recreates
    the container on the next supervision tick."""
    spec = _spec_for(module_id)
    if spec is None:
        return {"state": "error", "error": "module is no longer in the catalog"}
    row = db.module_row(module_id)
    if not row or row["state"] not in (INSTALLED, ENABLED, BROKEN, WAITING):
        return {"state": "error", "error": "not installed"}
    declared = {entry["name"]: entry for entry in spec.get("settings") or []}
    merged = dict(row.get("settings") or {})
    updates: dict[str, str] = {}
    for name, value in (values or {}).items():
        entry = declared.get(name)
        if entry is None:
            return {"state": "error", "error": f"unknown setting {name!r}"}
        _validate_setting_value(entry, value)
        merged[name] = value
        updates[name] = _setting_env_value(entry, value)
    if updates:
        db.write_module_env(module_id, updates)
    db.set_module_state(module_id, row["state"], settings=merged)
    audit.record(
        actor or audit.ACTOR_CP,
        module_id,
        "module.settings",
        {"fields": sorted(values or {})},
    )
    return {"state": "ready", "settings": merged}


def _validate_setting_value(entry: dict[str, Any], value: Any) -> None:
    kind = entry["type"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise ModuleError(f"setting {entry['name']} must be a boolean")
    elif kind == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ModuleError(f"setting {entry['name']} must be an integer")
    elif kind == "enum":
        if value not in (entry.get("choices") or []):
            raise ModuleError(f"setting {entry['name']} must be one of its choices")
    elif not isinstance(value, str):
        raise ModuleError(f"setting {entry['name']} must be a string")


# ---------------------------------------------------------------------------
# Platform contributions (§6) and supervision
# ---------------------------------------------------------------------------


def litellm_contributions() -> list[dict[str, str]]:
    """Enabled plugin contributions, for ``litellm.render_config`` (§6).

    A module that is installed but disabled is excluded, so disabling a module
    really does remove its endpoint from the chat path. A catalog that cannot
    load contributes nothing — the same degradation a broken litellm module
    gets, never a hard failure of the chat path.
    """
    try:
        loaded = _catalog()
        rows = {row["id"]: row["state"] for row in db.module_rows()}
    except Exception as exc:  # noqa: BLE001 — chat path must not hard-fail
        print(f"module contributions: unavailable: {exc}", flush=True)
        return []
    out: list[dict[str, str]] = []
    for module_id, spec in sorted(loaded["modules"].items()):
        if rows.get(module_id) != ENABLED or spec.get("type") != "plugin":
            continue
        server = (
            ((spec.get("platform") or {}).get("litellm") or {})
            .get("mcp_server")
        )
        if server:
            out.append({"name": server["name"], "url": server["url"]})
    return out


def supervise_once() -> None:
    """One supervision tick: keep every enabled module's container running
    under its current run fingerprint, and retry bring-ups that are waiting
    on the registry."""
    if _op_lock.locked():
        return  # an operation owns the module right now
    for row in db.module_rows():
        state = row["state"]
        if state not in (WAITING, ENABLED):
            # BROKEN is surfaced with the error and a retry, not auto-healed;
            # PENDING and INSTALLED belong to an operation or to the admin.
            continue
        try:
            if state == WAITING:
                _retry_waiting(row)
            else:
                _supervise_row(row)
        except Exception as exc:  # noqa: BLE001 — never kill the supervisor
            print(f"module supervise: {row['id']}: {exc}", flush=True)


def _retry_waiting(row: dict[str, Any]) -> None:
    """A WAITING row is a bring-up blocked on something external (§7 install
    step 6's data-services semantics): the registry, or the core services a
    chat-dependent chain needs. The tick re-runs the full install flow,
    skipping dependencies that are already installed; a wait keeps the row
    waiting, and a real bring-up failure (a hook exit, a run failure, a
    health timeout) marks the module broken."""
    module_id = row["id"]
    try:
        _do_install(module_id)
    except Exception as exc:  # noqa: BLE001 — the row records it
        db.set_module_state(module_id, BROKEN, error=str(exc)[:500])


def _supervise_row(row: dict[str, Any]) -> None:
    module_id = row["id"]
    spec = _spec_for(module_id)
    if spec is None:
        db.set_module_state(
            module_id, BROKEN, error="module is no longer in the catalog"
        )
        _clear_health_fail(module_id)
        return
    name = _container_name(module_id)
    env_path = _container_env_path(module_id)
    fingerprint = run_fingerprint(spec, env_path)
    drifted = bool(row.get("run_hash")) and row["run_hash"] != fingerprint

    if not _container_running(name):
        if drifted:
            # The env/image already moved: reviving the old container would
            # only serve stale settings before another recreate.
            print(
                f"module supervise: {module_id}: run fingerprint changed; recreating",
                flush=True,
            )
            _recreate(module_id, spec, env_path)
            return
        # A stopped container is a normal heal, not a failure: bring it back
        # up (and wait for its probe) before counting a health failure
        # against it.
        status, detail = _bring_up(module_id, spec, env_path)
        if status == "ok":
            _clear_health_fail(module_id)
        elif status == "wait":
            pass  # the tick retries; the health budget is not spent on the registry
        else:
            _count_health_failure(module_id, detail)
        return

    if _probe_ok(spec):
        _clear_health_fail(module_id)
        if drifted:
            print(
                f"module supervise: {module_id}: run fingerprint changed; recreating",
                flush=True,
            )
            _recreate(module_id, spec, env_path)
        return

    _count_health_failure(module_id)


def _clear_health_fail(module_id: str) -> None:
    """Forget a module's accumulated health failures (a heal or a reset)."""
    _health_fail_count.pop(module_id, None)
    _health_fail_error.pop(module_id, None)


def _count_health_failure(module_id: str, error: Optional[str] = None) -> None:
    """Contiguous failing ticks before the supervisor stops trying and marks
    the module broken — the same heal budget Open WebUI's supervisor uses."""
    _health_fail_count[module_id] = _health_fail_count.get(module_id, 0) + 1
    if error:
        _health_fail_error[module_id] = error
    if _health_fail_count[module_id] < HEALTH_FAIL_LIMIT:
        return
    detail = _health_fail_error.get(module_id, "")
    _clear_health_fail(module_id)
    db.set_module_state(
        module_id,
        BROKEN,
        error=detail or "the module kept failing its health checks",
    )
    print(f"module supervise: {module_id}: marked broken", flush=True)


def _recreate(module_id: str, spec: dict[str, Any], env_path: Path) -> None:
    """Stop the old container and run a fresh one — how a new digest, changed
    settings or changed run args take effect (§7).

    The pull runs before the teardown, so an enabled module whose recreate is
    pending keeps serving the old image while the registry is unreachable."""
    if _pull_step(module_id, spec, keep_state=ENABLED) != "ok":
        return  # the row keeps ENABLED with the reason; the tick retries
    ok, detail = _run_container(spec, env_path)
    if not ok:
        db.set_module_state(module_id, BROKEN, error=f"could not recreate: {detail}")
        return
    if not _wait_probe(spec):
        db.set_module_state(
            module_id, BROKEN, error="the module's health probe did not pass"
        )
        return
    run_hook(module_id, "start", spec)  # best effort on a recreate: recorded, not fatal
    _record_enabled(module_id, spec, env_path)


def _bring_up(module_id: str, spec: dict[str, Any], env_path: Path) -> tuple[str, str]:
    """Supervised revive of a stopped container: reuse the existing one (the
    data-services pattern) when it is there to start, else pull and run
    fresh. The start hook runs on every bring-up, including a revive."""
    name = _container_name(module_id)
    if _container_exists(name) and _docker(["start", name], timeout=120).returncode == 0:
        run_hook(module_id, "start", spec)
        if not _wait_probe(spec):
            return "fail", "the module's health probe did not pass"
        _record_enabled(module_id, spec, env_path)
        return "ok", ""
    return _bring_up_fresh(module_id, spec, env_path, keep_state=ENABLED)


def ensure_worker() -> None:
    """Long-lived supervisor for installed modules, plus the user-CRUD
    observer registration (§7) — registered before the thread starts, so an
    account created in the CP's first seconds cannot miss a hook."""
    global _sup_worker
    _register_observer_once()
    if _sup_worker is not None and _sup_worker.is_alive():
        return

    def _run() -> None:
        while True:
            try:
                supervise_once()
            except Exception as exc:  # noqa: BLE001 — never kill the supervisor
                print(f"module supervise: {exc}", flush=True)
            time.sleep(SUPERVISE_INTERVAL_SEC)

    _sup_worker = threading.Thread(target=_run, name="lemonseed-modules", daemon=True)
    _sup_worker.start()


_OBSERVER_REGISTERED = False
_OBSERVER_LOCK = threading.Lock()


def _register_observer_once() -> None:
    global _OBSERVER_REGISTERED
    if _OBSERVER_REGISTERED:
        return
    with _OBSERVER_LOCK:
        if _OBSERVER_REGISTERED:
            return
        try:
            import usermgmt  # noqa: PLC0415 — lazy: usermgmt imports the OWUI module

            usermgmt.register_observer(_user_event_observer)
            _OBSERVER_REGISTERED = True
        except Exception as exc:  # noqa: BLE001 — the next tick retries
            print(f"module hooks: observer registration failed: {exc}", flush=True)


def _user_event_observer(event: str, payload: dict[str, Any]) -> None:
    """Run every enabled module's user.sh for an account event (§7)."""
    for row in db.module_rows():
        if row["state"] != ENABLED:
            continue
        try:
            run_hook(row["id"], "user", None, payload={"event": event, **(payload or {})})
        except Exception as exc:  # noqa: BLE001 — observer boundary
            print(f"module hooks: {row['id']} user event: {exc}", flush=True)


# ---------------------------------------------------------------------------
# Portal view
# ---------------------------------------------------------------------------


def _docker_status(container: str) -> Optional[tuple[str, str]]:
    import modules  # noqa: PLC0415 — lazy: modules.py owns Open WebUI, not us

    return modules.owui_container_status(container)


def _registration_state(server: dict[str, str]) -> tuple[bool, Optional[str]]:
    """Is this contribution in the rendered config LiteLLM actually runs?

    Ground truth for the portal card's ``chat_registered`` — not the intent
    (a module row), the file the proxy was last handed. With no model chosen
    yet nothing is rendered at all: that is reported as False with a note,
    not an error, because there is no chat path to register into until the
    first model exists.
    """
    try:
        from litellm import CONFIG_PATH  # noqa: PLC0415 — service boundary

        text = CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (
            False,
            "no rendered chat configuration yet — pick a model and it will "
            "register with the first render",
        )
    except Exception:  # noqa: BLE001 — view helper must not fail the view
        return False, "the chat configuration could not be read"
    if f"url: {server.get('url', '')}" in text:
        return True, None
    return False, "the rendered configuration does not carry this contribution"


def _module_card(
    module_id: str,
    spec: Optional[dict[str, Any]],
    row: Optional[dict[str, Any]],
) -> dict[str, Any]:
    docker_status = None
    if row is not None and row["state"] in (ENABLED, BROKEN):
        docker_status = _docker_status(_container_name(module_id))
    registered, note = (None, None)
    if (card_server := (((spec or {}).get("platform") or {}).get("litellm") or {}).get("mcp_server")):
        registered, note = _registration_state(card_server)
    card: dict[str, Any] = {
        "id": module_id,
        "name": (spec or {}).get("name") or module_id,
        "type": (spec or {}).get("type"),
        "version": (spec or {}).get("version") or (row or {}).get("version") or "",
        "maturity": (spec or {}).get("maturity"),
        "license": (spec or {}).get("license"),
        "owners": (spec or {}).get("owners") or [],
        "homepage": (spec or {}).get("homepage"),
        "description": (spec or {}).get("description") or "",
        "installed": row is not None,
        "state": (row or {}).get("state"),
        "error": (row or {}).get("error"),
        "docker_status": docker_status[0] if docker_status else None,
        "settings": (spec or {}).get("settings") or [],
        "values": {},
        "logo": _logo_url(module_id, spec),
        # The platform contribution the portal should tell the admin about:
        # an MCP tool server that rides the chat path while the module is
        # enabled (§6). ``chat_registered`` is the same fact checked against
        # the rendered config file — true only once the proxy actually has it.
        "mcp_server": (((spec or {}).get("platform") or {}).get("litellm") or {})
        .get("mcp_server"),
        "chat_registered": registered,
        "chat_registration_note": note,
    }
    if spec is None:
        card["spec_missing"] = True
    else:
        declared = {entry["name"]: entry for entry in spec.get("settings") or []}
        user = dict((row or {}).get("settings") or {})
        card["values"] = {
            name: user[name] if name in user else entry.get("default")
            for name, entry in declared.items()
        }
    return card


def _logo_url(module_id: str, spec: Optional[dict[str, Any]]) -> Optional[str]:
    logo = (spec or {}).get("logo")
    if not logo:
        return None
    if (modules_catalog.module_root() / module_id / logo).is_file():
        return f"/_lemonseed/modules/{module_id}/{logo}"
    return None


def catalog_view() -> dict[str, Any]:
    """The portal's Modules payload: catalog cards (compatible entries only)
    plus every installed card, grouped for the two-tab page (§8)."""
    loaded = _catalog()
    version = appliance_version()
    rows = {row["id"]: row for row in db.module_rows()}
    catalog_cards: list[dict[str, Any]] = []
    incompatible: list[dict[str, str]] = []
    for module_id, spec in loaded["modules"].items():
        reason = modules_catalog.compat_reason(spec, version)
        if reason:
            incompatible.append({"id": module_id, "reason": reason})
            continue
        catalog_cards.append(_module_card(module_id, spec, rows.get(module_id)))
    installed_cards = [
        _module_card(module_id, loaded["modules"].get(module_id), row)
        for module_id, row in sorted(rows.items())
    ]
    return {
        "catalog_version": loaded["catalog_version"],
        "catalog": catalog_cards,
        "installed": installed_cards,
        "incompatible": incompatible,
        "problems": loaded["problems"],
        "op": _op_state(),
    }


def snapshot() -> dict[str, Any]:
    """Cheap status for /api/system and /api/modules (no docker calls)."""
    try:
        rows = db.module_rows()
    except Exception as exc:  # noqa: BLE001 — a DB problem must not break status
        return {"state": "error", "error": str(exc)[:200]}
    return {
        "state": "ready" if rows else "idle",
        "installed": [
            {
                "id": row["id"],
                "state": row["state"],
                "version": row["version"],
                "error": row["error"],
            }
            for row in rows
        ],
        "op": _op_state(),
    }
