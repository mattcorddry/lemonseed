"""Open WebUI Docker module lifecycle (M6.1+): pull, run, health, blue/green."""

from __future__ import annotations

import calendar
import hashlib
import json
import os
import secrets
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional
from urllib.error import URLError
from urllib.request import urlopen

import auth
import db
import image_pins
import owui_client
import owui_policy
from db import SECRETS_MODULES, _module_credentials, _write_module_secret
from hardware import lemonade_api_key

# Pin a concrete release tag — never :latest / :main in appliance code. The tag
# (and its digest) come from container-images.json, which Nix reads at eval time
# too, so the appliance, the QEMU profile and this module cannot drift apart.
# Rebase via `make refresh-image-pins` after pull + QEMU/hardware smoke.
OWUI_IMAGE_KEY = "open_webui"
OWUI_IMAGE_DEFAULT = image_pins.tag_ref(OWUI_IMAGE_KEY)
OWUI_IMAGE = os.environ.get("LEMONSEED_OWUI_IMAGE", OWUI_IMAGE_DEFAULT)
# OWUI_IMAGE stays the *tag* reference on purpose: it is what the settings
# readout shows ("Open WebUI v0.11.0") and what `_owui_progress_copy` shortens.
# A digest would leak a sha256 into user-facing copy that nobody can act on.
# The digest is used only to pull and run (see _owui_run_ref).
OWUI_PULL_TIMEOUT_SEC = 1800
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

# M6.3b: OWUI identity wire contract. These trusted-header names are the
# CP↔OWUI protocol: nginx forwards the same set for household users after the
# auth_request gate, and the CP signs in directly to the slot port (localhost
# only) with its own set — so only the CP can mint identities outside portal.
OWUI_TRUSTED_EMAIL_HEADER = "X-Forwarded-Email"
OWUI_TRUSTED_NAME_HEADER = "X-Forwarded-Name"
OWUI_TRUSTED_GROUPS_HEADER = "X-Forwarded-Groups"
OWUI_TRUSTED_ROLE_HEADER = "X-Forwarded-Role"

# CP service identity in OWUI (DESIGN.md M6.3b). Signing in first at
# first-healthy makes it OWUI's primary admin — the first-created user, which
# OWUI refuses to let anyone else modify or delete. It cannot be hidden from
# the admin user list, so the deterrent is the explicit display name. Shares the
# portal's synthetic email domain so the admin user list reads uniformly.
OWUI_SERVICE_EMAIL = auth.owui_email("lemonseed-cp")
OWUI_SERVICE_NAME = "Lemonseed Control Plane"
OWUI_SERVICE_ROLE = "admin"

# M6.3a: appliance identity for all models (OWUI merges into chat params).
# M6.3a/M6.3b: the Lemonseed identity prompt, model params, and model
# metadata now live in owui-policy.json (initial_setup seeds) — reconciled
# through the admin API, not env.



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
            check=False,
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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
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
            check=False,
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
            check=False,
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


def _docker_duration(seconds: float) -> str:
    """Whole-unit duration the way `docker ps` says it ("59 minutes", "2 hours")."""
    total = max(0, int(seconds))
    if total < 60:
        value, unit = total, "second"
    elif total < 3600:
        value, unit = total // 60, "minute"
    elif total < 86400:
        value, unit = total // 3600, "hour"
    else:
        value, unit = total // 86400, "day"
    return f"{value} {unit}" + ("" if value == 1 else "s")


