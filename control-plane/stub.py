#!/usr/bin/env python3
"""Lemonseed control plane (bring-up + app shell).

Listens on 127.0.0.1:8080 behind nginx :80.
  - Before setup: no-auth first-boot / setup wizard
  - After setup: Lemonseed shell at / (toolbar + iframe → chat, settings, …)
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

import auth
import chat_ready
import data_services
import hardware
import modules
from catalog import plan_model


HOST = os.environ.get("APPLIANCE_CP_HOST", "127.0.0.1")
PORT = int(os.environ.get("APPLIANCE_CP_PORT", "8080"))
VERSION_FILE = Path("/run/appliance/version")
FIRST_BOOT_FILE = Path("/etc/appliance-first-boot")
SETUP_COMPLETE_FILE = Path("/etc/appliance-setup-complete")
SETUP_STATE_FILE = Path("/etc/appliance-setup.json")
HANDOFF_DONE_FILE = Path("/run/lemonseed/mdns-handoff-done")
LEMOND_ENV_FILE = Path("/var/lib/lemonseed/lemonade/lemond.env")
# Brand icons: packaged under control-plane/static/brand, or repo assets/brand.
_BRAND_CANDIDATES = (
    Path(__file__).resolve().parent / "static" / "brand",
    Path(__file__).resolve().parent.parent / "assets" / "brand",
)
BRAND_DIR = next((p for p in _BRAND_CANDIDATES if p.is_dir()), _BRAND_CANDIDATES[0])
BRAND_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}
TIMEZONES = sorted(
    ["UTC"]
    + [
        timezone
        for timezone in available_timezones()
        if "/" in timezone and not timezone.startswith(("Etc/", "posix/", "right/"))
    ]
)


def brand_head_links() -> str:
    """Favicon + apple-touch links for HTML pages."""
    return (
        '<link rel="icon" href="/_lemonseed/brand/favicon.ico" sizes="any"/>'
        '<link rel="icon" type="image/png" href="/_lemonseed/brand/lemon-32.png" sizes="32x32"/>'
        '<link rel="apple-touch-icon" href="/_lemonseed/brand/apple-touch-icon.png"/>'
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
.brand-mark {
  display: flex; align-items: center; gap: 0.65rem; margin-bottom: 0.15rem;
}
.brand-mark .logo {
  display: block; width: 3.25rem; height: 3.25rem; margin: 0;
  border-radius: 50%; object-fit: cover; flex-shrink: 0;
  box-shadow: 0 0 0 1px rgba(255,255,255,0.08);
}
.brand-mark .wordmark {
  font-size: clamp(1.35rem, 5vw, 1.65rem); font-weight: 700;
  letter-spacing: 0.14em; color: var(--ink);
}
.logo {
  display: block; width: 3.25rem; height: 3.25rem; margin: 0;
  border-radius: 50%; object-fit: cover;
  box-shadow: 0 0 0 1px rgba(255,255,255,0.08);
}
h1 { margin: 0.55rem 0 0.45rem; font-size: clamp(2rem, 8vw, 2.5rem); letter-spacing: -0.03em; }
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
.app-shell {
  max-width: none; margin: 0; padding: 0; min-height: 100vh;
  display: flex; flex-direction: column;
}
.app-bar {
  display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
  padding: 0.55rem 1rem; border-bottom: 1px solid var(--line);
  background: rgba(15, 28, 24, 0.92); backdrop-filter: blur(8px);
}
.app-bar .brand-logo {
  display: block; width: 1.75rem; height: 1.75rem; margin: 0;
  border-radius: 50%; object-fit: cover; flex-shrink: 0;
  box-shadow: 0 0 0 1px rgba(255,255,255,0.1);
}
.app-bar .title { margin: 0; font-size: 1rem; font-weight: 600; }
.app-bar .spacer { flex: 1; }
.app-bar .nav {
  display: flex; align-items: center; gap: 0.15rem;
}
.app-bar .nav a, .app-bar .nav span {
  color: var(--muted); text-decoration: none; font-size: 0.88rem; font-weight: 600;
  padding: 0.35rem 0.65rem; border-radius: 999px;
}
.app-bar .nav a:hover { color: var(--accent); background: rgba(255,255,255,0.06); }
.app-bar .nav a.active { color: var(--accent-ink); background: var(--accent); }
.app-bar .nav .disabled {
  opacity: 0.45; cursor: default; pointer-events: none;
}
.app-bar .metrics {
  display: none; align-items: center; gap: 0.85rem;
}
@media (min-width: 720px) { .app-bar .metrics { display: flex; } }
.metric { min-width: 4.75rem; }
.metric label {
  display: block; font-size: 0.62rem; letter-spacing: 0.06em;
  text-transform: uppercase; color: var(--muted); margin-bottom: 0.2rem;
}
.metric .bar {
  height: 4px; border-radius: 999px; background: rgba(255,255,255,0.12); overflow: hidden;
}
.metric .bar > span {
  display: block; height: 100%; background: linear-gradient(90deg, #7dcf6a, var(--accent));
  transition: width 0.35s ease;
}
.app-bar .gear {
  color: var(--muted); text-decoration: none; font-size: 1.15rem;
  line-height: 1; padding: 0.25rem 0.45rem; border-radius: 8px;
}
.app-bar .gear:hover, .app-bar .gear.active { color: var(--accent); }
.app-bar a {
  color: var(--muted); text-decoration: none; font-size: 0.9rem; font-weight: 600;
}
.app-bar a:hover { color: var(--accent); }
.embed-shell {
  max-width: 42rem; margin: 0 auto; padding: 1.5rem 1rem 2.5rem;
}
.embed-shell h1 {
  margin: 0 0 1rem; font-size: clamp(1.5rem, 5vw, 2rem); letter-spacing: -0.02em;
}
.frame-wrap iframe.hidden {
  visibility: hidden;
  pointer-events: none;
  z-index: 0;
}
.frame-wrap { flex: 1; min-height: 0; position: relative; background: #0a1210; }
.frame-wrap iframe {
  position: absolute; inset: 0; width: 100%; height: 100%; border: 0; z-index: 1;
}
.frame-fallback {
  position: absolute; inset: 0; display: flex; align-items: center;
  justify-content: center; padding: 2rem; text-align: center; color: var(--muted);
  z-index: 2;
}
.frame-fallback.hidden { display: none; }
.landing {
  max-width: 28rem; margin: 0 auto; padding: 1.5rem;
  border: 1px solid var(--line); border-radius: 16px;
  background: rgba(15, 28, 24, 0.72);
  text-align: left;
}
.landing h2 {
  margin: 0 0 0.5rem; font-size: 1.25rem; color: var(--ink); letter-spacing: -0.02em;
}
.landing p { margin: 0; color: var(--muted); line-height: 1.45; }
.landing .status-line {
  margin-top: 1rem; font-weight: 700; font-variant-numeric: tabular-nums;
}
.landing .status-line.ok { color: var(--ok); }
.landing .status-line.wait { color: var(--wait); }
.landing .status-line.bad { color: var(--bad); }
.frame-wrap iframe.hidden { display: none; }
.frame-fallback.hidden { display: none; }
.login-page {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: 1.5rem;
}
.login-card {
  width: 100%; max-width: 24rem; padding: 2rem 1.75rem 2.25rem;
  border: 1px solid var(--line); border-radius: 16px;
  background: rgba(15, 28, 24, 0.72);
  text-align: center;
}
.login-card .brand-logo {
  display: block; width: 3.25rem; height: 3.25rem; margin: 0 auto 0.5rem;
  border-radius: 50%; object-fit: cover;
  box-shadow: 0 0 0 1px rgba(255,255,255,0.1);
}
.login-card h1 { font-size: 1.75rem; margin: 0.25rem 0 0.2rem; }
.login-card .lede { margin-bottom: 0.25rem; }
.login-card .field { text-align: left; }
.login-error { color: var(--bad); margin: 0.75rem 0 0; }
.btn {
  margin-top: 1.25rem; width: 100%; border: 0; border-radius: 999px;
  padding: 0.85rem 1rem; font-size: 1rem; font-weight: 700; cursor: pointer;
  background: var(--accent); color: var(--accent-ink);
}
.btn.primary { background: var(--accent); color: var(--accent-ink); }
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
    admin_reset: bool = False,
    auth_admin_required: bool = False,
    auth_active: bool = False,
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
    show_form = review_required or admin_reset or not setup_complete()
    heading = (
        "Reset admin credentials" if admin_reset else
        "Review setup" if review_required else "Initial setup"
    )
    submit_label = (
        "Reset and continue" if admin_reset else
        "Confirm and continue" if review_required else "Finish setup"
    )
    admin_fields = ""
    if auth_admin_required:
        admin_fields = f"""
