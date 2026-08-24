#!/usr/bin/env python3
"""Lemonseed control plane (bring-up stub).

Listens on 127.0.0.1:8080 behind nginx :80.
  - Before setup: no-auth first-boot / setup wizard
  - After setup: simple ready portal (auth + module routing come later via nginx)
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from zoneinfo import available_timezones

import chat_ready
import data_services
import hardware
from catalog import plan_model


HOST = os.environ.get("APPLIANCE_CP_HOST", "127.0.0.1")
PORT = int(os.environ.get("APPLIANCE_CP_PORT", "8080"))
VERSION_FILE = Path("/run/appliance/version")
FIRST_BOOT_FILE = Path("/etc/appliance-first-boot")
SETUP_COMPLETE_FILE = Path("/etc/appliance-setup-complete")
SETUP_STATE_FILE = Path("/etc/appliance-setup.json")
HANDOFF_DONE_FILE = Path("/run/lemonseed/mdns-handoff-done")
LEMOND_ENV_FILE = Path("/var/lib/lemonseed/lemonade/lemond.env")
TIMEZONES = sorted(
    timezone
    for timezone in available_timezones()
    if "/" in timezone and not timezone.startswith(("Etc/", "posix/", "right/"))
)


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def appliance_version() -> str:
    return _read_text(VERSION_FILE) or "unknown"


def setup_complete() -> bool:
    return SETUP_COMPLETE_FILE.is_file()


def load_setup() -> dict[str, Any]:
    raw = _read_text(SETUP_STATE_FILE)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def run_best_effort(cmd: list[str]) -> None:
    """Apply a system change, logging failures to the journal rather than hiding them."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{cmd[0]}: {exc}", flush=True)
        return
    if result.returncode != 0:
        print(f"{cmd[0]} failed ({result.returncode}): {result.stderr.strip()}", flush=True)