def _docker_epoch(text: str) -> Optional[float]:
    """Unix time from docker's RFC 3339 stamp; None for the zero value / junk.

    Only whole seconds are needed, so the fraction is dropped rather than
    parsed (docker emits nanosecond precision, which `datetime.fromisoformat`
    only accepts from 3.11 onwards). Docker prints UTC, so the fields are read
    as UTC (`calendar.timegm`), not local time.
    """
    text = (text or "").strip()
    if len(text) < 19 or text.startswith("0001-01-01"):
        return None
    try:
        return float(calendar.timegm(time.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None


def format_container_status(
    fields: list[str], now: float
) -> Optional[tuple[str, str]]:
    """Join docker inspect fields into the phrase `docker ps` would print.

    `fields` is [status, started_at, finished_at, exit_code, health, image].
    Returns (status, image) with status like "Up 59 minutes (healthy)" or
    "Exited (1) 3 minutes ago", or None when the status field is unusable.
    """

    def _text(index: int) -> str:
        return fields[index].strip() if len(fields) > index else ""

    status = _text(0).lower()
    if not status or status == "<no value>":
        return None
    health = _text(4)
    if status == "running":
        started = _docker_epoch(_text(1))
        elapsed = 0.0 if started is None else max(0.0, now - started)
        phrase = f"Up {_docker_duration(elapsed)}"
        if health:
            phrase += f" ({health})"
        return (phrase, _text(5))
    if status in ("paused", "restarting", "created"):
        return (status.capitalize(), _text(5))
    # exited / dead: docker reports the exit code and how long ago it stopped.
    finished = _docker_epoch(_text(2))
    code = _text(3)
    phrase = status.capitalize()
    if code.isdigit():
        phrase += f" ({code})"
    if finished is not None:
        phrase += f" {_docker_duration(now - finished)} ago"
    return (phrase, _text(5))


def owui_container_status(name: Optional[str] = None) -> Optional[tuple[str, str]]:
    """(docker status, image ref) for the active Open WebUI container.

    The portal shows docker's own wording ("Up 59 minutes (healthy)") because
    it is what an admin sees in `docker ps`. Returns None when docker cannot
    answer, so the caller falls back to the control plane's lifecycle word
    instead of inventing an uptime.
    """
    container = name or _container_name(_active_port_from_upstream())
    result = _docker(
        [
            "inspect",
            "-f",
            "{{.State.Status}}|{{.State.StartedAt}}|{{.State.FinishedAt}}"
            "|{{.State.ExitCode}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}"
            "|{{.Config.Image}}",
            container,
        ],
        timeout=15,
    )
    if result.returncode != 0:
        return None
    return format_container_status((result.stdout or "").strip().split("|"), time.time())


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


# Keys OWUI reads from env at import; must match the secrets file or we
# blue/green recreate OWUI. Since the M6.3b policy engine landed, everything
# else the container used to receive via env is reconciled through the admin
# API (owui-policy.json), so the env floor is the bootstrap set only.
#
# Staleness detection uses a CP-owned generation marker (below), NOT absence
# checks on retired keys: the OWUI image bakes OLLAMA_BASE_URL, WEBUI_SECRET_KEY,
# OPENAI_API_KEY and OPENAI_API_BASE_URL into its Config.Env, and docker
# merges --env-file over image ENV — `docker inspect` can therefore never
# report those keys absent, retired or not (QEMU proved this: infinite
# blue/green recreate storm). A marker only our CP writes distinguishes
# containers across any floor change; bump _ENV_GEN_VALUE whenever the floor
# or its semantics change.
_BOOTSTRAP_ENV_KEYS = (
    "DATABASE_URL",
    "WEBUI_SECRET_KEY",
    "WEBUI_AUTH",
    "WEBUI_AUTH_TRUSTED_EMAIL_HEADER",
    "WEBUI_AUTH_TRUSTED_NAME_HEADER",
    "WEBUI_AUTH_TRUSTED_GROUPS_HEADER",
    "WEBUI_AUTH_TRUSTED_ROLE_HEADER",
    "OFFLINE_MODE",
    "WEBUI_NAME",
    "ENABLE_VERSION_UPDATE_CHECK",
)

_ENV_GEN_KEY = "LEMONSEED_OWUI_ENV_GEN"
_ENV_GEN_VALUE = "2"

# The docker --env-file key set: bootstrap keys + the env-generation marker.
# (The module credential store intentionally holds more: OPENAI_API_KEY and
# DB_PASSWORD are storage-only and never reach the container.)
_CONTAINER_ENV_KEYS = _BOOTSTRAP_ENV_KEYS + (_ENV_GEN_KEY,)


def _bootstrap_env_drift(name: str, desired: dict[str, str]) -> Optional[str]:
    """First human-readable reason the container env is stale, or None.

    ``desired`` is always ``_container_env_subset(...)`` — the bootstrap keys
    plus our generation marker. A pre-shrink container lacks the marker and
    is flagged for recreate; image-baked keys are simply not in ``desired``
    and are ignored.
    """
    current = _container_env_map(name)
    if not current:
        return "no env map (docker inspect empty?)"
    for key, want in desired.items():
        if current.get(key) != want:
            return (f"{key} differs: container={current.get(key)!r} "
                    f"want={want!r}")
    return None


def _bootstrap_env_ok(name: str, desired: dict[str, str]) -> bool:
    """True when the running container Env matches the bootstrap policy set."""
    return _bootstrap_env_drift(name, desired) is None


def enrich_open_webui_secrets() -> dict[str, str]:
    """Merge Lemonade + OWUI knobs into the module secret file.

    M6.3b Phase 3: the payload is the **bootstrap set only** — env is read at
    OWUI import time for database/identity wiring; every other knob is
    reconciled into the DB config table through the admin API from
    owui-policy.json. OPENAI_API_KEY stays *as secret storage* (lemond and
    the policy's ``openai.api_keys`` entry read it from here) but is no
    longer an OWUI env entry.
    """
    existing = _module_credentials(MODULE_ID)
    database_url = existing.get("DATABASE_URL", "")
    if not database_url:
        return {}

    secret_key = existing.get("WEBUI_SECRET_KEY") or secrets.token_urlsafe(32)
    # OWUI is on the lemon network; use the Postgres service DNS name.
    database_url = _module_database_url(database_url)
    # OWUI auth (M6.2): WEBUI_AUTH on with trusted-header auth. The nginx edge
    # gate (auth_request) is the real login; it forwards the signed-in user's
    # identity via X-Forwarded-Email/-Name/-Groups/-Role (set only from the CP
    # session-verify response, so client-supplied values are dropped). OWUI's
    # trusted-header provider keys identity on EMAIL, auto-creates one account
    # per user, and syncs groups + role — no OWUI password login, no https
    # requirement, and the user is never called “User”.
    payload = {
        **existing,
        "DATABASE_URL": database_url,
        "WEBUI_SECRET_KEY": secret_key,
        "WEBUI_AUTH": "True",
        "WEBUI_AUTH_TRUSTED_EMAIL_HEADER": OWUI_TRUSTED_EMAIL_HEADER,
        "WEBUI_AUTH_TRUSTED_NAME_HEADER": OWUI_TRUSTED_NAME_HEADER,
        "WEBUI_AUTH_TRUSTED_GROUPS_HEADER": OWUI_TRUSTED_GROUPS_HEADER,
        "WEBUI_AUTH_TRUSTED_ROLE_HEADER": OWUI_TRUSTED_ROLE_HEADER,
        "WEBUI_NAME": "Lemonseed",
        "OFFLINE_MODE": "True",
        "ENABLE_VERSION_UPDATE_CHECK": "False",
        # Secret *storage* only (policy resolver + lemond consumers read it
        # here); _write_container_env keeps it out of the OWUI environment.
        "OPENAI_API_KEY": lemonade_api_key() or existing.get("OPENAI_API_KEY", ""),
    }
    _write_module_secret(MODULE_ID, payload)
    _write_container_env(payload)
    return payload


def _container_env_subset(payload: dict[str, str]) -> dict[str, str]:
    out = {k: v for k, v in payload.items() if k in _BOOTSTRAP_ENV_KEYS}
    out[_ENV_GEN_KEY] = _ENV_GEN_VALUE
    return out


def _write_container_env(payload: dict[str, str]) -> None:
    """Write the docker --env-file: bootstrap keys + env-generation marker.

    The module credential store (open-webui.env) intentionally holds more
    (OPENAI_API_KEY, DB_PASSWORD); OWUI's container env must not.
    """
    SECRETS_MODULES.mkdir(parents=True, exist_ok=True)
    path = _env_file_path()
    tmp = path.with_name(path.name + ".tmp")
    lines = [f"{k}={v}" for k, v in _container_env_subset(payload).items()]
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o640)
    tmp.replace(path)


def _env_file_path() -> Path:
    # Derived docker env-file, separate from the module credential store
    # (open-webui.env): only the bootstrap subset reaches the container.
    return SECRETS_MODULES / f"{MODULE_ID}.container.env"


# M6.3b: CP service identity in OWUI. In-memory because provisioning is
# idempotent in the DB — a CP restart just re-signs-in once on next boot.
_service_identity_ready = False
_service_account_id: Optional[str] = None


def ensure_owui_service_identity(port: int) -> bool:
    """Provision the CP's service account inside Open WebUI (M6.3b).

    Signs in as OWUI_SERVICE_EMAIL via trusted headers against the slot port
    (localhost only), which auto-provisions the account when missing and
    re-asserts its admin role on every sign-in. Must happen before the first
    household sign-in: OWUI's first-created user is the primary admin and is
    immune to modify/delete, so whoever signs in first owns that seat.

    Idempotent — a no-op once provisioned; until then the supervisor retries
    each tick. Never raises: a not-yet-provisioned identity must not fail an
    otherwise-healthy module.
    """
    global _service_identity_ready, _service_account_id
    if _service_identity_ready:
        return True
    try:
        client = owui_client.OwuiClient(
            f"http://127.0.0.1:{port}",
            email_header=OWUI_TRUSTED_EMAIL_HEADER,
            name_header=OWUI_TRUSTED_NAME_HEADER,
            groups_header=OWUI_TRUSTED_GROUPS_HEADER,
            role_header=OWUI_TRUSTED_ROLE_HEADER,
        )
        result = client.sign_in_trusted(
            OWUI_SERVICE_EMAIL, name=OWUI_SERVICE_NAME, role=OWUI_SERVICE_ROLE
        )
    except Exception as exc:  # noqa: BLE001 — service boundary; heals on next tick
        print(f"owui service identity: {exc}", flush=True)
        return False
    payload = result.get("data")
    data: dict[str, Any] = payload if isinstance(payload, dict) else {}
    # The sign-in response can carry a pre-update role (OWUI syncs the trusted
    # role after authenticating); requiring admin here means the next tick
    # re-signs-in and sees the synced value.
    if result.get("ok") and data.get("role") == OWUI_SERVICE_ROLE and data.get("id"):
        _service_identity_ready = True
        _service_account_id = str(data["id"])
        print(f"owui service identity: provisioned (id={_service_account_id})", flush=True)
        # The %CP_OWUI_ID% guards could not render until this moment, so do not
        # wait up to a supervisor tick to install them.
        sync_owui_guards()
        return True
    detail = str(result.get("error") or f"unexpected role {data.get('role')!r}")[:200]
    print(f"owui service identity: not provisioned: {detail}", flush=True)
    return False


# ---------------------------------------------------------------------------
# M6.3b Phase 3: policy reconcile wiring. The engine (owui_policy) owns all
# semantics; this section is glue: deps wiring, trigger bookkeeping, and the
# supervisor hooks. Reconcile failures never touch the module state machine —
# they surface via the owui_policy snapshot field (iframe gate, decision D1).
# ---------------------------------------------------------------------------

POLICY_AUDIT_INTERVAL_SEC = int(
    os.environ.get("LEMONSEED_OWUI_POLICY_AUDIT_SECONDS", "300") or "300"
)
# (port, container) whose write-reconcile completed; audit cadence anchor.
_policy_slot_done: Optional[tuple[int, str]] = None
_policy_audit_at = 0.0


def _owui_cp_key_path() -> Path:
    # Separate file (not the module env file): OWUI_CP_API_KEY must never
    # become OWUI container environment — the container env floor stays
    # bootstrap-keys-only (§8 smoke asserts it).
    return SECRETS_MODULES / f"{MODULE_ID}.cp-key"


def _load_owui_cp_key() -> Optional[str]:
    try:
        key = _owui_cp_key_path().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return key or None


def _persist_owui_cp_key(key: str) -> bool:
    try:
        path = _owui_cp_key_path()
        path.write_text(key + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return True
    except OSError as exc:  # noqa: BLE001 — mint still works this run in-memory
        print(f"owui cp key persist: {exc}", flush=True)
        return False


def _policy_context() -> owui_policy.ResolverContext:
    placeholders: dict[str, str] = {}
    if _service_account_id:
        placeholders = {
            "CP_OWUI_ID": _service_account_id,
            "SERVICE_ACCOUNT_ID": _service_account_id,
        }
    return owui_policy.ResolverContext(
        secrets=_module_credentials(MODULE_ID),
        lemond_base=LEMONADE_BASE,
        placeholders=placeholders,
        cp_users=auth.owui_identities(),
    )


def _policy_deps(port: int) -> owui_policy.ReconcileDeps:
    return owui_policy.ReconcileDeps(
        build_client=lambda api_map, api_key, on_remint: owui_client.OwuiClient(
            f"http://127.0.0.1:{port}",
            email_header=OWUI_TRUSTED_EMAIL_HEADER,
            name_header=OWUI_TRUSTED_NAME_HEADER,
            groups_header=OWUI_TRUSTED_GROUPS_HEADER,
            role_header=OWUI_TRUSTED_ROLE_HEADER,
            api_map=api_map,
            api_key=api_key,
            on_remint=on_remint,
        ),
        service_identity={
            "email": OWUI_SERVICE_EMAIL,
            "name": OWUI_SERVICE_NAME,
            "role": OWUI_SERVICE_ROLE,
        },
        load_api_key=_load_owui_cp_key,
        persist_api_key=_persist_owui_cp_key,
        refresh_context=_policy_context,
    )


def _policy_pending_triggers() -> frozenset[str]:
    """Cadence set for a slot's write-reconcile (§5.3 firing rules)."""
    triggers = {"at_boot"}
    state = owui_policy.load_state()
    try:
        manifest = owui_policy.load_manifest()
    except owui_policy.ManifestError:
        manifest = None
    # Unreadable manifest or absent/changed hash: also re-assert on_upgrade —
    # idempotent writes, the fail-safe reading of "undeterminable" (§5.5).
    if manifest is None or not state or state.get("policy_hash") != manifest.policy_hash:
        triggers.add("on_upgrade")
    return frozenset(triggers)


def _policy_maybe_write(port: int, container: str) -> None:
    """Run the pending write-reconcile for a healthy identified slot.

    Called from the ensure_open_webui ready paths only (identity gate).
    Retries each tick until ok; reconcile itself never raises.
    """
    global _policy_slot_done, _policy_audit_at
    if not _service_identity_ready or _policy_slot_done == (port, container):
        return
    triggers = _policy_pending_triggers()
    result = owui_policy.reconcile(port, triggers, _policy_deps(port))
    if result.get("skipped"):
        return
    if result.get("ok"):
        _policy_slot_done = (port, container)
        _policy_audit_at = time.monotonic()
        # Rare (boot / slot change / CP restart) — journal lines here are the
        # audit trail for "when did the CP last write OWUI intent".
        print(f"owui policy: {'+'.join(sorted(triggers))} reconcile ok; "
              f"audit armed for {container}:{port}", flush=True)
    else:
        print(f"owui policy: reconcile not ok: {result.get('last_error')}", flush=True)


def _policy_maybe_audit(port: int, container: str) -> None:
    """Slow periodic audit-only reconcile (observe + drift report, D3)."""
    global _policy_audit_at
    if _policy_slot_done != (port, container):
        return
    now = time.monotonic()
    if now - _policy_audit_at < POLICY_AUDIT_INTERVAL_SEC:
        return
    _policy_audit_at = now
    result = owui_policy.reconcile(port, frozenset(), _policy_deps(port))
    drift = result.get("drift") or {}
    print(f"owui policy: audit tick ok={result.get('ok')} "
          f"pending={drift.get('pending', 0)} "
          f"error={result.get('last_error') or '-'}", flush=True)


# ---------------------------------------------------------------------------
# M6.3b Phase 4B: guard rendering + edge enforcement.
#
# The manifest *declares* ownership; this section makes that true by rendering
# /run/lemonseed/owui-guards.conf, which nginx includes in `locations."/"`.
# The whole file is re-rendered from the manifest every sync (never edited
# incrementally), so a guard that stops being enforced cannot survive as a stale
# rule — that is the no-stale-rules guarantee (DESIGN 4B §3.2.1).
#
# Two hashes are tracked on purpose: what is on disk, and what nginx is known
# to be serving. A reload can fail after a successful write; with one hash the
# CP would believe its own claim forever.
# ---------------------------------------------------------------------------

GUARDS_CONF = Path("/run/lemonseed/owui-guards.conf")

# sha256 of the guard text the live nginx is known to serve. Advanced only on a
# successful reload, so the next tick retries a failed one.
_guards_nginx_sha: Optional[str] = None
# Last render failure we logged, so a persistently broken manifest logs once
# instead of once per tick.
_guards_error_logged: Optional[str] = None


def _guards_placeholders() -> dict[str, str]:
    """Guard placeholder map (empty until the service account id is known).

    Deliberately its own accessor: `_policy_context()` is *not* reusable here
    because it reads module secrets from the DB, and guards must render even
    while the data plane is unavailable.
    """
    if not _service_account_id:
        return {}
    return {
        "CP_OWUI_ID": _service_account_id,
        "SERVICE_ACCOUNT_ID": _service_account_id,
    }


def render_owui_guards() -> dict[str, Any]:
    """Render the guard include from the manifest. Never raises.

    Returns ``{ok, error, render, declared, declared_sha}`` where ``declared``
    is the enforce-mode guard count in the manifest and ``declared_sha`` the
    hash of the text that render produced.
    """
    try:
        manifest = owui_policy.load_manifest()
        render = owui_policy.render_guards(manifest, _guards_placeholders())
    except owui_policy.PolicyError as exc:
        return {
            "ok": False,
            "error": str(exc)[:300],
            "render": None,
            "declared": 0,
            "declared_sha": None,
        }
    return {
        "ok": True,
        "error": None,
        "render": render,
        "declared": sum(1 for g in manifest.guards if g.mode == "enforce"),
        "declared_sha": render.sha256,
    }


def _guards_file_sha() -> Optional[str]:
    try:
        data = GUARDS_CONF.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def _write_guards_conf(text: str) -> bool:
    """Atomic replace, same pattern as ``auth._write_conf`` (a reload landing
    mid-write would leave nginx with truncated config)."""
    try:
        GUARDS_CONF.parent.mkdir(parents=True, exist_ok=True)
        tmp = GUARDS_CONF.with_name(GUARDS_CONF.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o644)
        tmp.replace(GUARDS_CONF)
        return True
    except OSError as exc:  # noqa: BLE001 — keep the previous file, retry tick
        print(f"owui guards: write failed: {exc}", flush=True)
        return False


def sync_owui_guards() -> dict[str, Any]:
    """Make the rendered include and the live nginx match the manifest.

    Cheap enough for every supervisor tick: one small JSON parse plus a hash
    compare. The expensive parts (file replace + nginx reload) only run when
    the rendered hash actually changed, so a quiet appliance does zero I/O.
    """
    global _guards_nginx_sha, _guards_error_logged

    result = render_owui_guards()
    if not result["ok"]:
        # Fail-open for the UI, fail-closed for intent: keep the previous file
        # (never a partial render) and report it. The reconcile loop is
        # unaffected because it talks to the slot port, not through nginx.
        if result["error"] != _guards_error_logged:
            print(f"owui guards: render failed: {result['error']}", flush=True)
            _guards_error_logged = result["error"]
        return owui_guard_state(result)
    _guards_error_logged = None

    render = result["render"]
    on_disk = _guards_file_sha()
    if on_disk != render.sha256:
        if not _write_guards_conf(render.text):
            return owui_guard_state(result)
        print(
            f"owui guards: rendered {len(render.enforced)} enforced "
            f"({len(render.pending)} pending) sha={render.sha256[:12]}",
            flush=True,
        )
    if on_disk != render.sha256 or _guards_nginx_sha != render.sha256:
        if reload_nginx():
            _guards_nginx_sha = render.sha256
        else:
            # Leave _guards_nginx_sha alone: the next tick re-attempts the
            # reload instead of trusting a config nginx never accepted.
            print("owui guards: nginx reload failed; will retry next tick", flush=True)
    return owui_guard_state(result)


def owui_guard_state(result: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Guard truth for /api/modules and the portal (T4-B: *enforced* means
    actually served, not merely declared).

    ``rendered`` lists enforced keys only when declared == on-disk == nginx; a
    mismatch reports an empty list plus ``pending_reason`` so the portal can
    never paint intent as reality.
    """
    if result is None:
        result = render_owui_guards()
    render = result.get("render")
    rendered_sha = _guards_file_sha()
    declared_sha = result.get("declared_sha")
    in_sync = bool(result.get("ok")) and rendered_sha == declared_sha == _guards_nginx_sha
    enforced = list(render.enforced) if (in_sync and render) else []
    return {
        "declared": result.get("declared", 0),
        "declared_sha": declared_sha,
        "rendered_sha": rendered_sha,
        "nginx_sha": _guards_nginx_sha,
        "in_sync": in_sync,
        "rendered": enforced,
        "pending": list(render.pending) if render else [],
        "enforced_count": len(enforced),
        "pending_reason": result.get("error") or (
            render.errors[0] if render and render.errors else None
        ),
    }


def owui_config_export() -> dict[str, Any]:
    """Live Open WebUI config map for the admin portal (M6.3b Phase 4A).

    Signs in as the CP service identity over trusted headers (the only identity
    allowed on the slot ports) and reads ``configs/export`` from the active
    slot. Never raises and returns ``{ok, data, error}``: the portal renders
    manifest intent when Open WebUI is mid-restart, so an unavailable export is
    a state to display, not an error to propagate.

    Values come back raw because ``configs/export`` is unredacted; masking is
    the view's job (``owui_portal``), so nothing here should cache or echo the
    payload into a log line.
    """
    port = _active_port_from_upstream()
    if not port or not _service_identity_ready:
        return {"ok": False, "data": None, "error": "open webui is not ready"}
    try:
        client = owui_client.OwuiClient(
            f"http://127.0.0.1:{port}",
            email_header=OWUI_TRUSTED_EMAIL_HEADER,
            name_header=OWUI_TRUSTED_NAME_HEADER,
            groups_header=OWUI_TRUSTED_GROUPS_HEADER,
            role_header=OWUI_TRUSTED_ROLE_HEADER,
        )
        signed = client.sign_in_trusted(
            OWUI_SERVICE_EMAIL, name=OWUI_SERVICE_NAME, role=OWUI_SERVICE_ROLE
        )
        if not signed.get("ok"):
            return {"ok": False, "data": None,
                    "error": f"service sign-in failed: {signed.get('error')}"[:200]}
        result = client.config_export()
    except Exception as exc:  # noqa: BLE001 — HTTP boundary; the portal degrades
        return {"ok": False, "data": None, "error": str(exc)[:200]}
    data = result.get("data")
    if not result.get("ok") or not isinstance(data, dict):
        return {"ok": False, "data": None,
                "error": str(result.get("error") or "export was not a config map")[:200]}
    return {"ok": True, "data": data, "error": None}


def _stop_container(name: str) -> None:
    _docker(["rm", "-f", name], timeout=60)


def reap_stale_containers() -> list[str]:
    """Delete not-running OWUI containers left by an earlier boot.

    Containers run with ``--restart no`` (the supervisor owns lifecycle), so
    every reboot — and every blue/green or reset that stopped one — leaves an
    exited container in ``docker ps -a`` that nothing will ever start again.
    It does no harm to the running system, but it makes ``docker ps`` a lie for
    an admin who is troubleshooting, and after a ``lemonseed-update`` it keeps
    the previous release's container around forever.

    Only non-running containers are touched: a *running* container on the other
    slot is a legitimate blue/green survivor.
    """
    listing = _docker(
        [
            "ps",
            "-a",
            "--filter",
            f"name={CONTAINER_PREFIX}-",
            "--format",
            "{{.Names}}\t{{.State}}",
        ],
        timeout=60,
    )
    if listing.returncode != 0:
        return []
    removed: list[str] = []
    for line in (listing.stdout or "").splitlines():
        name, _, state = line.partition("\t")
        name = name.strip()
        if not name.startswith(f"{CONTAINER_PREFIX}-"):
            continue  # the name filter is a substring match, not a prefix one
        if state.strip().lower().startswith("running"):
            continue
        if _docker(["rm", "-f", name], timeout=60).returncode == 0:
            removed.append(name)
    if removed:
        print(
            f"owui supervise: removed {len(removed)} stopped container(s): "
            f"{', '.join(sorted(removed))}",
            flush=True,
        )
    return removed


def ensure_lemon_network() -> tuple[bool, str]:
    inspect = _docker(["network", "inspect", LEMON_NETWORK], timeout=30)
    if inspect.returncode == 0:
        return True, "exists"
    create = _docker(["network", "create", "--driver", "bridge", LEMON_NETWORK], timeout=30)
    if create.returncode != 0:
        return False, (create.stderr or create.stdout or "network create failed").strip()
    return True, "created"


# ---------------------------------------------------------------------------
# M6.3b Phase 4B: admin-triggered intent actions.
#
# Two operations an admin can ask for from the portal: re-apply Lemonseed's
# intent now (T4-E), and rebuild from scratch by dropping Open WebUI's settings
# table (T4-C/T4-D). Both are manual-only, both are admin-gated twice (nginx
# `adminGate` + CP `_require_admin`), and neither ever raises into the HTTP
# handler.
# ---------------------------------------------------------------------------

# A manual run must be able to do everything the boot run does, or the button is
# a lie. "manual" exists so a future rule can opt out of boot-time writes.
MANUAL_RECONCILE_TRIGGERS = frozenset({"at_boot", "manual"})
# A reset wipes every row, so it is a first-install: fire every cadence.
RESET_RECONCILE_TRIGGERS = frozenset({"at_boot", "on_upgrade", "manual"})
# Typed confirmation for the destructive path. Not a secret — a speed bump that
# makes an accidental click impossible. Compared case-insensitively and kept to
# one short word: bare-metal review found the long uppercase phrase read as a
# transcription test rather than a confirmation.
OWUI_RESET_CONFIRM = "reset"

_reconcile_request_lock = threading.Lock()
# Progress/last-result of the admin reset: {state, error, at}. The handler
# returns 202 immediately; the portal reads this from /api/modules (T4-D).
_owui_reset: dict[str, Any] = {"state": "idle", "error": None, "at": None}
_owui_reset_lock = threading.Lock()


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _slot_client(port: int) -> owui_client.OwuiClient:
    """CP-service-identity transport against a slot port (localhost only)."""
    return owui_client.OwuiClient(
        f"http://127.0.0.1:{port}",
        email_header=OWUI_TRUSTED_EMAIL_HEADER,
        name_header=OWUI_TRUSTED_NAME_HEADER,
        groups_header=OWUI_TRUSTED_GROUPS_HEADER,
        role_header=OWUI_TRUSTED_ROLE_HEADER,
    )


def owui_admin_client() -> Optional[owui_client.OwuiClient]:
    """A signed-in OWUI client for the portal's account primitives (M7 P3).

    The reconcile loop built this client inside every run, so nothing outside it
    had a keyed one until ``usermgmt`` needed ``users.delete``. Same recipe: api
    map from the manifest, the persisted CP key (minted here if absent), and one
    service sign-in that both re-asserts the service role and gives the client an
    identity to heal a 401 with.

    ``None`` means Open WebUI is not running or its service identity is not
    provisioned yet. Callers must read that as "the Open WebUI half did not
    happen" and report it, never as success: a CP account with no OWUI account is
    a real and recoverable state (USERS-AND-GROUPS.md D-18), and claiming
    otherwise is how someone ends up unable to sign in to a module nobody
    mentioned.
    """
    port = _active_port_from_upstream()
    if not port or not _service_identity_ready:
        return None
    try:
        manifest = owui_policy.load_manifest()
    except owui_policy.ManifestError as exc:
        print(f"owui admin client: no manifest: {exc}", flush=True)
        return None
    client = owui_client.OwuiClient(
        f"http://127.0.0.1:{port}",
        email_header=OWUI_TRUSTED_EMAIL_HEADER,
        name_header=OWUI_TRUSTED_NAME_HEADER,
        groups_header=OWUI_TRUSTED_GROUPS_HEADER,
        role_header=OWUI_TRUSTED_ROLE_HEADER,
        api_map=manifest.api,
        api_key=_load_owui_cp_key(),
        on_remint=_persist_owui_cp_key,
    )
    sign = client.sign_in_trusted(
        OWUI_SERVICE_EMAIL, name=OWUI_SERVICE_NAME, role=OWUI_SERVICE_ROLE
    )
    if not sign.get("ok"):
        print(f"owui admin client: sign-in failed: {sign.get('error')}", flush=True)
        return None
    if not client.api_key:
        # Not fatal: ``call`` falls back to the JWT when there is no sk- key. A
        # box with auth.enable_api_keys off is a supported configuration.
        client.mint_api_key()
    return client


def in_flight_work() -> Optional[str]:
    """Work that a reboot would leave half-done, in a sentence, else ``None``.

    The power controls ask this before shutting the box down. Each of these
    windows holds its lock for its whole duration by design (that is what stops
    the supervisor's heal from interleaving), so ``locked()`` is an honest
    signal here, and reading it can never block the handler that asks.
    """
    reset_state = str(owui_reset_state().get("state") or "idle")
    if reset_state not in ("idle", "ready", "error"):
        return "the chat interface settings are being rebuilt"
    if _reconcile_request_lock.locked() or owui_policy.reconcile_in_flight():
        return "Lemonseed settings are being applied to the chat interface"
    if _lock.locked():
        return "the chat interface is being installed, updated or restarted"
    return None


def request_policy_reconcile(
    triggers: Optional[frozenset[str]] = None,
) -> dict[str, Any]:
    """Run a write reconcile now, on behalf of an admin (T4-E).

    This is the seam the account primitives call after they change a mirror
    (`usermgmt` fires it on create, group change and delete), so it is public
    rather than a one-off for the portal button. Refusals are values, not
    exceptions: ``not ready`` (nothing to write to yet) and ``in flight``
    (non-blocking lock, the same pattern as the CP's other background work).
    """
    global _policy_slot_done, _policy_audit_at
    if not _service_identity_ready:
        return {"ok": False, "reason": "not ready"}
    port = _active_port_from_upstream()
    if not port or not http_ready(port, timeout=3.0):
        return {"ok": False, "reason": "not ready"}
    if not _reconcile_request_lock.acquire(blocking=False):
        return {"ok": False, "reason": "in flight"}
    try:
        # Clear the slot marker: _policy_maybe_write skips a slot it already
        # wrote, and an admin asking again means they want a real run.
        _policy_slot_done = None
        result = owui_policy.reconcile(
            port,
            frozenset(triggers or MANUAL_RECONCILE_TRIGGERS),
            _policy_deps(port),
        )
    except Exception as exc:  # noqa: BLE001 — HTTP boundary
        return {"ok": False, "reason": str(exc)[:200]}
    finally:
        _reconcile_request_lock.release()

    if result.get("skipped"):
        return {"ok": False, "reason": "in flight"}
    if result.get("ok"):
        _policy_slot_done = (port, _container_name(port))
        _policy_audit_at = time.monotonic()
        # This run may have minted the service-account id (first boot), which is
        # what unblocks the %CP_OWUI_ID% guards.
        sync_owui_guards()
    drift = result.get("drift") or {}
    # Counts only, never entries: the portal's ownership tree is where values
    # belong (masked, behind the same gating), and an action response is one
    # more surface a value could leak from.
    return {
        "ok": bool(result.get("ok")),
        "reason": None,
        "policy_hash": result.get("policy_hash"),
        "drift": {k: v for k, v in drift.items() if isinstance(v, int)},
        "last_error": result.get("last_error"),
    }


def owui_reset_state() -> dict[str, Any]:
    """Reset progress for /api/modules (T4-D: 202 + poll, never a held socket)."""
    with _owui_reset_lock:
        return dict(_owui_reset)


def _set_owui_reset(state: str, error: Optional[str] = None) -> None:
    with _owui_reset_lock:
        _owui_reset.update({"state": state, "error": error, "at": _iso_now()})


def reset_owui_config(confirm: str) -> dict[str, Any]:
    """Admin-only rebuild of Open WebUI's settings from the manifest (T4-C).

    Drops OWUI's ``config`` table and lets a fresh reconcile re-apply every
    Lemonseed-owned setting, so "the settings are a mess" has an answer that
    does not require reflashing. Nothing destructive happens without the exact
    typed confirmation, and it is never automatic (P4-B).

    Returns ``{accepted, reason}`` immediately; the window runs on a daemon
    thread and reports through ``owui_reset_state()``.
    """
    if not isinstance(confirm, str) or confirm.strip().lower() != OWUI_RESET_CONFIRM:
        return {"accepted": False, "reason": "confirmation text did not match"}
    if not _service_identity_ready:
        return {"accepted": False, "reason": "not ready"}
    active = _active_port_from_upstream()
    if not active or not http_ready(active, timeout=3.0):
        return {"accepted": False, "reason": "not ready"}
    if not _lock.acquire(blocking=False):
        # A blue/green or a heal owns the slots right now; do not race it.
        return {"accepted": False, "reason": "a change is already in progress"}
    # _lock is held for the whole window (released by the worker) so the
    # supervisor's heal cannot interleave; ensure_open_webui answers "busy".
    _set_owui_reset("stopping")
    try:
        threading.Thread(
            target=_reset_owui_config_window, name="lemonseed-owui-reset", daemon=True
        ).start()
    except OSError as exc:  # noqa: BLE001 — thread never started: unlock here
        _lock.release()
        _set_owui_reset("error", str(exc)[:200])
        return {"accepted": False, "reason": str(exc)[:200]}
    return {"accepted": True, "reason": None}


def _wait_slot_ready(port: int) -> bool:
    """Wait for stable SPA-asset readiness (the same bar as a normal cutover)."""
    budget = int(os.environ.get("LEMONSEED_OWUI_READY_SECONDS", "240") or "240")
    deadline = time.monotonic() + max(30, budget)
    while time.monotonic() < deadline:
        if http_ready_stable(port, attempts=3, delay=2.0):
            return True
        time.sleep(2)
    return False


def _drop_owui_cp_key() -> bool:
    """Forget the minted ``sk-`` key.

    Required by a reset, not tidiness: the key is only valid while
    ``auth.enable_api_keys`` is true, and that row lives in the table we just
    dropped. With the stale file in place the client 401s and its repair path
    (re-mint) fails too until the settings are re-applied — so the reset's own
    reconcile would abort. Without it the client runs on its JWT and mints a
    fresh key once the settings land.
    """
    try:
        _owui_cp_key_path().unlink(missing_ok=True)
        return True
    except OSError as exc:  # noqa: BLE001 — best-effort; the reconcile reports 401s
        print(f"owui cp key drop: {exc}", flush=True)
        return False


def _restore_last_known_good(port: int) -> bool:
    """The LKG ladder, wired for the one case it exists for (D1/T4-C): a reset
    that got as far as wiping config but could not rebuild it."""
    try:
        client = _slot_client(port)
        signed = client.sign_in_trusted(
            OWUI_SERVICE_EMAIL, name=OWUI_SERVICE_NAME, role=OWUI_SERVICE_ROLE
        )
        if not signed.get("ok"):
            print(f"owui reset: LKG restore skipped (sign-in failed): "
                  f"{str(signed.get('error'))[:200]}", flush=True)
            return False
        outcome = owui_policy.restore_lkg(client)
    except Exception as exc:  # noqa: BLE001 — the ladder must not add a failure
        print(f"owui reset: LKG restore failed: {str(exc)[:200]}", flush=True)
        return False
    if outcome.get("ok"):
        print(f"owui reset: restored last-known-good config "
              f"({outcome.get('restored_keys')} keys)", flush=True)
        return True
    print(f"owui reset: LKG restore skipped: {outcome.get('reason')}", flush=True)
    return False


def _reset_owui_config_window() -> None:
    """The reset window (§6.2), on its own daemon thread.

    Order matters and unit tests assert it: both slots down *before* the
    truncate (they share one database, so blue/green buys nothing here), then
    one slot rebuilt and reconciled before anything is allowed to serve it.
    """
    global _service_identity_ready, _service_account_id, _policy_slot_done, _state

    candidate = _other_port(_active_port_from_upstream())
    try:
        # Interstitial instead of 502s for document requests while we are down.
        OWUI_READY_FLAG.unlink(missing_ok=True)
        for port in SLOT_PORTS:
            _stop_container(_container_name(port))

        _set_owui_reset("clearing")
        cut = db.truncate_owui_config()
        if cut.get("state") != "ready":
            raise RuntimeError(f"could not clear the settings table: {cut.get('error')}")
        _drop_owui_cp_key()
        # The identity we remembered was minted against state we just deleted;
        # force a fresh sign-in against the rebuilt slot.
        _service_identity_ready = False
        _service_account_id = None
        _policy_slot_done = None

        _set_owui_reset("starting")
        ok, detail = _run_container(candidate, _env_file_path())
        if not ok:
            raise RuntimeError(f"could not start the rebuilt chat interface: {detail}")
        if not _wait_slot_ready(candidate):
            raise RuntimeError("the rebuilt chat interface did not become ready")

        _set_owui_reset("reconciling")
        if not ensure_owui_service_identity(candidate):
            raise RuntimeError("the Lemonseed service account could not be restored")
        rebuild = owui_policy.reconcile(
            candidate, RESET_RECONCILE_TRIGGERS, _policy_deps(candidate)
        )
        if not rebuild.get("ok"):
            # Never cut over to an unreconciled slot: a user must not meet the
            # blank-slate OWUI we just created (decision D1).
            _restore_last_known_good(candidate)
            rebuild = owui_policy.reconcile(
                candidate, RESET_RECONCILE_TRIGGERS, _policy_deps(candidate)
            )
            if not rebuild.get("ok"):
                raise RuntimeError(
                    "settings could not be re-applied after the reset: "
                    f"{rebuild.get('last_error')}"
                )

        _set_owui_reset("cutting over")
        write_upstream(candidate)
        if not reload_nginx():
            # Non-fatal: the slot is healthy and reconciled, and the supervisor
            # retries the reload on its next tick.
            print("owui reset: nginx reload failed after cutover; supervisor will retry",
                  flush=True)
        sync_owui_guards()
        _state = {
            "state": "ready",
            "image": OWUI_IMAGE,
            "port": candidate,
            "container": _container_name(candidate),
            "path": OWUI_EMBED_PATH,
        }
        _set_owui_reset("ready")
        print(f"owui reset: settings rebuilt on port {candidate}", flush=True)
    except Exception as exc:  # noqa: BLE001 — reported via owui_reset_state()
        detail = str(exc)[:300]
        _set_owui_reset("error", detail)
        _state = {"state": "error", "error": detail, "phase": "reset"}
        print(f"owui reset: failed: {detail}", flush=True)
    finally:
        _lock.release()


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
        _owui_run_ref(),
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


def _image_present(ref: Optional[str] = None) -> bool:
    return _docker(["image", "inspect", ref or OWUI_IMAGE], timeout=30).returncode == 0


def _owui_run_ref() -> str:
    """Pinned digest when it is local, else the tag.

    A ``docker pull name@digest`` does not create ``name:tag``, so after a
    pinned pull only the digest reference resolves. Conversely the QEMU profile
    loads images from ``dockerTools.pullImage`` tarballs, which carry RepoTags
    but no RepoDigests, so only the tag resolves there. Both branches are live
    code paths, not hypotheticals (verified 2026-10-12 against the registry).
    """
    digest = image_pins.digest_ref(OWUI_IMAGE_KEY)
    if _image_present(digest):
        return digest
    return OWUI_IMAGE


# Registry reachability preflight. network-online.target can be reached while
# DNS is still broken (or the WAN link is down), in which case every docker
# pull fails with an opaque "no such host" and the supervisor hammers the
# registry on every 10s tick. Check once, cache the failure, and re-check on
# a slow cadence so the box self-heals when connectivity returns.
_REGISTRY_HOST = OWUI_IMAGE.split("/")[0] if "/" in OWUI_IMAGE else "registry-1.docker.io"
_PULL_FAIL_COOLDOWN_SEC = 60
_pull_fail_at = 0.0
_net_check_at = 0.0
_net_ok = False


def _network_reachable() -> bool:
    """Best-effort check that we can reach the image registry (DNS + TCP 443).

    Cached for _PULL_FAIL_COOLDOWN_SEC: True stays until a re-check fails,
    False is retried after the cooldown. Any exception counts as unreachable.
    """
    global _net_check_at, _net_ok
    now = time.monotonic()
    if _net_ok and now - _net_check_at < _PULL_FAIL_COOLDOWN_SEC:
        return True
    if not _net_ok and now - _net_check_at < _PULL_FAIL_COOLDOWN_SEC:
        return False
    try:
        host = socket.gethostbyname(_REGISTRY_HOST)
        with socket.create_connection((host, 443), timeout=5):
            pass
        _net_ok = True
    except (OSError, URLError, socket.gaierror):
        _net_ok = False
    _net_check_at = now
    return _net_ok


def _pull_image() -> tuple[bool, str]:
    global _pull_fail_at
    tag = OWUI_IMAGE
    digest = image_pins.digest_ref(OWUI_IMAGE_KEY)
    if _image_present(digest):
        return True, "present"
    if _image_present(tag):
        # Loaded from an appliance-local tarball (QEMU profile) or left by an
        # older build. Running it beats a pull that can only add drift.
        return True, "present:tag"
    # QEMU smoke preloads OWUI via virtio-fs. Prefer waiting over a doomed DNS pull.
    wait_s = int(os.environ.get("LEMONSEED_OWUI_WAIT_LOCAL_SECONDS", "0") or "0")
    if wait_s > 0:
        return False, "waiting for local image preload"
    if not _network_reachable():
        now = time.monotonic()
        if now - _pull_fail_at >= _PULL_FAIL_COOLDOWN_SEC:
            _pull_fail_at = now
            # One diagnostic probe so the journal shows the underlying error.
            probe = _docker(["pull", digest], timeout=30)
            if probe.returncode != 0:
                detail = (probe.stderr or probe.stdout or "").strip().splitlines()
                print(
                    f"owui pull: registry unreachable; docker probe: "
                    f"{(detail[-1] if detail else 'no output')[:300]}",
                    flush=True,
                )
        return (
            False,
            "Cannot reach the container registry — check this appliance's internet connection. "
            "Retrying automatically.",
        )
    result = _docker(["pull", digest], timeout=OWUI_PULL_TIMEOUT_SEC)
    if result.returncode == 0:
        return True, "pulled"
    # Registries do garbage-collect untagged manifests. Refusing to start the
    # chat interface would be worse than accepting drift, so fall back to the
    # tag — loudly, because we have just left the tested pin.
    print(
        f"owui pull: WARNING pinned image {digest} is unavailable "
        f"({(result.stderr or result.stdout or '').strip()[:200]}); "
        f"falling back to {tag}, which may differ from what CI validated",
        flush=True,
    )
    result = _docker(["pull", tag], timeout=OWUI_PULL_TIMEOUT_SEC)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "docker pull failed").strip()
    return True, "pulled:tag_fallback"


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
        env_ok = _bootstrap_env_ok(active_name, _container_env_subset(env))

        # Sticky ready: serving HTTP with expected env — never touch it.
        if not force_update and active_ready and env_ok:
            # Covers "booted before CP restart / first provisioning failed":
            # retry cheaply (one localhost POST) until the seat is ours.
            if ensure_owui_service_identity(active):
                _policy_maybe_write(active, active_name)
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
            print(
                f"owui env: {active_name} stale: "
                f"{_bootstrap_env_drift(active_name, _container_env_subset(env))}; "
                "recreating",
                flush=True,
            )
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

        # Take the primary-admin seat before cutover so no household user can
        # sign in as OWUI's first user first (M6.3b).
        if ensure_owui_service_identity(candidate):
            # Reconcile the candidate slot before it serves anyone (D1: users
            # never see a blank-slate OWUI); users can't reach it yet.
            _policy_maybe_write(candidate, _container_name(candidate))

        print(f"owui: cutover -> port {candidate} "
              f"container {_container_name(candidate)}", flush=True)
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

    # Guards first: they must be in place before an admin can reach the OWUI UI
    # on this tick, and cheap enough to evaluate every time (render + hash
    # compare; the write and the reload are behind that compare).
    try:
        sync_owui_guards()
    except Exception as exc:  # noqa: BLE001 — never kill the supervisor
        print(f"owui guards: sync failed: {str(exc)[:200]}", flush=True)

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
        result = ensure_open_webui(force_update=False)
        _policy_maybe_audit(active, name)
        return result

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


def _supervise_forever() -> None:
    """Worker body: reap once, then reconcile forever.

    Its own function (rather than a closure) so the ordering is testable without
    starting a thread.
    """
    # Once, before the first tick: reaping here rather than every tick avoids
    # racing a container that `docker run -d` has created but not started yet.
    try:
        reap_stale_containers()
    except Exception as exc:  # noqa: BLE001 — never kill the supervisor
        print(f"owui supervise: stale container reap failed: {str(exc)[:200]}", flush=True)
    while True:
        try:
            supervise_modules_once()
        except Exception as exc:  # noqa: BLE001 — never kill the supervisor
            print(f"owui supervise: {exc}", flush=True)
        time.sleep(SUPERVISE_INTERVAL_SEC)


def ensure_module_worker() -> None:
    """Long-lived supervisor: keep OWUI up after boot and after crashes."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return

    _worker = threading.Thread(target=_supervise_forever, name="lemonseed-owui", daemon=True)
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
    reset = owui_reset_state()
    # A reset window means both slots are down at some point and the config
    # table is empty; treat it like a blue/green so the shell waits on the
    # interstitial instead of riding a half-configured 200.
    resetting = reset.get("state") not in ("idle", "ready", "error")
    # During pull/start/replace, keep the shell on Getting Ready even if the
    # old slot still answers HTTP (policy blue/green after lemonseed-update).
    updating = resetting or lifecycle in ("pulling", "starting", "busy", "waiting")
    ready = bool(probe_ready and not updating)
    if resetting:
        state = "resetting"
    elif updating:
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
        "service_ready": _service_identity_ready,
        "workflow": workflow,
        "task": task,
    }
    if _service_account_id:
        owui["service_account_id"] = _service_account_id
    # M6.3b: manifest reconcile state — the shell's third iframe-gate
    # condition and the Phase-4 portal tree payload. cp_pid tags the CP
    # process generation so host-side smoke can spot a mid-run CP restart.
    owui["owui_policy"] = {**owui_policy.state_summary(), "cp_pid": os.getpid()}
    # M6.3b Phase 4B: declared vs *served* guard state, and reset progress.
    # No config values in either, but they stay inside the same auth gating.
    owui["owui_guards"] = owui_guard_state()
    if reset.get("state") != "idle":
        owui["owui_reset"] = reset
    if snap.get("error"):
        owui["error"] = snap["error"]
    if snap.get("reason") and not ready:
        owui["reason"] = snap["reason"]
    if snap.get("message"):
        owui["message"] = snap["message"]
    return {"open_webui": owui}


def _is_storage_wait(reason: str, snap: dict[str, Any]) -> bool:
    """Whether a "waiting" OWUI state is really the data plane holding it up."""
    if snap.get("provisioning"):
        return True
    lowered = reason.lower()
    return "data plane" in lowered or "database_url" in lowered or "api key" in lowered


def _owui_progress_copy(state: str, tag: str, snap: dict[str, Any]) -> tuple[str, str]:
    if state == "resetting":
        return "Configuring chat", "Rebuilding the chat interface settings"
    if state == "pulling" or snap.get("phase") == "pull":
        return "Configuring chat", f"Pulling the Open WebUI {tag} container"
    if state in ("starting", "busy"):
        return "Configuring chat", f"Starting Open WebUI ({tag})"
    if state == "waiting":
        reason = str(snap.get("reason") or "Waiting for storage services")
        # The chat module *reports* this, but it is not the thing that is slow:
        # OWUI cannot start until the data plane is provisioned, and telling
        # people "configuring chat" while Postgres pulls sends them looking in
        # the wrong place (M6.6 S5.7).
        if _is_storage_wait(reason, snap):
            return "Preparing storage", "Waiting for the database and cache to start"
        return "Configuring chat", reason
    if state == "error":
        return "Configuring chat", str(snap.get("error") or "Chat interface failed")
    if state == "ready":
        return "Configuring chat", "Chat interface is ready"
    return "Configuring chat", "Preparing the chat interface"