<label for="admin_username">Admin username</label>
<input id="admin_username" name="admin_username" maxlength="64" autocomplete="off" value="admin" required/>
<label for="admin_password">Admin password</label>
<input id="admin_password" name="admin_password" type="password" minlength="8" maxlength="128" autocomplete="new-password" required/>
"""
    review_html = f"""<section class="panel">
<h2>{heading}</h2>
<form method="POST" action="/setup" autocomplete="off">
<label for="name">Name</label>
<input id="name" name="name" maxlength="64" autocomplete="off" value="{escape(name, quote=True)}" required/>
<p class="lede" style="margin-top:0.75rem">Lemonseed will be available at <span id="portal-url">{escape(portal_url)}</span>.</p>
<label for="timezone">Timezone</label>
<select id="timezone" name="timezone" required>
{timezone_options}
</select>{admin_fields}
<button class="cta" type="submit">{submit_label}</button>
</form>
{err_html}{'<p class="okmsg">Settings saved. Waiting for services…</p>' if done else ""}
</section>"""
    # Once auth is active the portal is gated; land on the CP login page so
    # the browser establishes a session before the chat frame loads (M6.2).
    # rd=/ is a same-origin relative target the login handler echoes back.
    base_url = f"http://{mdns_name(str(setup.get('name') or name))}.local"
    redirect_url = f"{base_url}/login?rd=%2F" if auth_active else f"{base_url}/"
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
    poll_script = boot_script if done or not (review_required or admin_reset) else ""
    body = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Lemonseed setup</title>{brand_head_links()}<style>{STYLE}</style>
</head><body><main class="shell">
<div class="brand-mark">
<img class="logo" src="/_lemonseed/brand/lemon-128.png" width="52" height="52" alt=""/>
<span class="wordmark">LEMONSEED</span>
</div>
<h1>{"Reset admin credentials" if admin_reset else "Review setup" if review_required else "Starting Lemonseed"}</h1>
<p class="lede">{"This appliance was reinstalled with a pending admin reset. Choose the new admin account; other users and data are preserved." if admin_reset else "Confirm the preserved appliance settings before continuing." if review_required else "Waiting for appliance services to become ready."}</p>
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
{review_html if show_form else ""}
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


def page_login(*, rd: str = "/", error: str = "") -> bytes:
    """Branded login page (replaces Authelia's unbranded sign-in)."""
    setup = load_setup()
    name = escape(str(setup.get("name") or "Lemonseed"))
    error_html = f'<p class="login-error">{escape(error)}</p>' if error else ""
    rd_hidden = f'<input type="hidden" name="rd" value="{escape(rd)}"/>'
    body = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{name} — Sign in</title>{brand_head_links()}<style>{STYLE}</style>
</head><body>
<main class="login-page">
<div class="login-card">
<img class="brand-logo" src="/_lemonseed/brand/lemon-64.png" width="48" height="48" alt=""/>
<h1>{name}</h1>
<p class="lede">Sign in to continue</p>
{error_html}
<form id="login-form" method="post" action="/login">
{rd_hidden}
<div class="field"><label for="username">Username</label><input id="username" name="username" type="text" autocomplete="username" required autofocus/></div>
<div class="field"><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required/></div>
<button type="submit" class="btn primary">Sign in</button>
</form>
</div>
</main>
<script>
// Submit as JSON (the CP expects application/json).
document.getElementById('login-form').addEventListener('submit', async (ev) => {{
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const body = Object.fromEntries(fd.entries());
  const res = await fetch('/login', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(body),
  }});
  if (res.redirected || res.status === 302) {{
    const loc = res.headers.get('Location') || '/';
    location.href = loc;
    return;
  }}
  if (res.status === 401) {{
    const err = document.createElement('p');
    err.className = 'login-error';
    err.textContent = 'Invalid username or password.';
    document.querySelector('.login-card').insertBefore(err, document.querySelector('form'));
  }}
}});
</script>
</body></html>"""
    return body.encode()


def page_shell(snap: dict[str, Any], *, view: str = "chat") -> bytes:
    """Post-setup app shell: persistent toolbar + keep-alive iframes."""
    setup = load_setup()
    name = escape(str(setup.get("name") or "Lemonseed"))
    mods = snap.get("modules") or modules.modules_snapshot()
    _owui = mods.get("open_webui") or {}
    if view not in ("chat", "settings", "modules"):
        view = "chat"

    chat_active = "active" if view == "chat" else ""
    gear_active = "active" if view == "settings" else ""
    # Chat frame stays blank until healthy; panel frame holds settings/modules.
    chat_frame_class = "hidden"
    panel_frame_class = "" if view == "settings" else "hidden"
    panel_src = "/settings/content/" if view == "settings" else "about:blank"
    landing_class = "" if view == "chat" else "hidden"

    body = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{name}</title>{brand_head_links()}<style>{STYLE}</style>
</head><body>
<main class="app-shell">
<header class="app-bar">
<img class="brand-logo" src="/_lemonseed/brand/lemon-64.png" width="28" height="28" alt=""/>
<p class="title">{name}</p>
<nav class="nav" aria-label="Main">
  <a href="/" id="nav-chat" class="{chat_active}" data-view="chat">Chat</a>
  <span class="disabled" title="Coming soon">Modules</span>
</nav>
<span class="spacer"></span>
<div class="metrics" aria-hidden="true">
  <div class="metric" title="System load (1 min)">
    <label>Load</label>
    <div class="bar"><span id="metric-load" style="width:0%"></span></div>
  </div>
  <div class="metric" title="Storage used on /var/lib/lemonseed">
    <label>Storage</label>
    <div class="bar"><span id="metric-storage" style="width:0%"></span></div>
  </div>
</div>
<a href="/settings/" id="nav-settings" class="gear {gear_active}" title="Settings" data-view="settings">⚙</a>
<button id="nav-logout" class="gear" title="Log out" aria-label="Log out">⎋</button>
</header>
<div class="frame-wrap">
<div class="frame-fallback {landing_class}" id="chat-fallback">
  <div class="landing">
    <h2 id="landing-title">Getting Ready</h2>
    <p id="landing-lede">Preparing this appliance…</p>
    <p class="status-line wait" id="landing-status">Starting…</p>
  </div>
</div>
<iframe id="chat-frame" class="{chat_frame_class}" title="Chat" src="about:blank" allow="clipboard-read; clipboard-write"></iframe>
<iframe id="panel-frame" class="{panel_frame_class}" title="Settings" src="{panel_src}"></iframe>
</div>
</main>
<script>
let currentView = {json.dumps(view)};
let chatBootstrapped = false;
let readyStreak = 0;
const CHAT_SRC = '/?embed=1';
const SETTINGS_SRC = '/settings/content/';

function landingCopy(chat, owui) {{
  const chatState = chat.state || 'idle';
  const ui = owui.state || 'idle';
  const httpReady = !!owui.http_ready;
  const modelReady = chatState === 'ready';
  const uiReady = ui === 'ready' && httpReady;

  if (chatState === 'rebooting') {{
    return {{
      title: 'Rebooting',
      workflow: 'Applying GPU memory settings',
      task: 'The appliance is rebooting — this page will reconnect automatically…',
      cls: 'wait',
    }};
  }}
  if (chatState === 'error' || ui === 'error') {{
    const err = chat.error || owui.error || chat.reason || owui.reason || 'Something went wrong. Open Settings for diagnostics.';
    return {{
      title: 'Getting Ready',
      workflow: chat.workflow || owui.workflow || 'Local chat unavailable',
      task: chat.task || owui.task || String(err),
      cls: 'bad',
    }};
  }}
  if (modelReady && uiReady) {{
    return {{
      title: 'Getting Ready',
      workflow: 'Ready',
      task: 'Opening chat…',
      cls: 'ok',
    }};
  }}
  // Prefer model progress when the LLM is still preparing.
  if (!modelReady && chatState && chatState !== 'idle' && chatState !== 'ready') {{
    return {{
      title: 'Getting Ready',
      workflow: chat.workflow || 'Downloading an initial LLM',
      task: chat.task || chat.message || 'Preparing a local model…',
      cls: 'wait',
    }};
  }}
  if (!uiReady) {{
    return {{
      title: 'Getting Ready',
      workflow: owui.workflow || 'Configuring chat',
      task: owui.task || owui.reason || owui.message || 'Preparing the chat interface…',
      cls: 'wait',
    }};
  }}
  return {{
    title: 'Getting Ready',
    workflow: chat.workflow || 'Downloading an initial LLM',
    task: chat.task || chat.message || 'Finishing setup…',
    cls: 'wait',
  }};
}}

function showChatFrame() {{
  const chat = document.getElementById('chat-frame');
  const panel = document.getElementById('panel-frame');
  const fallback = document.getElementById('chat-fallback');
  panel.classList.add('hidden');
  if (chatBootstrapped) {{
    fallback.classList.add('hidden');
    chat.classList.remove('hidden');
  }} else {{
    chat.classList.add('hidden');
    fallback.classList.remove('hidden');
  }}
}}

function showPanelFrame(src) {{
  const chat = document.getElementById('chat-frame');
  const panel = document.getElementById('panel-frame');
  const fallback = document.getElementById('chat-fallback');
  // Keep chat mounted (hidden) so OWUI / websockets stay alive.
  chat.classList.add('hidden');
  fallback.classList.add('hidden');
  panel.classList.remove('hidden');
  if (src && panel.src !== new URL(src, location.origin).href) {{
    panel.src = src;
  }}
}}

function setView(name, pushHistory) {{
  currentView = name;
  document.getElementById('nav-chat').classList.toggle('active', name === 'chat');
  document.getElementById('nav-settings').classList.toggle('active', name === 'settings');
  if (name === 'chat') {{
    showChatFrame();
    if (pushHistory) history.pushState({{view: name}}, '', '/');
  }} else if (name === 'settings') {{
    showPanelFrame(SETTINGS_SRC);
    if (pushHistory) history.pushState({{view: name}}, '', '/settings/');
  }}
}}

function ensureChatLoaded() {{
  if (chatBootstrapped) return;
  chatBootstrapped = true;
  const chat = document.getElementById('chat-frame');
  chat.src = CHAT_SRC;
  if (currentView === 'chat') showChatFrame();
}}

async function gatewayReachable() {{
  // End-to-end check through nginx (same path the SPA hits after embed).
  try {{
    const response = await fetch('/_app/version.json', {{cache: 'no-store'}});
    return response.ok;
  }} catch (_err) {{
    return false;
  }}
}}

document.getElementById('nav-chat').addEventListener('click', (ev) => {{
  ev.preventDefault();
  setView('chat', true);
}});
document.getElementById('nav-settings').addEventListener('click', (ev) => {{
  ev.preventDefault();
  setView('settings', true);
}});
document.getElementById('nav-logout').addEventListener('click', async () => {{
  // Clear the session cookie, then land on the login page.
  try {{
    const res = await fetch('/logout', {{method: 'POST'}});
    if (res.redirected) {{
      location.href = res.headers.get('Location') || '/login';
      return;
    }}
  }} catch (_) {{}}
  location.href = '/login';
}});
window.addEventListener('popstate', () => {{
  const path = location.pathname.replace(/\\/$/, '') || '/';
  setView(path === '/settings' ? 'settings' : 'chat', false);
}});

async function refreshStatus() {{
  try {{
    const response = await fetch('/api/status', {{cache: 'no-store'}});
    if (!response.ok) return;
    const data = await response.json();
    const telemetry = data.telemetry || {{}};
    const loadEl = document.getElementById('metric-load');
    const storageEl = document.getElementById('metric-storage');
    if (loadEl && telemetry.load_pct != null) loadEl.style.width = telemetry.load_pct + '%';
    if (storageEl && telemetry.storage_pct != null) storageEl.style.width = telemetry.storage_pct + '%';

    const owui = (data.modules || {{}}).open_webui || {{}};
    const chat = data.chat || {{}};
    const ui = owui.state || 'idle';
    const httpReady = !!owui.http_ready;
    const modelReady = chat.state === 'ready';
    const copy = landingCopy(chat, owui);
    const title = document.getElementById('landing-title');
    const lede = document.getElementById('landing-lede');
    const status = document.getElementById('landing-status');
    if (title) title.textContent = copy.title;
    if (lede) lede.textContent = copy.workflow;
    if (status) {{
      status.textContent = copy.task;
      status.className = 'status-line ' + copy.cls;
    }}
    if (ui === 'ready' && httpReady && modelReady && await gatewayReachable()) {{
      readyStreak += 1;
    }} else {{
      readyStreak = 0;
    }}
    // Two consecutive healthy polls: OWUI + model verified + nginx asset path.
    if (!chatBootstrapped && readyStreak >= 2) {{
      ensureChatLoaded();
    }}
  }} catch (_err) {{}}
}}
setInterval(refreshStatus, 5000);
refreshStatus();
if (currentView === 'settings') showPanelFrame(SETTINGS_SRC);
</script>
</body></html>"""
    return body.encode()