def run_required(cmd: list[str], action: str) -> None:
    """Run an identity-critical command or fail setup with a useful message."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Could not {action}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(f"Could not {action}: {detail or f'exit {result.returncode}'}")


def mdns_name(name: str) -> str:
    """Convert a display name into the deterministic mDNS label we advertise."""
    label = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (label or "lemonseed")[:63].rstrip("-") or "lemonseed"


def primary_mdns_slug() -> str:
    """Live appliance mDNS label (setup JSON, else /etc/hostname, else lemonseed)."""
    setup = load_setup()
    slug = str(setup.get("mdns_name") or "").strip()
    if slug:
        return slug
    host = _read_text(Path("/etc/hostname"))
    if host:
        return host.splitlines()[0].strip() or "lemonseed"
    return "lemonseed"


def request_hostname(handler: BaseHTTPRequestHandler) -> str:
    return (handler.headers.get("Host") or "").split(":")[0].strip().lower()


def mark_mdns_handoff_done() -> None:
    """Drop the temporary lemonseed.local CNAME after installer discovery."""
    try:
        HANDOFF_DONE_FILE.parent.mkdir(parents=True, exist_ok=True)
        HANDOFF_DONE_FILE.write_text("1\n", encoding="utf-8")
    except OSError as exc:
        print(f"mdns handoff marker: {exc}", flush=True)
    run_best_effort(["systemctl", "stop", "lemonseed-avahi-alias.service"])


def maybe_complete_mdns_handoff(handler: BaseHTTPRequestHandler) -> None:
    """If the client found us via lemonseed.local, release that alias."""
    host = request_hostname(handler)
    if host not in ("lemonseed.local", "lemonseed"):
        return
    slug = primary_mdns_slug()
    if not slug or slug == "lemonseed":
        return
    if not HANDOFF_DONE_FILE.is_file():
        print(f"mdns handoff: client used {host}; releasing alias (primary {slug}.local)", flush=True)
    mark_mdns_handoff_done()


def redirect_off_factory_mdns(handler: BaseHTTPRequestHandler, path: str) -> bool:
    """Send browsers on lemonseed.local to the configured <slug>.local URL."""
    host = request_hostname(handler)
    if host not in ("lemonseed.local", "lemonseed"):
        return False
    slug = primary_mdns_slug()
    if not slug or slug == "lemonseed":
        return False
    mark_mdns_handoff_done()
    location = f"http://{slug}.local{path or '/'}"
    body = (
        f"<html><head><meta http-equiv=\"refresh\" content=\"0;url={escape(location, quote=True)}\"/>"
        f"</head><body>Continue at <a href=\"{escape(location, quote=True)}\">{escape(location)}</a>.</body></html>\n"
    ).encode()
    handler.send_response(302)
    handler.send_header("Location", location)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)
    return True


def local_ipv4_addresses() -> set[str]:
    try:
        output = subprocess.check_output(
            ["ip", "-4", "-o", "addr", "show", "up", "scope", "global"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()
    addresses = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) >= 4 and not fields[1].startswith("docker"):
            addresses.add(fields[3].split("/", 1)[0])
    return addresses


def avahi_hostname() -> str:
    """Return Avahi's effective hostname, including any collision suffix."""
    try:
        output = subprocess.check_output(
            [
                "busctl",
                "call",
                "org.freedesktop.Avahi",
                "/",
                "org.freedesktop.Avahi.Server",
                "GetHostName",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        fields = shlex.split(output)
        return fields[1].lower() if len(fields) >= 2 and fields[0] == "s" else ""
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
        ValueError,
    ):
        return ""


def mdns_conflict(label: str) -> bool:
    """True when another host already owns <label>.local on this LAN."""
    # Avahi knows its effective name even when its address changed since a
    # previous DHCP lease. This is a stronger self-test than comparing IPs.
    if label.lower() == avahi_hostname():
        return False
    try:
        output = subprocess.check_output(
            ["avahi-resolve-host-name", "-4", f"{label}.local"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False
    remote_addresses = {
        line.rsplit(maxsplit=1)[-1]
        for line in output.splitlines()
        if line.strip()
    }
    return bool(remote_addresses - local_ipv4_addresses())


def suggested_name() -> str:
    base = "Lemonseed"
    if not mdns_conflict(mdns_name(base)):
        return base
    for number in range(2, 100):
        candidate = f"{base} {number}"
        if not mdns_conflict(mdns_name(candidate)):
            return candidate
    return "Lemonseed 100"


def docker_snapshot() -> dict[str, str]:
    """Probe the Docker daemon, not merely its socket-activation unit."""
    try:
        version = subprocess.check_output(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=5,
        ).strip()
        return {"state": "ready", "version": version}
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        output = getattr(exc, "output", "") or ""
        return {"state": "unavailable", "detail": output.strip()[:200]}


def lemond_snapshot() -> dict[str, Any]:
    """Return only Lemonseed-safe local inference readiness fields."""
    raw = _read_text(LEMOND_ENV_FILE)
    key = next(
        (line.removeprefix("LEMONADE_API_KEY=") for line in (raw or "").splitlines()
         if line.startswith("LEMONADE_API_KEY=")),
        "",
    )
    if not key:
        return {"state": "not-configured"}
    try:
        req = Request(
            "http://127.0.0.1:13305/v1/health",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urlopen(req, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return {"state": "ready", "health": payload}
    except Exception:  # noqa: BLE001 - service may be starting or unavailable
        return {"state": "starting"}


def boot_snapshot() -> dict[str, Any]:
    """Best-effort readiness signals for the first-boot UI."""
    core_units = [
        "network-online.target",
        "nginx.service",
        "avahi-daemon.service",
        "lemond.service",
        "appliance-control-plane.service",
    ]
    data_units = [
        "lemonseed-postgres.service",
        "lemonseed-valkey.service",
    ]
    status: dict[str, str] = {}
    for unit in core_units + data_units:
        try:
            out = subprocess.check_output(
                ["systemctl", "is-active", unit],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            out = "unknown"
        status[unit] = out
    lemond = lemond_snapshot()
    docker = docker_snapshot()
    startup_ready = (
        all(status[u] == "active" for u in core_units)
        and docker["state"] == "ready"
        and lemond["state"] == "ready"
        and FIRST_BOOT_FILE.is_file()
    )
    return {
        "version": appliance_version(),
        "first_boot": _read_text(FIRST_BOOT_FILE),
        "setup_complete": setup_complete(),
        "units": status,
        "first_boot_marker": "recorded" if FIRST_BOOT_FILE.is_file() else "pending",
        "lemond": lemond,
        "docker": docker,
        "data": data_services.data_snapshot(),
        "startup_ready": startup_ready,
    }


STYLE = """
:root {
  --bg0: #0f1c18;
  --ink: #f2f7f4;
  --muted: #a7bfb4;
  --accent: #c6f27a;
  --accent-ink: #142018;
  --line: rgba(242, 247, 244, 0.14);
  --ok: #9be67c;
  --wait: #ffe2a8;
  --bad: #ff8f8f;
  --font: "Segoe UI", "Helvetica Neue", sans-serif;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; min-height: 100%;
  background:
    radial-gradient(1100px 560px at 8% -10%, #2a5a45 0%, transparent 55%),
    radial-gradient(900px 480px at 100% 0%, #1c3d55 0%, transparent 50%),
    var(--bg0);
  color: var(--ink); font-family: var(--font);
}
.shell { max-width: 36rem; margin: 0 auto; padding: 2.25rem 1.25rem 3rem; }
.wordmark {
  margin: 0; font-size: 0.85rem; letter-spacing: 0.18em;
  text-transform: uppercase; color: var(--accent); font-weight: 700;
}
h1 { margin: 0.35rem 0 0.45rem; font-size: clamp(2rem, 8vw, 2.5rem); letter-spacing: -0.03em; }
.lede { color: var(--muted); line-height: 1.45; margin: 0; }
.panel {
  margin-top: 1.5rem; padding: 1.1rem;
  border: 1px solid var(--line); border-radius: 14px;
  background: rgba(15, 28, 24, 0.55);
}
.panel h2 {
  margin: 0 0 0.75rem; font-size: 0.9rem; letter-spacing: 0.04em;
  text-transform: uppercase; color: var(--muted);
}
.row { display: flex; justify-content: space-between; gap: 1rem; padding: 0.35rem 0; }
.row + .row { border-top: 1px solid var(--line); }
.name { color: var(--muted); font-size: 0.92rem; }
.state { font-variant-numeric: tabular-nums; font-weight: 600; }
.state.ok { color: var(--ok); }
.state.wait { color: var(--wait); }
.state.bad { color: var(--bad); }
.progress {
  height: 0.7rem; overflow: hidden; border-radius: 999px;
  background: rgba(255,255,255,0.1); margin: 0.85rem 0 0.55rem;
}
.progress > span {
  display: block; height: 100%; background: linear-gradient(90deg, #7dcf6a, var(--accent));
  transition: width 0.4s ease;
}
details { margin-top: 1rem; }
summary { color: var(--muted); cursor: pointer; }
label { display: block; margin: 0.85rem 0 0.35rem; color: var(--muted); font-size: 0.9rem; }
input, select {
  width: 100%; padding: 0.75rem 0.85rem; border-radius: 10px;
  border: 1px solid var(--line); background: rgba(0,0,0,0.25); color: var(--ink);
}
.cta {
  margin-top: 1.1rem; width: 100%; border: 0; border-radius: 999px;
  padding: 0.9rem 1rem; font-size: 1rem; font-weight: 700;
  background: var(--accent); color: var(--accent-ink); cursor: pointer;
}
.err { color: var(--bad); margin-top: 0.75rem; }
.okmsg { color: var(--ok); margin-top: 0.75rem; }
"""


def state_class(value: str) -> str:
    if value in ("active", "yes", "true"):
        return "ok"
    if value in ("activating", "inactive", "unknown"):
        return "wait"
    return "bad"


def page_setup(
    snap: dict[str, Any],
    *,
    error: str = "",
    done: bool = False,
    review_required: bool = False,
) -> bytes:
    rows = []
    units = snap.get("units") or {}
    for unit, st in units.items():
        short = unit.replace(".service", "").replace(".target", "").replace(".socket", "")
        rows.append(
            f'<div class="row"><span class="name">{short}</span>'
            f'<span class="state {state_class(st)}">{st}</span></div>'
        )
    marker = str(snap.get("first_boot_marker", "pending"))
    rows.append(
        '<div class="row"><span class="name">first boot marker</span>'
        f'<span class="state {state_class("active" if marker == "recorded" else "activating")}">{marker}</span></div>'
    )
    lemond_state = str((snap.get("lemond") or {}).get("state", "starting"))
    rows.append(
        '<div class="row"><span class="name">lemond health</span>'
        f'<span class="state {state_class("active" if lemond_state == "ready" else "activating")}">{lemond_state}</span></div>'
    )
    docker_state = str((snap.get("docker") or {}).get("state", "unavailable"))
    rows.append(
        '<div class="row"><span class="name">docker daemon</span>'
        f'<span class="state {state_class("active" if docker_state == "ready" else "failed")}">{docker_state}</span></div>'
    )
    ready_count = sum(st == "active" for st in units.values())
    ready_count += marker == "recorded"
    ready_count += lemond_state == "ready"
    ready_count += docker_state == "ready"
    total_count = len(units) + 3
    startup_percent = int(100 * ready_count / total_count) if total_count else 100
    # A missing completion marker means a genuinely fresh/incomplete setup.
    # Do not resurrect a stale JSON draft or browser-era appliance name.
    setup = load_setup() if setup_complete() else {}
    name = str(setup.get("name") or suggested_name())
    portal_url = f"http://{mdns_name(name)}.local/"
    # Prefer a previously confirmed timezone on review; otherwise leave UTC as a
    # placeholder and let the browser timezone script pick the real default.
    saved_timezone = str(setup.get("timezone") or "")
    selected_timezone = saved_timezone if saved_timezone in TIMEZONES else "UTC"
    timezone_options = "\n".join(
        f'<option value="{escape(timezone)}"'
        f'{" selected" if timezone == selected_timezone else ""}>{escape(timezone)}</option>'
        for timezone in TIMEZONES
    )
    err_html = f'<p class="err">{escape(error)}</p>' if error else ""
    review_html = f"""<section class="panel">
<h2>{"Review setup" if review_required else "Initial setup"}</h2>
<form method="POST" action="/setup" autocomplete="off">
<label for="name">Name</label>
<input id="name" name="name" maxlength="64" autocomplete="off" value="{escape(name, quote=True)}" required/>
<p class="lede" style="margin-top:0.75rem">Lemonseed will be available at <span id="portal-url">{escape(portal_url)}</span>.</p>
<label for="timezone">Timezone</label>
<select id="timezone" name="timezone" required>
{timezone_options}
</select>
<button class="cta" type="submit">{"Confirm and continue" if review_required else "Finish setup"}</button>
</form>
{err_html}{'<p class="okmsg">Settings saved. Waiting for services…</p>' if done else ""}
</section>"""
    redirect_url = f"http://{mdns_name(str(setup.get('name') or name))}.local/"
    # Manual escape hatch: mDNS propagation or a slow unit must not trap the user.
    continue_html = (
        '<p class="lede" style="margin-top:0.85rem">If this page does not continue on its own, open '
        f'<a style="color:var(--accent)" href="{escape(redirect_url, quote=True)}">{escape(redirect_url)}</a>.</p>'
        if setup_complete()
        else ""
    )
    boot_script = f"""
<script>
let redirectPending = false;
const portalUrl = {json.dumps(redirect_url)};
const waitForStartup = async () => {{
  try {{
    // Poll this origin: the new mDNS name may not resolve yet.
    const response = await fetch('/api/boot', {{cache: 'no-store'}});
    const status = await response.json();
    if (status.startup_ready && !redirectPending) {{
      redirectPending = true;
      setTimeout(() => location.href = portalUrl, 1500);
    }}
  }} catch (_) {{
    // The control plane itself may be restarting; try again.
  }}
}};
setInterval(waitForStartup, 2000);
waitForStartup();
</script>"""
    # Preserve any edits on a review form. The completed or regular boot view
    # may poll until all required services, including lemond health, are ready.
    poll_script = boot_script if done or not review_required else ""
    body = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Lemonseed setup</title><style>{STYLE}</style>
</head><body><main class="shell">
<p class="wordmark">Lemonseed</p>
<h1>{"Review setup" if review_required else "Starting Lemonseed"}</h1>
<p class="lede">{"Confirm the preserved appliance settings before continuing." if review_required else "Waiting for appliance services to become ready."}</p>
<section class="panel">
<h2>Startup</h2>
<p class="lede"><span id="startup-label">Starting services</span> · {ready_count} of {total_count} ready</p>
<div class="progress" role="progressbar" aria-labelledby="startup-label" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{startup_percent}"><span style="width:{startup_percent}%"></span></div>
<details>
<summary>Show service details</summary>
{''.join(rows)}
</details>
<p class="lede" style="margin-top:0.85rem">Version {snap.get("version","unknown")}</p>
{continue_html}
</section>
{review_html if review_required or not setup_complete() else ""}
{poll_script}
<script>
const nameInput = document.getElementById('name');
const portalUrl = document.getElementById('portal-url');
const timezoneSelect = document.getElementById('timezone');
if (nameInput && portalUrl) {{
  // Browsers may restore the previous form value after reinstalling at the
  // same URL. The server is authoritative for the initial value.
  nameInput.value = {json.dumps(name)};
  const updatePortalUrl = () => {{
    let label = nameInput.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 63).replace(/-+$/g, '');
    portalUrl.textContent = `http://${{label || 'lemonseed'}}.local/`;
  }};
  nameInput.addEventListener('input', updatePortalUrl);
  updatePortalUrl();
}}
if (timezoneSelect && !{json.dumps(bool(saved_timezone))}) {{
  try {{
    const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if ([...timezoneSelect.options].some(option => option.value === browserTimezone)) {{
      timezoneSelect.value = browserTimezone;
    }}
  }} catch (_) {{
    // Keep the server fallback (UTC) when Intl is unavailable.
  }}
}}
</script>
</main></body></html>"""
    return body.encode()


def page_ready(snap: dict[str, Any]) -> bytes:
    setup = load_setup()
    name = escape(str(setup.get("name") or "Lemonseed"))
    chat = snap.get("chat") or chat_ready.chat_snapshot()
    chat_state = str(chat.get("state") or "idle")
    display = escape(str(chat.get("display_name") or chat.get("model_id") or "—"))
    reason = escape(str(chat.get("reason") or chat.get("error") or chat.get("message") or ""))
    progress = int(chat.get("progress") or 0)
    if chat_state == "ready":
        chat_label = "ready"
        chat_class = "ok"
        chat_lede = f"{display} is loaded for local chat."
    elif chat_state == "preparing":
        chat_label = f"preparing ({progress}%)"
        chat_class = "wait"
        chat_lede = escape(str(chat.get("message") or "Preparing local chat…"))
    elif chat_state == "not-ready":
        chat_label = "not ready"
        chat_class = "bad"
        chat_lede = reason or "No suitable local model for this hardware."
    elif chat_state == "error":
        chat_label = "error"
        chat_class = "bad"
        chat_lede = reason or "Local chat failed to prepare."
    else:
        chat_label = chat_state
        chat_class = "wait"
        chat_lede = "Waiting to prepare local chat…"

    gpus = (snap.get("hardware") or {}).get("gpus") or []
    gpu_rows = ""
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        gpu_rows += (
            f'<div class="row"><span class="name">gpu {escape(str(gpu.get("index", 0)))}</span>'
            f'<span class="state ok">{escape(str(gpu.get("name")))} · '
            f'{escape(str(gpu.get("vram_gb")))} GiB</span></div>'
        )
    if not gpu_rows:
        gpu_rows = '<div class="row"><span class="name">gpu</span><span class="state wait">none detected</span></div>'

    body = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{name}</title><style>{STYLE}</style>
</head><body><main class="shell">
<p class="wordmark">Lemonseed</p>
<h1>{name}</h1>
<p class="lede">Appliance is online. Local inference runs on this machine; a household chat UI comes in a later milestone.</p>
<section class="panel">
<h2>Local chat</h2>
<div class="row"><span class="name">status</span><span class="state {chat_class}" id="chat-state">{chat_label}</span></div>
<div class="row"><span class="name">model</span><span class="state ok" id="chat-model">{display}</span></div>
<p class="lede" id="chat-lede">{chat_lede}</p>
</section>
<section class="panel">
<h2>Status</h2>
<div class="row"><span class="name">version</span><span class="state ok">{snap.get("version","unknown")}</span></div>
<div class="row"><span class="name">setup</span><span class="state ok">complete</span></div>
<div class="row"><span class="name">timezone</span><span class="state ok">{setup.get("timezone","UTC")}</span></div>
{gpu_rows}
</section>
<section class="panel">
<details>
<summary style="cursor:pointer;color:var(--muted)">Diagnostics</summary>
<p class="lede" id="chat-reason">{reason}</p>
<p class="lede"><a style="color:var(--accent)" href="/api/hardware">/api/hardware</a> ·
<a style="color:var(--accent)" href="/api/chat">/api/chat</a> ·
<a style="color:var(--accent)" href="/api/status">/api/status</a></p>
</details>
</section>
<script>
async function refreshChat() {{
  try {{
    const response = await fetch('/api/chat', {{cache: 'no-store'}});
    if (!response.ok) return;
    const chat = await response.json();
    const state = chat.state || 'idle';
    const model = chat.display_name || chat.model_id || '—';
    const progress = chat.progress || 0;
    let label = state;
    let cls = 'wait';
    let lede = chat.message || '';
    if (state === 'ready') {{ label = 'ready'; cls = 'ok'; lede = model + ' is loaded for local chat.'; }}
    else if (state === 'preparing') {{ label = 'preparing (' + progress + '%)'; cls = 'wait'; }}
    else if (state === 'not-ready') {{ label = 'not ready'; cls = 'bad'; lede = chat.reason || lede; }}
    else if (state === 'error') {{ label = 'error'; cls = 'bad'; lede = chat.error || chat.reason || lede; }}
    const stateEl = document.getElementById('chat-state');
    stateEl.textContent = label;
    stateEl.className = 'state ' + cls;
    document.getElementById('chat-model').textContent = model;
    document.getElementById('chat-lede').textContent = lede;
    document.getElementById('chat-reason').textContent = chat.reason || chat.error || '';
  }} catch (_err) {{}}
}}
setInterval(refreshChat, 3000);
</script>
</main></body></html>"""
    return body.encode()


def write_setup(payload: dict[str, Any]) -> None:
    SETUP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETUP_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SETUP_STATE_FILE)


def apply_identity(name_slug: str) -> None:
    """Apply and verify the persistent, live, and advertised hostname."""
    run_required(
        ["systemctl", "restart", "lemonseed-identity.service"],
        "apply the appliance hostname",
    )
    static_name = _read_text(Path("/etc/hostname"))
    live_name = os.uname().nodename
    if static_name != name_slug or live_name != name_slug:
        raise ValueError(
            "Could not apply the appliance hostname "
            f"(expected {name_slug}, /etc/hostname={static_name or 'empty'}, "
            f"live={live_name or 'empty'})"
        )

    run_required(
        ["systemctl", "restart", "avahi-daemon.service"],
        "restart mDNS discovery",
    )
    advertised = ""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        advertised = avahi_hostname()
        if advertised == name_slug:
            return
        time.sleep(0.25)
    raise ValueError(
        "mDNS did not advertise the selected name "
        f"(expected {name_slug}.local, got {(advertised or 'no name')}.local)"
    )


def complete_setup(name: str, timezone: str) -> None:
    name = name.strip()[:64]
    timezone = timezone.strip()[:64] or "UTC"
    if not name:
        raise ValueError("name required")
    if timezone not in TIMEZONES:
        raise ValueError("select a valid timezone")
    name_slug = mdns_name(name)
    if mdns_conflict(name_slug):
        raise ValueError(
            f"{name_slug}.local is already in use on this LAN; choose a different name"
        )
    payload = {
        "name": name,
        "mdns_name": name_slug,
        "timezone": timezone,
        "confirmed_version": appliance_version(),
        "completed_at": subprocess.check_output(
            ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], text=True
        ).strip(),
    }
    previous_setup = _read_text(SETUP_STATE_FILE)
    # Stage the desired identity for lemonseed-identity.service. Do not publish
    # the completion marker until static/live/mDNS names are all verified.
    write_setup(payload)
    try:
        apply_identity(name_slug)
    except ValueError:
        if previous_setup:
            try:
                write_setup(json.loads(previous_setup))
            except json.JSONDecodeError:
                SETUP_STATE_FILE.unlink(missing_ok=True)
        else:
            SETUP_STATE_FILE.unlink(missing_ok=True)
        raise

    run_best_effort(["timedatectl", "set-timezone", timezone])
    marker = SETUP_COMPLETE_FILE.with_suffix(".tmp")
    marker.write_text(payload["completed_at"] + "\n", encoding="utf-8")
    marker.replace(SETUP_COMPLETE_FILE)
    chat_ready.ensure_chat_worker()


class Handler(BaseHTTPRequestHandler):
    server_version = "LemonseedCP/0.3"

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Installer page on the previous origin (IP or .local) polls these
        # endpoints after reboot; allow that cross-origin fetch.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Lemonseed-Role", "control-plane")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, (json.dumps(obj) + "\n").encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/healthz", "/health"):
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if path == "/api/boot":
            self._json(200, boot_snapshot())
            return
        if path == "/api/hardware":
            report = hardware.collect_hardware()
            hardware.persist_hardware(report)
            self._json(200, report)
            return
        if path == "/api/chat":
            chat_ready.ensure_chat_worker()
            self._json(200, chat_ready.chat_snapshot())
            return
        if path == "/api/plan":
            report = hardware.collect_hardware()
            self._json(200, plan_model(report))
            return
        if path == "/api/data":
            data_services.ensure_provision_worker()
            self._json(200, data_services.data_snapshot())
            return
        if path == "/api/status":
            maybe_complete_mdns_handoff(self)
            chat_ready.ensure_chat_worker()
            data_services.ensure_provision_worker()
            boot = boot_snapshot()
            setup = load_setup()
            self._json(
                200,
                {
                    "status": "ok",
                    "role": "control-plane",
                    **boot,
                    "setup": setup,
                    "chat": chat_ready.chat_snapshot(),
                    "data": data_services.data_snapshot(),
                    "portal_url": f"http://{primary_mdns_slug()}.local/",
                },
            )
            return
        if path in ("/", "/index.html", "/setup"):
            if redirect_off_factory_mdns(self, path if path != "/index.html" else "/"):
                return
            snap = boot_snapshot()
            snap["chat"] = chat_ready.chat_snapshot()
            snap["hardware"] = hardware.collect_hardware()
            setup = load_setup()
            review_required = (
                not setup_complete()
                or setup.get("confirmed_version") != appliance_version()
            )
            if setup_complete() and snap["startup_ready"] and not review_required:
                chat_ready.ensure_chat_worker()
                self._send(200, page_ready(snap), "text/html; charset=utf-8")
            else:
                self._send(
                    200,
                    page_setup(snap, review_required=review_required),
                    "text/html; charset=utf-8",
                )
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path != "/setup":
            self._json(404, {"error": "not found"})
            return
        ctype = self.headers.get("Content-Type", "")
        try:
            if "application/json" in ctype:
                data = json.loads(raw.decode() or "{}")
            else:
                form = parse_qs(raw.decode())
                data = {k: (v[0] if v else "") for k, v in form.items()}
            complete_setup(
                str(data.get("name", "")),
                str(data.get("timezone", "UTC")),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(
                200,
                page_setup(boot_snapshot(), error=str(exc), review_required=True),
                "text/html; charset=utf-8",
            )
            return
        self._send(
            200,
            page_setup(boot_snapshot(), done=True),
            "text/html; charset=utf-8",
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main() -> None:
    chat_ready.ensure_chat_worker()
    data_services.ensure_provision_worker()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"appliance-control-plane listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