def telemetry_snapshot() -> dict[str, Any]:
    """Lightweight load + storage bars for the app toolbar."""
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    cpu = hardware._cpu_info()
    threads = max(1, int(cpu.get("threads") or cpu.get("cores") or 1))
    load_pct = min(100, round(100 * load1 / threads))
    storage = hardware._disk_free(Path("/var/lib/lemonseed"))
    total = int(storage.get("total_bytes") or 0)
    used = int(storage.get("used_bytes") or 0)
    storage_pct = round(100 * used / total) if total else 0
    return {
        "load_pct": load_pct,
        "load": {"1m": round(load1, 2), "5m": round(load5, 2), "15m": round(load15, 2)},
        "storage_pct": storage_pct,
        "storage_path": "/var/lib/lemonseed",
    }


def page_settings_content(snap: dict[str, Any]) -> bytes:
    setup = load_setup()
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

    mods = snap.get("modules") or modules.modules_snapshot()
    owui = mods.get("open_webui") or {}
    owui_state = escape(str(owui.get("state") or "idle"))
    owui_class = "ok" if owui.get("state") == "ready" else ("bad" if owui.get("state") == "error" else "wait")

    body = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Settings</title>{brand_head_links()}<style>{STYLE}</style>
</head><body><main class="embed-shell">
<h1>Settings</h1>
<section class="panel">
<h2>Chat</h2>
<div class="row"><span class="name">open webui</span><span class="state {owui_class}">{owui_state}</span></div>
<div class="row"><span class="name">path</span><span class="state ok">/?embed=1</span></div>
</section>
<section class="panel">
<h2>Local inference</h2>
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
<a style="color:var(--accent)" href="/api/modules">/api/modules</a> ·
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


def complete_setup(
    name: str, timezone: str, admin_username: str = "", admin_password: str = ""
) -> None:
    name = name.strip()[:64]
    timezone = timezone.strip()[:64] or "UTC"
    if not name:
        raise ValueError("name required")
    if timezone not in TIMEZONES:
        raise ValueError("select a valid timezone")
    admin_username = (admin_username or "").strip()
    if auth.admin_bootstrap_required() and (not admin_username or not admin_password):
        raise ValueError("admin username and password are required")
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
    if admin_username and admin_password:
        # Synchronous first-admin bootstrap: waits bounded for Postgres, then
        # stores the bcrypt hash, generates the session secret, and flips the gate.
        auth.create_admin(admin_username, admin_password)
    marker = SETUP_COMPLETE_FILE.with_suffix(".tmp")
    marker.write_text(payload["completed_at"] + "\n", encoding="utf-8")
    marker.replace(SETUP_COMPLETE_FILE)
    chat_ready.ensure_chat_worker()
    modules.ensure_module_worker()


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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Client went away mid-response (reboot, tab close, nginx abort).
            # socketserver would otherwise dump a full traceback per occurrence.
            self.log_message("client disconnected during response (%s)", self.path)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, (json.dumps(obj) + "\n").encode(), "application/json")

    def _require_admin(self) -> bool:
        """Return True (and send 403) if the caller is not in the admin group.

        nginx sets X-Forwarded-Groups from the CP session-verify response
        headers (via auth_request_set + proxy_set_header). When the auth gate
        is disabled (pre-admin) the header is absent, which is fine: setup is
        not yet complete and /settings/ redirects to /setup anyway.
        """
        groups = self.headers.get("X-Forwarded-Groups", "")
        parts = {g.strip() for g in groups.split(",") if g.strip()}
        if "admin" not in parts:
            self._send(403, b"forbidden\n", "text/plain; charset=utf-8")
            return False
        return True

    def _parse_cookie(self, name: str) -> Optional[str]:
        """Extract a cookie value from the request headers."""
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            part = part.strip()
            if part.startswith(name + "="):
                return part[len(name) + 1 :]
        return None

    def _handle_session_verify(self) -> None:
        """nginx auth_request target: verify the session cookie.

        Returns 200 + X-Forwarded-* headers on success, 401 on failure.
        """
        token = self._parse_cookie(auth.SESSION_COOKIE)
        if not token:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = auth.verify_token(token)
        if data is None:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("X-Forwarded-User", data.get("u", ""))
        self.send_header("X-Forwarded-Email", data.get("e", ""))
        self.send_header("X-Forwarded-Groups", ",".join(data.get("g", [])))
        self.send_header("X-Forwarded-Name", data.get("n", "") or data.get("u", ""))
        self.send_header("X-Forwarded-Role", auth.role(data.get("g", [])))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_login_get(self, query: str) -> None:
        """GET /login — branded login page."""
        if setup_complete() and not auth.admin_bootstrap_required():
            # Auth is active: show the login form.
            rd = ""
            if query:
                params = parse_qs(query)
                rd = params.get("rd", [""])[0]
            body = page_login(rd=rd)
            self._send(200, body, "text/html; charset=utf-8")
            return
        # Pre-setup or admin reset: redirect to /setup.
        self.send_response(302)
        self.send_header("Location", "/setup")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_login_post(self, raw: bytes) -> None:
        """POST /login — verify credentials, issue session token."""
        try:
            data = json.loads(raw.decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send(400, b"bad request\n", "text/plain; charset=utf-8")
            return
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        rd = str(data.get("rd", "/"))

        # Rate limiting by source IP.
        ip = self.client_address[0] if self.client_address else "unknown"
        if not auth.rate_limit_check(ip):
            self._send(401, b"invalid username or password\n", "text/plain; charset=utf-8")
            return

        user = auth.verify_credentials(username, password)
        if user is None:
            auth.rate_limit_record_failure(ip)
            self._send(401, b"invalid username or password\n", "text/plain; charset=utf-8")
            return

        auth.rate_limit_reset(ip)
        token = auth.issue_token(
            user["username"],
            user.get("email", ""),
            user.get("groups", []),
            user.get("display_name", ""),
        )

        # Sanitize the redirect target: must be a same-origin relative path.
        if not rd.startswith("/") or rd.startswith("//"):
            rd = "/"

        self.send_response(302)
        self.send_header("Location", rd)
        cookie = f"{auth.SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
        self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_logout(self) -> None:
        """POST /logout — clear the session cookie."""
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", f"{auth.SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/healthz", "/health"):
            self._send(200, b"ok\n", "text/plain; charset=utf-8")
            return
        if path.startswith("/_lemonseed/brand/"):
            rel = path[len("/_lemonseed/brand/") :]
            if not rel or ".." in rel or rel.startswith("/") or "/" in rel:
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
                return
            fp = BRAND_DIR / rel
            if not fp.is_file():
                self._send(404, b"not found\n", "text/plain; charset=utf-8")
                return
            ctype = BRAND_MIME.get(fp.suffix.lower(), "application/octet-stream")
            body = fp.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/favicon.ico":
            fp = BRAND_DIR / "favicon.ico"
            if fp.is_file():
                body = fp.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/x-icon")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(body)
                return
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
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
        if path == "/api/modules":
            data_services.ensure_provision_worker()
            modules.ensure_module_worker()
            self._json(200, modules.modules_snapshot())
            return
        if path == "/api/status":
            maybe_complete_mdns_handoff(self)
            chat_ready.ensure_chat_worker()
            data_services.ensure_provision_worker()
            modules.ensure_module_worker()
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
                    "modules": modules.modules_snapshot(),
                    "auth": auth.auth_status(),
                    "telemetry": telemetry_snapshot(),
                    "portal_url": f"http://{primary_mdns_slug()}.local/",
                },
            )
            return
        if path in ("/settings/content", "/settings/content/"):
            if redirect_off_factory_mdns(self, "/settings/"):
                return
            if not setup_complete() or auth.reset_marker_present():
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._require_admin():
                return
            snap = boot_snapshot()
            snap["chat"] = chat_ready.chat_snapshot()
            snap["hardware"] = hardware.collect_hardware()
            snap["modules"] = modules.modules_snapshot()
            self._send(200, page_settings_content(snap), "text/html; charset=utf-8")
            return
        if path in ("/settings", "/settings/"):
            if redirect_off_factory_mdns(self, "/settings/"):
                return
            if not setup_complete() or auth.reset_marker_present():
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._require_admin():
                return
            snap = boot_snapshot()
            snap["chat"] = chat_ready.chat_snapshot()
            snap["hardware"] = hardware.collect_hardware()
            snap["modules"] = modules.modules_snapshot()
            chat_ready.ensure_chat_worker()
            modules.ensure_module_worker()
            self._send(200, page_shell(snap, view="settings"), "text/html; charset=utf-8")
            return
        if path == "/login":
            self._handle_login_get(urlparse(self.path).query)
            return
        if path == "/_lemonseed/session-verify":
            self._handle_session_verify()
            return
        if path in ("/", "/index.html", "/setup"):
            if redirect_off_factory_mdns(self, path if path != "/index.html" else "/"):
                return
            snap = boot_snapshot()
            snap["chat"] = chat_ready.chat_snapshot()
            snap["hardware"] = hardware.collect_hardware()
            snap["data"] = data_services.data_snapshot()
            snap["modules"] = modules.modules_snapshot()
            setup = load_setup()
            # The review form (name + timezone + admin creds) is a first-boot
            # concern. On an A/B update the version changes, so a version
            # mismatch must NOT re-prompt an already-configured appliance —
            # only a genuinely incomplete setup does.
            review_required = not setup_complete()
            admin_reset = auth.reset_marker_present()
            if setup_complete() and not review_required and not admin_reset:
                chat_ready.ensure_chat_worker()
                modules.ensure_module_worker()
                # Shell is available even while startup_ready is still warming up.
                self._send(200, page_shell(snap), "text/html; charset=utf-8")
            else:
                self._send(
                    200,
                    page_setup(
                        snap,
                        review_required=review_required,
                        admin_reset=admin_reset,
                        auth_admin_required=auth.admin_bootstrap_required(),
                    ),
                    "text/html; charset=utf-8",
                )
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if path == "/login":
            self._handle_login_post(raw)
            return
        if path == "/logout":
            self._handle_logout()
            return
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
                str(data.get("admin_username", "")),
                str(data.get("admin_password", "")),
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self._send(
                200,
                page_setup(
                    boot_snapshot(),
                    error=str(exc),
                    review_required=True,
                    admin_reset=auth.reset_marker_present(),
                    auth_admin_required=auth.admin_bootstrap_required(),
                ),
                "text/html; charset=utf-8",
            )
            return
        except Exception as exc:  # noqa: BLE001 — service boundary: log + 500
            import traceback

            traceback.print_exc()
            self._json(500, {"error": f"internal error: {exc}"})
            return
        self._send(
            200,
            page_setup(boot_snapshot(), done=True, auth_active=auth.admin_exists() is True),
            "text/html; charset=utf-8",
        )

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def main() -> None:
    # A reboot-pending marker from the previous boot is stale by definition
    # (we are up); clear it so the portal doesn't show "Rebooting…" forever.
    chat_ready.clear_reboot_pending()
    chat_ready.ensure_chat_worker()
    data_services.ensure_provision_worker()
    modules.ensure_module_worker()
    auth.ensure_auth_gate_worker()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"appliance-control-plane listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
