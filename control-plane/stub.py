#!/usr/bin/env python3
"""Lemonseed control plane (bring-up + app shell).

Listens on 127.0.0.1:8080 behind nginx :80.
  - Before setup: no-auth first-boot / setup wizard
  - After setup: Lemonseed shell at / (toolbar + iframe → chat, settings, …)
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from zoneinfo import available_timezones

import auth
import catalog
import chat_ready
import data_services
import hardware
import modules
import owui_policy
import owui_portal
import sessions
from catalog import plan_model

HOST = os.environ.get("APPLIANCE_CP_HOST", "127.0.0.1")
PORT = int(os.environ.get("APPLIANCE_CP_PORT", "8080"))
VERSION_FILE = Path("/run/appliance/version")
FIRST_BOOT_FILE = Path("/etc/appliance-first-boot")
SETUP_COMPLETE_FILE = Path("/etc/appliance-setup-complete")
SETUP_STATE_FILE = Path("/etc/appliance-setup.json")
HANDOFF_DONE_FILE = Path("/run/lemonseed/mdns-handoff-done")
LEMOND_ENV_FILE = Path("/var/lib/lemonseed/lemonade/lemond.env")
# One status, two meanings, two body shapes (M6.6): 403 from an OWUI guard is
# "Lemonseed owns this setting" and carries lemonseed=managed-by-lemonseed;
# 403 from the CP is "yours to see, not to do" and carries needs-admin. The
# client keys off the marker, never off the status alone.
NEEDS_ADMIN_MARKER = "needs-admin"
# The portal labels its own requests so the edge can answer an XHR with a real
# 401 instead of a 302 that fetch() would follow into login HTML at HTTP 200.
XHR_HEADER = "X-Requested-With"
XHR_MARKER = "fetch"
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
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"{cmd[0]}: {exc}", flush=True)
        return
    if result.returncode != 0:
        print(f"{cmd[0]} failed ({result.returncode}): {result.stderr.strip()}", flush=True)


def run_required(cmd: list[str], action: str) -> None:
    """Run an identity-critical command or fail setup with a useful message."""
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=15)
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


def status_public() -> dict[str, Any]:
    """The half of the status surface an unauthenticated client may see (M6.6 step 5).

    Exactly two clients reach the appliance before anyone signs in, and each
    needs three things:

    * the installer's post-reboot handoff recognises the box by ``role`` and
      finds the portal through ``portal_url`` / ``setup.mdns_name``;
    * the gateway's not-ready page reloads once ``status`` says ``ok``.

    Everything else moved to ``/api/system`` behind the session gate: the full
    snapshot carries household facts (account counts, telemetry, data-plane and
    module state), which any device on the LAN could read before this split.
    It also ran seven ``systemctl`` probes plus a Docker and a lemond query on
    every poll, so an unauthenticated poller could make the appliance do real
    work; spawning the background workers is now a signed-in caller's job (they
    are started unconditionally at CP startup regardless).

    Keys are listed rather than subtracted from the full snapshot on purpose.
    The full status grows over time, and a filter would leak every new field by
    default until somebody remembered to subtract it.
    """
    setup = load_setup()
    return {
        "status": "ok",
        "role": "control-plane",
        "version": appliance_version(),
        "setup_complete": setup_complete(),
        "setup": {"mdns_name": setup.get("mdns_name")},
        "portal_url": f"http://{primary_mdns_slug()}.local/",
    }


# Every control-plane call the portal makes goes through portalFetch() below
# (M6.6 step 4). The contract it implements is the reason it exists: fifteen
# hand-written fetch() calls each invented their own answer to a dead session,
# and the one that looked correct was unreachable. Twelve of them were
# control-plane calls; the three that remain outside are noted where they are.
#
#   401  not signed in       -> navigate the TOP frame to /login, then bail.
#                               The portal lives in an iframe, so a same-frame
#                               redirect renders a login page inside the portal.
#   403  signed in, not yours -> hand the response back so the caller renders
#                               ``detail``. Never redirect: "not you" and "not
#                               yours" are different failures, and a redirect
#                               throws away the reason the server just explained.
#   5xx  broken or starting   -> bounded retries with backoff, then a response
#                               the caller can render. Never redirect.
#
# It also labels every request, which is how the edge knows to answer an XHR with
# a real 401 instead of a 302 that fetch() follows into login HTML at HTTP 200.
PORTAL_FETCH_JS = r"""
// Bounded on purpose: the appliance is either coming up or genuinely broken, and
// an unbounded retry loop turns one dead service into a browser that never stops
// asking. Three tries, roughly 0.5s / 1s / 2s, then let the caller say something.
const PORTAL_RETRY_DELAYS = [500, 1000, 2000];

function portalGotoLogin() {
  // rd must be where the *person* was, not where this document lives. The
  // settings panel is an iframe of the shell, so using its own path would send
  // the admin to /settings/content/ after sign-in: a bare fragment with no nav,
  // no chat frame and no shell around it.
  let here = window.location.pathname + (window.location.search || '');
  try {
    if (window.top !== window.self) {
      here = window.top.location.pathname + (window.top.location.search || '');
    }
  } catch (err) {
    // Reading another frame's location throws if it is ever cross-origin. The
    // own-path fallback still gets them signed back in, just less precisely.
  }
  window.top.location = '/login?rd=' + encodeURIComponent(here);
}

function portalUnavailable(status) {
  return {
    ok: false,
    status: status,
    // Callers all read a JSON body; this keeps that one assumption true for a
    // response that never had one. Callers gate on ok, so this is a bail-out.
    json: async () => ({ detail: 'The appliance is not answering. Try again shortly.' }),
  };
}

async function portalFetch(path, options) {
  const init = Object.assign({cache: 'no-store'}, options || {});
  init.headers = Object.assign({'X-Requested-With': 'fetch'}, init.headers || {});
  const method = (init.method || 'GET').toUpperCase();
  for (let attempt = 0; ; attempt += 1) {
    let res = null;
    try {
      res = await fetch(path, init);
    } catch (err) {
      res = null;  // no answer at all: same shape to the caller as a 5xx
    }
    if (res && res.status === 401) {
      portalGotoLogin();
      return portalUnavailable(401);
    }
    // Only GET is retried. reset-config and reconcile are not idempotent in the
    // way that matters: a retry after a lost response can run them twice.
    const retryable = (res === null || res.status >= 500) && method === 'GET';
    if (retryable && attempt < PORTAL_RETRY_DELAYS.length) {
      await new Promise((done) => setTimeout(done, PORTAL_RETRY_DELAYS[attempt]));
      continue;
    }
    if (res === null) return portalUnavailable(0);
    return res;  // 2xx, and 403/409 for the caller to render
  }
}

// The one place that knows how to describe a refusal. A managed-path denial and
// an ordinary permissions error both arrive as 403 carrying a detail written for
// the operator; rendering it verbatim is what makes "Lemonseed owns this setting"
// discoverable instead of looking like a broken button.
async function portalError(res, fallback) {
  if (res && typeof res.json === 'function') {
    try {
      const data = await res.json();
      if (data && data.detail) return data.detail;
    } catch (err) {
      // An HTML error page has no detail to read; fall through to the fallback.
    }
  }
  return fallback || 'That did not work.';
}
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
        admin_fields = """
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
<title>Lemonseed setup</title>{brand_head_links()}<link rel="stylesheet" href="/_lemonseed/lemonseed.css"/>
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


def _redirect_target(query: str) -> str:
    """Everything after ``rd=`` in a login query string, deliberately unparsed.

    nginx composes the gate redirect as ``/login?rd=$request_uri`` without
    encoding, and ``$request_uri`` legitimately contains ``?`` and ``&``
    (``/settings/?tab=x``). Reading it as an ordinary query parameter truncates
    the target at the second ``?``; taking the remainder keeps it whole and
    leaves safety to ``auth.safe_redirect_target``, the layer that actually
    knows what a browser would resolve.
    """
    if not query.startswith("rd="):
        return ""
    return query[len("rd=") :]


def page_login(*, rd: str = "/", error: str = "") -> bytes:
    """Branded login page (replaces Authelia's unbranded sign-in)."""
    setup = load_setup()
    name = escape(str(setup.get("name") or "Lemonseed"))
    error_html = f'<p class="login-error">{escape(error)}</p>' if error else ""
    rd_hidden = f'<input type="hidden" name="rd" value="{escape(rd)}"/>'
    body = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{name} — Sign in</title>{brand_head_links()}<link rel="stylesheet" href="/_lemonseed/lemonseed.css"/>
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
// The gate can land this page inside the shell's iframe, and a page inside a
// page is unusable, so break out to the top frame first (M6.6 D3).
if (window.top !== window.self) {{
  window.top.location = window.location.href;
}}
// Submit as JSON (the CP expects application/json) and label the request as
// ours, so the CP answers with a body instead of a 302 that fetch() would
// follow without ever exposing Location.
document.getElementById('login-form').addEventListener('submit', async (ev) => {{
  const form = ev.target;
  ev.preventDefault();
  const card = document.querySelector('.login-card');
  let message = '';
  let target = '/';
  try {{
    const body = Object.fromEntries(new FormData(form).entries());
    const res = await fetch('/login', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json', 'X-Requested-With': 'fetch'}},
      body: JSON.stringify(body),
    }});
    const data = await res.json().catch(() => ({{}}));
    if (res.ok) {{
      window.top.location = data.target || '/';
      return;
    }}
    message = data.detail || 'Sign in failed.';
  }} catch (err) {{
    message = 'The appliance is not answering yet. Try again in a moment.';
  }}
  let err = card.querySelector('.login-error');
  if (!err) {{
    err = document.createElement('p');
    err.className = 'login-error';
    card.insertBefore(err, form);
  }}
  err.textContent = message;
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
<title>{name}</title>{brand_head_links()}<link rel="stylesheet" href="/_lemonseed/lemonseed.css"/>
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
  <div class="landing" id="landing-card">
    <h2 id="landing-title">Getting Ready</h2>
    <p id="landing-lede">Preparing this appliance…</p>
    <p class="status-line wait" id="landing-status">Starting…</p>
    <div id="landing-picker" hidden>
      <div id="landing-cards" class="model-cards"></div>
    </div>
  </div>
</div>
<iframe id="chat-frame" class="{chat_frame_class}" title="Chat" src="about:blank" allow="clipboard-read; clipboard-write"></iframe>
<iframe id="panel-frame" class="{panel_frame_class}" title="Settings" src="{panel_src}"></iframe>
</div>
</main>
<script>{PORTAL_FETCH_JS}</script>
<script>
let currentView = {json.dumps(view)};
let chatBootstrapped = false;
// Deep links point the OWUI iframe at an admin route. showChatFrame returns the
// frame to the chat entry point when the admin clicks Chat themselves, but that
// reset must not swallow the switch the admin just asked for -- bare-metal
// review found "Open … workspace" landing back on the chat page because this
// switch was indistinguishable from a Chat-tab click.
let chatOverridden = false;
let readyStreak = 0;
const CHAT_SRC = '/?embed=1';
const SETTINGS_SRC = '/settings/content/';

function landingCopy(chat, owui) {{
  const chatState = chat.state || 'idle';
  const ui = owui.state || 'idle';
  const httpReady = !!owui.http_ready;
  const modelReady = chatState === 'ready';
  // M6.3b third gate: OWUI must be reconciled to Lemonseed intent before
  // anyone sees it (no blank-slate chat interface — decision D1).
  const policy = owui.owui_policy || {{}};
  const policyOk = policy.ok === true;
  const uiReady = ui === 'ready' && httpReady && policyOk;

  // A settings reset stops Open WebUI, so nothing below this point can be true
  // while it runs; say so plainly instead of falling through to "Preparing the
  // chat interface…" (bare-metal review: the interstitial was uninformative).
  if (owuiResetting(owui)) {{
    return {{
      title: 'Getting Ready',
      workflow: 'Rebuilding chat settings',
      task: 'Chats, models and accounts are untouched. This takes a minute…',
      cls: 'wait',
    }};
  }}

  if (chatState === 'rebooting') {{
    return {{
      title: 'Rebooting',
      workflow: 'Applying GPU memory settings',
      task: 'The appliance is rebooting — this page will reconnect automatically…',
      cls: 'wait',
    }};
  }}
  if (chatState === 'awaiting-choice') {{
    return {{
      title: 'Large Language Model',
      workflow: 'AI requires a Large Language Model to power the chat system. Select a model from the list below that best fits your needs, and Lemonseed will download it for you. These are large files and may take a long time to download on slower internet connections.',
      task: '',
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
      workflow: chat.workflow || 'Preparing the Large Language Model',
      task: chat.task || chat.message || 'Preparing a local model…',
      cls: 'wait',
    }};
  }}
  if (ui === 'ready' && httpReady && !policyOk) {{
    return {{
      title: 'Getting Ready',
      workflow: 'Configuring chat',
      task: policy.last_error
        ? String(policy.last_error)
        : 'Applying Lemonseed settings to the chat interface…',
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
    workflow: chat.workflow || 'Preparing the Large Language Model',
    task: chat.task || chat.message || 'Finishing setup…',
    cls: 'wait',
  }};
}}

function showChatFrame() {{
  const chat = document.getElementById('chat-frame');
  const panel = document.getElementById('panel-frame');
  const fallback = document.getElementById('chat-fallback');
  panel.classList.add('hidden');
  if (chatOverridden) {{
    chatOverridden = false;
    chat.src = CHAT_SRC;
  }}
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

// True while a reset window is open (both slots down, settings being rebuilt).
// 'idle' is absent from the payload; 'ready'/'error' are terminal and must not
// hold the interstitial open forever.
function owuiResetting(owui) {{
  const reset = owui.owui_reset;
  return !!(reset && reset.state && reset.state !== 'ready' && reset.state !== 'error');
}}

// A reset tears Open WebUI's config table out from under the running SPA, which
// keeps serving a cached bundle whose settings no longer exist: the admin saw a
// working chat page until they reloaded the page (found on bare metal). Drop the
// frame so the interstitial covers the window and a fresh bundle loads once the
// rebuilt slot is healthy again.
function retireChatFrame() {{
  if (!chatBootstrapped) return;
  chatBootstrapped = false;
  chatOverridden = false;
  readyStreak = 0;
  const chat = document.getElementById('chat-frame');
  chat.src = 'about:blank';
  chat.classList.add('hidden');
  if (currentView === 'chat') showChatFrame();
}}

async function gatewayReachable() {{
  // End-to-end check through nginx (same path the SPA hits after embed).
  // Deliberately NOT portalFetch: this is a probe whose whole job is to answer
  // "is it up yet", so retrying and redirecting on auth would both slow it down
  // and hide the very failure it exists to detect.
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
  // Tear the chat frame down before navigating. It carries its own Open WebUI
  // session, so leaving it loaded keeps a signed-in, interactive chat on screen
  // for as long as it takes the browser to paint /login (M6.6 step 4).
  retireChatFrame();
  // /logout answers 302 and fetch() follows it, so the cookie is cleared by the
  // request itself; the navigation is what actually leaves the shell.
  try {{
    await portalFetch('/logout', {{method: 'POST'}});
  }} catch (_err) {{
    // A failed logout still gets the user out of the shell: the cookie is
    // HttpOnly and idle-expiring, and a stuck signed-in page is worse.
  }}
  window.top.location = '/login';
}});
window.addEventListener('popstate', () => {{
  const path = location.pathname.replace(/\\/$/, '') || '/';
  setView(path === '/settings' ? 'settings' : 'chat', false);
}});

function makeLandingCard(name, desc, flag, selected, fits, onPick) {{
  const card = document.createElement('button');
  card.type = 'button';
  card.className = 'model-card' + (selected ? ' selected' : '') + (fits ? '' : ' unfit');
  const nameEl = document.createElement('span');
  nameEl.className = 'model-card-name';
  nameEl.textContent = name;
  const descEl = document.createElement('span');
  descEl.className = 'model-card-desc';
  descEl.textContent = desc;
  card.appendChild(nameEl);
  card.appendChild(descEl);
  if (flag) {{
    const flagEl = document.createElement('span');
    flagEl.className = 'model-card-flag';
    flagEl.textContent = flag;
    card.prepend(flagEl);
  }}
  if (fits) card.addEventListener('click', onPick);
  return card;
}}

async function loadLandingPicker() {{
  try {{
    const res = await portalFetch('/_lemonseed/api/models');
    if (!res.ok) return;
    const data = await res.json();
    const cards = document.getElementById('landing-cards');
    if (!cards) return;
    cards.innerHTML = '';
    for (const family of data.families || []) {{
      const best = family.best_fit || {{}};
      const size = best.weights_gb != null ? Number(best.weights_gb).toFixed(1) + ' GB' : '';
      const name = [family.maker, family.name, size ? '(' + size + ')' : ''].filter(Boolean).join(' ');
      const desc = family.fits ? (family.description || '') : 'No model from this family fits your hardware.';
      cards.appendChild(makeLandingCard(name, desc, family.flag, !!family.selected, !!family.fits, () => chooseLandingFamily(family.id)));
    }}
  }} catch (_err) {{}}
}}

async function chooseLandingFamily(family) {{
  try {{
    const res = await portalFetch('/_lemonseed/api/models', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{family: family}}),
    }});
    if (!res.ok) {{
      // Before M6.6 this returned in silence: a non-admin clicking a card got no
      // answer at all. The reason is already written for the operator.
      const status = document.getElementById('landing-status');
      if (status) {{
        status.textContent = await portalError(res, 'Could not select that model.');
        status.className = 'status-line bad';
        status.style.display = '';
      }}
      return;
    }}
    // Download is starting — the next poll leaves awaiting-choice and shows progress.
    const picker = document.getElementById('landing-picker');
    if (picker) picker.hidden = true;
  }} catch (_err) {{}}
}}

async function refreshStatus() {{
  try {{
    // /api/system is the gated full snapshot. /api/status is the open subset
    // (installer handoff + the gateway's not-ready page) and no longer carries
    // the telemetry, module and chat state this function reads.
    const response = await portalFetch('/api/system');
    // 401 is already handled inside portalFetch (top-frame navigation to /login).
    // A 403 must NOT redirect: it means signed-in-but-not-permitted, and this
    // polls every 5s, so a redirect here would be a login loop. Say it once.
    if (!response.ok) {{
      const denied = document.getElementById('landing-status');
      if (denied && response.status === 403) {{
        denied.textContent = await portalError(response, 'You cannot see the appliance status.');
        denied.className = 'status-line bad';
        denied.style.display = '';
      }}
      return;
    }}
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
    const policyOk = !!(owui.owui_policy && owui.owui_policy.ok === true);
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
      status.style.display = copy.task ? '' : 'none';
    }}
    // Inline model picker on the first-login landing while awaiting a choice.
    const picker = document.getElementById('landing-picker');
    if (picker) {{
      const awaiting = chat.state === 'awaiting-choice';
      if (awaiting && picker.hidden) {{ picker.hidden = false; loadLandingPicker(); }}
      else if (!awaiting && !picker.hidden) {{ picker.hidden = true; }}
    }}
    if (owuiResetting(owui)) retireChatFrame();
    if (ui === 'ready' && httpReady && modelReady && policyOk && await gatewayReachable()) {{
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


# Plain string (not an f-string) on purpose: JS braces stay single.
OWUI_CONFIG_JS = r"""
function owuiEl(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = String(text);
  return el;
}

// What a home user needs here: is the chat interface running, and do its
// settings match Lemonseed's. Everything else in the payload (key-by-key
// ownership, guard rules, policy hashes) is developer detail -- it lives in
// the manifest and the log, not in a home admin's settings page.

// Prefer what Open WebUI reports about itself; fall back to the tag of the
// image this Lemonseed build ships. Never show a registry path as a version.
function owuiVersionLabel(version, image) {
  let raw = String(version || '').trim();
  if (!raw) {
    const tail = String(image || '').split('/').pop();
    raw = (tail && tail.indexOf(':') >= 0) ? tail.split(':').pop() : '';
  }
  if (!raw) return '';
  return (raw.charAt(0) === 'v' || raw.charAt(0) === 'V') ? raw : 'v' + raw;
}

// Reconcile stamps are always written in UTC (owui_policy._utcnow_iso), so
// trimming the ISO text beats a timezone conversation in the browser.
function owuiStamp(iso) {
  const text = String(iso || '');
  return text.length >= 16 ? text.slice(0, 16).replace('T', ' ') + ' UTC' : text;
}

// Only problems get a pill; "everything is fine" is the row's job.
function owuiPill(host, label, value, title) {
  const node = owuiEl('span', 'owui-pill bad', label + ' ');
  node.appendChild(owuiEl('b', null, value == null ? '—' : value));
  if (title) node.title = title;
  host.appendChild(node);
  return node;
}

function renderOwuiPolicy(policy) {
  const counts = policy.drift_counts || {};
  // The status row speaks docker's own words ("Up 59 minutes (healthy)") so it
  // matches what an admin would see in `docker ps`.
  const row = document.getElementById('owui-state');
  if (row && policy.container_status) {
    const text = String(policy.container_status);
    row.textContent = text;
    row.className = 'state ' + (text.indexOf('Up ') === 0
      ? (text.indexOf('(unhealthy)') >= 0 ? 'wait' : 'ok') : 'bad');
  }
  const title = document.getElementById('owui-title');
  if (title) {
    const label = owuiVersionLabel(policy.owui_version, policy.container_image);
    if (label) title.textContent = 'Open WebUI ' + label;
  }
  const config = document.getElementById('owui-config-state');
  if (config) {
    if (policy.ok) {
      config.textContent = policy.last_reconciled
        ? 'updated ' + owuiStamp(policy.last_reconciled) : 'updated';
      config.className = 'state ok';
      config.removeAttribute('title');
    } else {
      // Never claim a config was applied when the engine says otherwise; the
      // hover text is the one place the real reason is reachable.
      config.textContent = policy.last_reconciled ? 'needs attention' : 'not applied yet';
      config.className = 'state bad';
      if (policy.last_error) config.title = String(policy.last_error);
    }
  }
  const strip = document.getElementById('owui-strip');
  if (strip) {
    strip.innerHTML = '';
    if (counts.error) {
      owuiPill(strip, 'could not be applied', counts.error, 'the control plane could not set these');
    }
    if (counts.pending) {
      owuiPill(strip, 'changed in Open WebUI since boot', counts.pending,
               'reported only: Lemonseed re-applies its settings at the next boot or after "Apply now"');
    }
    strip.hidden = !strip.firstChild;
  }
}

async function loadOwuiConfig() {
  const note = document.getElementById('owui-note');
  const button = document.getElementById('owui-refresh');
  // An empty <p> still occupies a line box, which reads as a gap between the
  // Status and Controls subsections when there is nothing to report.
  const setNote = (text) => { note.textContent = text; note.hidden = !text; };
  button.disabled = true;
  try {
    const res = await portalFetch('/_lemonseed/api/owui/config');
    if (res.status === 403) {
      setNote('Administration settings are visible to admins only.');
      return;
    }
    if (!res.ok) {
      setNote('Could not read the configuration view (HTTP ' + res.status + ').');
      return;
    }
    const view = await res.json();
    renderOwuiPolicy(view.policy || {});
    // Only report what the admin can act on; "everything is fine" is the
    // Status rows' job, and a paragraph per reload is noise.
    const bits = [];
    if (!view.live_available) {
      bits.push('The chat interface is not answering right now, so this shows what '
                + 'Lemonseed intends rather than what it currently holds.');
    }
    setNote(bits.join(' '));
  } catch (_err) {
    setNote('Could not read the configuration view.');
  } finally {
    button.disabled = false;
  }
}

document.getElementById('owui-refresh').addEventListener('click', loadOwuiConfig);

// --- Phase 4B admin actions -------------------------------------------------
// Both are admin-only endpoints also gated in nginx; the CP re-checks the
// admin group either way. Results go to the status banner, not a toast: an
// apply or a reset changes whether the chat interface works, so its outcome
// has to stay on screen after the admin stops looking at it.

async function owuiApplyNow() {
  const button = document.getElementById('owui-apply');
  const refresh = document.getElementById('owui-refresh');
  button.disabled = true;
  refresh.disabled = true;
  owuiStatus('Applying Lemonseed settings…');
  try {
    const res = await portalFetch('/_lemonseed/api/owui/reconcile', {method: 'POST'});
    const body = await res.json().catch(() => ({}));
    if (res.status === 409) {
      owuiStatus('A settings update is already running. Try again in a moment.', true);
      return;
    }
    if (res.status === 503) {
      owuiStatus('The chat interface is not ready to receive settings yet.', true);
      return;
    }
    if (!res.ok) {
      // 403 arrives with a reason the control plane wrote (not an admin, or a
      // path Lemonseed owns). HTTP codes are not a home-user error message.
      owuiStatus(body.detail || 'Could not apply settings (HTTP ' + res.status + ').', true);
      return;
    }
    const drift = body.drift || {};
    if (body.ok) {
      owuiStatus('Settings applied. Re-applied ' + (drift.fixed || 0)
                 + ', already correct ' + (drift.ok || 0) + '.');
    } else {
      owuiStatus('Applied, but some settings could not be set: '
                 + (body.last_error || 'check the control plane log for the reason'), true);
    }
  } catch (_err) {
    owuiStatus('Could not reach the control plane.', true);
    return;
  } finally {
    button.disabled = false;
    refresh.disabled = false;
  }
  loadOwuiConfig();
}

let owuiResetTimer = null;

// The banner doubles as "what is happening to the chat interface right now",
// so it is a bordered block rather than the muted hint line above it.
function owuiStatus(text, bad) {
  const status = document.getElementById('owui-reset-status');
  status.textContent = text || '';
  status.hidden = !text;
  status.classList.toggle('bad', !!bad);
}

function owuiResetPoll() {
  clearTimeout(owuiResetTimer);
  owuiResetTimer = setTimeout(async () => {
    let reset = null;
    try {
      const res = await portalFetch('/api/modules');
      const body = await res.json();
      reset = (body.open_webui || {}).owui_reset || null;
    } catch (_err) {
      reset = null;
    }
    if (!reset || reset.state === 'idle') {
      owuiStatus('');
      return;
    }
    if (reset.state === 'ready' || reset.state === 'error') {
      owuiStatus(reset.state === 'ready'
        ? 'Chat settings rebuilt. The chat interface is back.'
        : 'Reset failed: ' + (reset.error || 'unknown error')
          + '. Your chats, models and accounts were not changed.',
        reset.state !== 'ready');
      loadOwuiConfig();
      return;
    }
    owuiStatus('Rebuilding chat settings (' + reset.state + ')… the chat interface '
               + 'is offline until this finishes.');
    owuiResetPoll();
  }, 3000);
}

async function owuiResetConfig() {
  // Server-rendered so the word the admin types and the one the control plane
  // checks cannot drift apart across a deploy. Compared case-insensitively by
  // the CP; single-line copy because window.prompt wraps awkwardly.
  const phrase = document.getElementById('owui-reset').dataset.confirm || '';
  const typed = window.prompt(
    'Reset rebuilds the chat interface settings from Lemonseed defaults. Chats, '
    + 'models and accounts are kept; settings changed in Open WebUI are lost. '
    + 'Type ' + phrase + ' to confirm.',
    '');
  if (typed === null) return;
  owuiStatus('');
  try {
    const res = await portalFetch('/_lemonseed/api/owui/reset-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({confirm: typed}),
    });
    const body = await res.json().catch(() => ({}));
    if (res.status === 400) {
      owuiStatus('Confirmation did not match. Type ' + phrase + ' to reset.', true);
      return;
    }
    if (res.status === 409) {
      owuiStatus('Another change is already in progress.', true);
      return;
    }
    if (res.status === 503) {
      owuiStatus('The chat interface is not ready to be reset yet.', true);
      return;
    }
    if (!res.ok) {
      owuiStatus(body.detail || 'Reset refused (HTTP ' + res.status + ').', true);
      return;
    }
    owuiStatus('Rebuilding chat settings… the chat interface is offline until it '
               + 'finishes.');
    owuiResetPoll();
  } catch (_err) {
    owuiStatus('Could not reach the control plane.', true);
  }
}

document.getElementById('owui-apply').addEventListener('click', owuiApplyNow);
document.getElementById('owui-reset').addEventListener('click', owuiResetConfig);
loadOwuiConfig();

// A reset started before this page was opened is still running; adopt it, or the
// banner stays blank while the chat interface is offline and the admin has no
// idea why. Terminal states are deliberately not replayed on every visit.
(async function owuiAdoptReset() {
  try {
    const res = await portalFetch('/api/modules');
    const body = await res.json();
    const reset = (body.open_webui || {}).owui_reset;
    if (!reset || reset.state === 'ready' || reset.state === 'error') return;
    owuiStatus('Rebuilding chat settings (' + reset.state + ')… the chat interface '
               + 'is offline until this finishes.');
    owuiResetPoll();
  } catch (_err) {}
})();
"""


def _owui_version_label(version: Optional[str], image: Optional[str]) -> str:
    """Render ``0.11.0``/``ghcr.io/…/open-webui:v0.11.0`` as ``v0.11.0`` ('' if unknown).

    The portal names the chat panel after the version it is showing, so a
    registry path must never leak into that label.
    """
    raw = str(version or "").strip()
    if not raw:
        tail = str(image or "").rsplit("/", 1)[-1]
        raw = tail.split(":", 1)[1] if ":" in tail else ""
    if not raw:
        return ""
    return raw if raw[0] in "vV" else "v" + raw


def _fmt_utc(iso: Optional[str]) -> str:
    """``2026-10-09T05:05:00+00:00`` -> ``2026-10-09 05:05 UTC``.

    One format for both the server-rendered rows and the portal JS (which
    trims the same ISO string), so the two can never disagree. Unparseable
    input is shortened rather than dropped — a wrong-looking timestamp is
    still more useful to an admin than a blank row.
    """
    text = str(iso or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:16].replace("T", " ")
    utc = parsed.astimezone(datetime.timezone.utc) if parsed.tzinfo else parsed
    return utc.strftime("%Y-%m-%d %H:%M UTC")


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
    elif chat_state == "awaiting-choice":
        chat_label = "awaiting choice"
        chat_class = "wait"
        chat_lede = ""
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
    # Prefer docker's own words ("Up 59 minutes (healthy)") — that is what an
    # admin comparing with `docker ps` expects to read. The control plane's
    # lifecycle word is the fallback where docker cannot answer (dev machines,
    # unit tests), rather than showing an invented uptime.
    container = modules.owui_container_status()
    if container:
        owui_status = container[0]
        if owui_status.startswith("Up "):
            owui_class = "wait" if "(unhealthy)" in owui_status else "ok"
        else:
            owui_class = "bad"
    else:
        owui_status = owui_state
    owui_policy_state = owui.get("owui_policy") or {}
    owui_version = _owui_version_label(
        owui_policy_state.get("owui_version"),
        (container[1] if container else None) or owui.get("image"),
    )
    owui_title = f"Open WebUI {owui_version}" if owui_version else "open webui"
    # The engine's own verdict, not the container's: "updated <when>" only
    # after a reconcile reported ok.
    if owui_policy_state.get("ok"):
        reconciled = _fmt_utc(owui_policy_state.get("last_reconciled"))
        owui_config_text = f"updated {reconciled}" if reconciled else "updated"
        owui_config_class = "ok"
        owui_config_title = ""
    else:
        owui_config_text = ("needs attention"
                            if owui_policy_state.get("last_reconciled") else "not applied yet")
        owui_config_class = "bad"
        owui_config_title = escape(str(owui_policy_state.get("last_error") or ""))
    owui_config_hint = f' title="{owui_config_title}"' if owui_config_title else ""

    body = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Settings</title>{brand_head_links()}<link rel="stylesheet" href="/_lemonseed/lemonseed.css"/>
</head><body><main class="embed-shell">
<h1>Settings</h1>
<section class="panel">
<h2>Lemonade AI Engine</h2>
<div class="row"><span class="name">status</span><span class="state {chat_class}" id="chat-state">{chat_label}</span></div>
<div class="row"><span class="name">model</span><span class="state ok" id="chat-model">{display}</span></div>
<p class="lede" id="chat-lede">{chat_lede}</p>
</section>
<section class="panel" id="model-picker">
<h2>Large Language Model</h2>
<p class="lede">AI requires a Large Language Model to power the chat system. Select a model from the list below that best fits your needs, and Lemonseed will download it for you. These are large files and may take a long time to download on slower internet connections.</p>
<div id="model-cards" class="model-cards"></div>
</section>
<section class="panel">
<h2>Status</h2>
<div class="row"><span class="name">version</span><span class="state ok">{snap.get("version","unknown")}</span></div>
<div class="row"><span class="name">setup</span><span class="state ok">complete</span></div>
<div class="row"><span class="name">timezone</span><span class="state ok">{setup.get("timezone","UTC")}</span></div>
{gpu_rows}
</section>
<section class="panel" id="owui-config">
<h2>Chat</h2>
<h3 class="subhead">Status</h3>
<div class="row"><span class="name" id="owui-title">{owui_title}</span><span class="state {owui_class}" id="owui-state">{owui_status}</span></div>
<div class="row"><span class="name">Lemonseed configuration</span><span class="state {owui_config_class}" id="owui-config-state"{owui_config_hint}>{owui_config_text}</span></div>
<div class="owui-strip" id="owui-strip" hidden></div>
<p class="owui-hint" id="owui-note">Loading…</p>
<h3 class="subhead">Controls</h3>
<p class="lede">Lemonseed configures the chat interface (Open WebUI) to work with the Lemonade AI engine. Here you can trigger Lemonseed to reapply the managed configuration parameters or reset the Open WebUI configuration database to defaults.</p>
<p class="owui-status" id="owui-reset-status" hidden></p>
<div class="owui-actions">
<button type="button" id="owui-apply">Apply Lemonseed settings now</button>
<button type="button" id="owui-reset" data-confirm="{modules.OWUI_RESET_CONFIRM}">Reset chat settings</button>
<button type="button" id="owui-refresh">Refresh status</button>
</div>
</section>
<section class="panel">
<h2>API</h2>
<details>
<summary style="cursor:pointer;color:var(--muted)">System API Data</summary>
<p class="lede" id="chat-reason">{reason}</p>
<p class="lede"><a style="color:var(--accent)" href="/api/hardware">/api/hardware</a> ·
<a style="color:var(--accent)" href="/api/chat">/api/chat</a> ·
<a style="color:var(--accent)" href="/api/modules">/api/modules</a> ·
<a style="color:var(--accent)" href="/api/system">/api/system</a></p>
</details>
</section>
<script>{PORTAL_FETCH_JS}</script>
<script>
async function refreshChat() {{
  try {{
    const response = await portalFetch('/api/chat');
    if (!response.ok) return;
    const chat = await response.json();
    const state = chat.state || 'idle';
    const model = chat.display_name || chat.model_id || '—';
    const progress = chat.progress || 0;
    let label = state;
    let cls = 'wait';
    let lede = chat.message || '';
    if (state === 'ready') {{ label = 'ready'; cls = 'ok'; lede = model + ' is loaded for local chat.'; }}
    else if (state === 'awaiting-choice') {{ label = 'awaiting choice'; cls = 'wait'; lede = ''; }}
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

function makeModelCard(name, desc, flag, selected, fits, onPick) {{
  const card = document.createElement('button');
  card.type = 'button';
  card.className = 'model-card' + (selected ? ' selected' : '') + (fits ? '' : ' unfit');
  const nameEl = document.createElement('span');
  nameEl.className = 'model-card-name';
  nameEl.textContent = name;
  const descEl = document.createElement('span');
  descEl.className = 'model-card-desc';
  descEl.textContent = desc;
  card.appendChild(nameEl);
  card.appendChild(descEl);
  if (flag) {{
    const flagEl = document.createElement('span');
    flagEl.className = 'model-card-flag';
    flagEl.textContent = flag;
    card.prepend(flagEl);
  }}
  if (fits) card.addEventListener('click', onPick);
  return card;
}}

async function loadModelPicker() {{
  try {{
    const modelsRes = await portalFetch('/_lemonseed/api/models');
    if (!modelsRes.ok) return;
    const data = await modelsRes.json();
    const cards = document.getElementById('model-cards');
    cards.innerHTML = '';
    for (const family of data.families || []) {{
      const best = family.best_fit || {{}};
      const size = best.weights_gb != null ? Number(best.weights_gb).toFixed(1) + ' GB' : '';
      const name = [family.maker, family.name, size ? '(' + size + ')' : ''].filter(Boolean).join(' ');
      const desc = family.fits
        ? (family.description || '')
        : 'No model from this family fits your hardware.';
      cards.appendChild(makeModelCard(name, desc, family.flag, !!family.selected, !!family.fits, () => chooseFamily(family.id)));
    }}
  }} catch (_err) {{}}
}}

async function chooseFamily(family) {{
  try {{
    const res = await portalFetch('/_lemonseed/api/models', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{family: family}}),
    }});
    if (!res.ok) {{
      // An HTTP status is not a message. A non-admin, or a family that stopped
      // fitting, both have a real reason sitting in the body already.
      const body = await res.json().catch(() => ({{}}));
      alert(body.detail || 'Could not change model (HTTP ' + res.status + ').');
      return;
    }}
    // Re-render to reflect the new selection; the status row shows progress.
    loadModelPicker();
  }} catch (_err) {{ alert('Could not reach the control plane.'); }}
}}

loadModelPicker();
</script>
<script>{OWUI_CONFIG_JS}</script>
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

    def _send(
        self,
        code: int,
        body: bytes,
        content_type: str,
        extra_headers: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # Installer page on the previous origin (IP or .local) polls these
        # endpoints after reboot; allow that cross-origin fetch.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Lemonseed-Role", "control-plane")
        # Headers that must be emitted *with* the response, never queued before
        # send_response() (that would put them ahead of the status line).
        for key, value in extra_headers or ():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Client went away mid-response (reboot, tab close, nginx abort).
            # socketserver would otherwise dump a full traceback per occurrence.
            self.log_message("client disconnected during response (%s)", self.path)

    def _json(self, code: int, obj: Any, extra_headers: Optional[list[tuple[str, str]]] = None) -> None:
        self._send(
            code,
            (json.dumps(obj) + "\n").encode(),
            "application/json",
            extra_headers=extra_headers,
        )

    def _is_xhr(self) -> bool:
        """True when the caller identified itself as the portal's own fetch()."""
        return self.headers.get(XHR_HEADER, "").strip() == XHR_MARKER

    def _require_admin(self) -> bool:
        """True when the caller is an admin; sends JSON 403 and returns False if not.

        nginx sets X-Forwarded-Groups from the CP session-verify response
        headers (via auth_request_set + proxy_set_header). When the auth gate
        is disabled (pre-admin) the header is absent, which is fine: setup is
        not yet complete and /settings/ redirects to /setup anyway.

        The body is JSON with ``detail`` because the portal renders ``detail``
        for every 403 it gets, from any source (M6.6).
        """
        groups = self.headers.get("X-Forwarded-Groups", "")
        parts = {g.strip() for g in groups.split(",") if g.strip()}
        if "admin" not in parts:
            self._json(
                403,
                {
                    "detail": "You are signed in, but this needs an admin account.",
                    "lemonseed": NEEDS_ADMIN_MARKER,
                },
            )
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

        Returns 200 + X-Forwarded-* headers on success, 401 to sign in again, and
        **503** when the session store is unreachable (M6.6 D2, fail closed).
        The 503 matters: without it a Valkey outage would answer 401 and silently
        sign the whole household out, and nginx turns a non-401/403 subrequest
        failure into a gateway error page instead of a login redirect.
        """
        token = self._parse_cookie(auth.SESSION_COOKIE)
        if not token:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            record = auth.verify_session(token)
        except sessions.SessionStoreUnavailable:
            self._json(503, {"detail": "session store unavailable", "state": "unavailable"})
            return
        if record is None:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("X-Forwarded-User", record.get("u", ""))
        self.send_header("X-Forwarded-Email", record.get("e", ""))
        self.send_header("X-Forwarded-Groups", ",".join(record.get("g", [])))
        self.send_header("X-Forwarded-Name", record.get("n", "") or record.get("u", ""))
        self.send_header("X-Forwarded-Role", auth.role(record.get("g", [])))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_login_get(self, query: str) -> None:
        """GET /login — branded login page."""
        if setup_complete() and not auth.admin_bootstrap_required():
            # Auth is active: show the login form.
            rd = auth.safe_redirect_target(_redirect_target(query))
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
        rd = auth.safe_redirect_target(str(data.get("rd", "/")))

        # Rate limiting by source IP.
        ip = self.client_address[0] if self.client_address else "unknown"
        if not auth.rate_limit_check(ip):
            self._json(401, {"detail": "Too many attempts. Wait a minute and try again."})
            return

        user = auth.verify_credentials(username, password)
        if user is None:
            auth.rate_limit_record_failure(ip)
            self._json(401, {"detail": "Invalid username or password."})
            return

        auth.rate_limit_reset(ip)
        try:
            token = auth.create_session(user)
        except sessions.SessionStoreUnavailable:
            # Fail closed: a cookie we cannot validate later is access we cannot
            # expire or revoke. Login is the one place to say so out loud.
            self._json(
                503,
                {"detail": "Sign-in is unavailable: the session store is not reachable."},
            )
            return
        except ValueError:
            # Account vanished or was disabled between check and issue.
            self._json(401, {"detail": "Invalid username or password."})
            return

        # A fetch() cannot read a 302's Location, so an XHR gets the target in a
        # 200 body and navigates _top itself; a plain form post still gets the
        # redirect. Same cookie either way.
        cookie = f"{auth.SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"
        if self._is_xhr():
            self._json(200, {"target": rd}, extra_headers=[("Set-Cookie", cookie)])
            return
        self.send_response(302)
        self.send_header("Location", rd)
        self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_logout(self) -> None:
        """POST /logout — drop the session record and clear the cookie.

        The store is touched best-effort: an unreachable Valkey must not trap a
        user in a signed-in shell, and the cleared cookie plus the idle TTL bounds
        any orphaned record.
        """
        token = self._parse_cookie(auth.SESSION_COOKIE)
        if token:
            auth.drop_session(token)
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", f"{auth.SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_model_choice_post(self, raw: bytes) -> None:
        """POST /_lemonseed/api/models — record the user's model family choice (admin).

        Body: {"family": "qwen"} | {"family": "auto"}. Persisted on user-data,
        then the chat worker re-plans and pulls/loads the new model.
        """
        if not self._require_admin():
            return
        try:
            data = json.loads(raw.decode() or "{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            self._json(400, {"error": "bad request"})
            return
        family = str(data.get("family", "")).strip()
        catalog_ = catalog.load_catalog()
        known = {
            str(f.get("id")) for f in (catalog_.get("families") or []) if isinstance(f, dict)
        }
        if family not in known and family != "auto":
            self._json(400, {"error": f"unknown family: {family}"})
            return
        # Optional explicit model within the family (e.g. the QEMU e2e pins
        # Gemma 12B for a deterministic pull regardless of box size).
        model_id = data.get("model_id")
        if isinstance(model_id, str):
            model_id = model_id.strip() or None
            if model_id is not None and family != "auto":
                entries = [m for m in (catalog_.get("models") or []) if isinstance(m, dict)]
                if not any(
                    m.get("id") == model_id and m.get("family") == family for m in entries
                ):
                    self._json(400, {"error": f"unknown model: {model_id}"})
                    return
        saved = catalog.write_model_choice(family, model_id)
        # A new choice invalidates any prior give-up decision for the old model
        # and cancels an in-flight download so the user doesn't wait for a
        # model they no longer want.
        chat_ready.clear_gave_up()
        chat_ready.cancel_active_pull()
        chat_ready.ensure_chat_worker()
        self._json(200, {"state": "ok", "choice": saved})

    # ------------------------------------------------------------------
    # QEMU e2e GC helpers (test-only; admin-gated like the model choice).
    # The disk-space guard is exercised end-to-end by scripts/qemu-api-smoke-test.sh
    # (QEMU_E2E_GC=1): fill /var past the 90% high-water mark, pull a model,
    # and assert the LRU model was evicted via lemond's API. Not exposed in
    # the UI and not routed by the gateway's public allowlist.
    # ------------------------------------------------------------------

    _GC_FILL_PATH = Path("/var/tmp/lemonseed-gc-test.bin")

    def _handle_gc_test(self, raw: bytes) -> None:
        if not self._require_admin():
            return
        try:
            data = json.loads(raw.decode() or "{}")
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            nbytes = int(data.get("bytes") or 0)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
            self._json(400, {"error": "bad request"})
            return
        if nbytes <= 0 or nbytes > (16 << 30):
            self._json(400, {"error": "bytes must be in (0, 16 GiB]"})
            return
        try:
            self._GC_FILL_PATH.parent.mkdir(parents=True, exist_ok=True)
            with self._GC_FILL_PATH.open("wb") as fh:
                chunk = b"\0" * (1 << 20)
                remaining = nbytes
                while remaining > 0:
                    fh.write(chunk[: min(len(chunk), remaining)])
                    remaining -= len(chunk)
            self._json(200, {"state": "ok", "bytes": nbytes})
        except OSError as exc:
            self._json(500, {"error": f"fill failed: {exc}"})

    def _handle_gc_test_remove(self) -> None:
        if not self._require_admin():
            return
        try:
            self._GC_FILL_PATH.unlink(missing_ok=True)
            self._json(200, {"state": "ok"})
        except OSError as exc:
            self._json(500, {"error": f"remove failed: {exc}"})

    def _handle_gc_test_models(self) -> None:
        if not self._require_admin():
            return
        # lemond's /v1/models lists cached models (the `lemonade list` source).
        key = hardware.lemonade_api_key()
        try:
            req = Request(
                "http://127.0.0.1:13305/v1/models",
                headers={"Authorization": f"Bearer {key}"} if key else {},
            )
            with urlopen(req, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self._json(200, payload if isinstance(payload, dict) else {"data": []})
        except Exception as exc:  # noqa: BLE001 — service boundary
            self._json(502, {"error": f"lemond unreachable: {exc}"})

    def _handle_owui_config_view(self) -> None:
        """Open WebUI config ownership view: intent + fact + health (4A).

        Three sources, one payload: the manifest (what Lemonseed intends), a
        live ``configs/export`` (what Open WebUI actually holds) and the
        engine's last drift report (how enforcement is going). Degrades rather
        than fails: a rejected manifest still shows live keys classified
        ``yours``; an unreachable Open WebUI still shows intent. Only the view
        builder itself blowing up is worth a 5xx.
        """
        try:
            manifest = owui_policy.load_manifest()
        except owui_policy.ManifestError as exc:
            manifest = None
            manifest_error: Optional[str] = str(exc)[:300]
        else:
            manifest_error = None
        export = modules.owui_config_export()
        status = dict(owui_policy.state_summary())
        # Docker's own view of the running container, for the portal's Status
        # row (uptime wording + the image tag behind the version label). None
        # simply omits the keys and the panel keeps the lifecycle word it
        # rendered server-side.
        container = modules.owui_container_status()
        if container:
            status["container_status"], status["container_image"] = container
        if manifest_error:
            status["manifest_error"] = manifest_error
        try:
            view = owui_portal.build_view(
                manifest, status, export.get("data"),
                guard_state=modules.owui_guard_state(),
            )
        except Exception as exc:  # noqa: BLE001 — HTTP boundary
            self._json(502, {"ok": False, "error": f"config view failed: {str(exc)[:200]}"})
            return
        view["export_error"] = export.get("error")
        self._json(200, view)

    def _handle_owui_reconcile_post(self) -> None:
        """Admin "apply Lemonseed settings now" (T4-E).

        Status codes are the contract the portal JS branches on: 200 applied
        (or reported drift), 409 something else is already running, 503 nothing
        is serving yet to write to.
        """
        result = modules.request_policy_reconcile()
        if result.get("reason") == "in flight":
            self._json(409, result)
            return
        if result.get("reason") == "not ready":
            self._json(503, result)
            return
        self._json(200, result)

    def _handle_owui_reset_post(self, raw: bytes) -> None:
        """Admin config reset (T4-C/T4-D): validate, then 202 and walk away.

        The window itself takes minutes under emulation, so it must not hold
        this connection; progress and the final result come from the
        ``owui_reset`` field of ``/api/modules``.
        """
        try:
            data = json.loads(raw.decode() or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"accepted": False, "reason": "expected a JSON body"})
            return
        confirm = data.get("confirm") if isinstance(data, dict) else None
        result = modules.reset_owui_config(confirm if isinstance(confirm, str) else "")
        if not result.get("accepted"):
            reason = str(result.get("reason") or "")
            if reason == "confirmation text did not match":
                self._json(400, {**result, "confirm_text": modules.OWUI_RESET_CONFIRM})
            elif reason in ("not ready",):
                self._json(503, result)
            else:
                self._json(409, result)
            return
        self._json(202, result)

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
            self._send(200, body, ctype)
            return
        if path == "/favicon.ico":
            fp = BRAND_DIR / "favicon.ico"
            if fp.is_file():
                self._send(200, fp.read_bytes(), "image/x-icon")
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
        if path == "/_lemonseed/api/models":
            report = hardware.collect_hardware()
            hardware.persist_hardware(report)
            self._json(
                200,
                {
                    "families": catalog.families_view(report),
                    "plan": plan_model(report),
                },
            )
            return
        if path == "/_lemonseed/api/gc-test/models":
            self._handle_gc_test_models()
            return
        if path == "/_lemonseed/api/owui/config":
            # M6.3b 4A: ownership view of the managed Open WebUI config tree.
            # Admin-only by design: members must not enumerate the appliance's
            # managed surface (and the payload merges the raw config export).
            if not self._require_admin():
                return
            self._handle_owui_config_view()
            return
        if path == "/api/data":
            data_services.ensure_data_worker()
            data_services.ensure_provision_worker()
            self._json(200, data_services.data_snapshot())
            return
        if path == "/api/modules":
            data_services.ensure_provision_worker()
            modules.ensure_module_worker()
            self._json(200, modules.modules_snapshot())
            return
        if path == "/api/status":
            # Unauthenticated by design (installer handoff + the gateway's
            # not-ready page). Keep this cheap and free of household facts; the
            # full snapshot lives at /api/system behind the session gate.
            maybe_complete_mdns_handoff(self)
            self._json(200, status_public())
            return
        if path == "/api/system":
            # nginx gates this with the session (it is not admin-only: every user's
            # shell polls it to decide when the chat frame may load).
            chat_ready.ensure_chat_worker()
            data_services.ensure_data_worker()
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
                    # The session store is what makes a session revocable, so an
                    # admin troubleshooting "nobody can sign in" needs to see it
                    # here. Sessions fail closed when it is down, which is why
                    # this is a status dot and not a fallback path.
                    "session_store": sessions.store_status(),
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
        if path == "/_lemonseed/api/models":
            self._handle_model_choice_post(raw)
            return
        if path == "/_lemonseed/api/gc-test/fill":
            self._handle_gc_test(raw)
            return
        if path == "/_lemonseed/api/gc-test/fill/remove":
            self._handle_gc_test_remove()
            return
        if path == "/_lemonseed/api/owui/reconcile":
            # M6.3b 4B: admin-only, and gated in nginx too (adminGate). The
            # endpoint is idempotent, but a member must not be able to force
            # the appliance to re-assert (and thus undo) their changes.
            if not self._require_admin():
                return
            self._handle_owui_reconcile_post()
            return
        if path == "/_lemonseed/api/owui/reset-config":
            if not self._require_admin():
                return
            self._handle_owui_reset_post(raw)
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

    def handle(self) -> None:  # noqa: N802
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            # Client went away (reboot, tab close, nginx abort). socketserver
            # would otherwise dump a full traceback per occurrence.
            self.log_message("client disconnected (%s)", self.path)
        except Exception:  # noqa: BLE001
            # Service boundary: log and keep the server alive.
            import traceback

            self.log_message("error handling %s", self.path)
            traceback.print_exc(file=sys.stderr)

    def log_message(self, format: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), format % args), flush=True)


def _safe_refresh_gpus() -> None:
    """One-time GPU inventory probe at CP startup (see hardware.refresh_gpus).

    Runs off the request path; a failure must never take down the control
    plane — the next collect_hardware() will retry the probe.
    """
    try:
        hardware.refresh_gpus()
    except Exception as exc:  # noqa: BLE001
        print(f"hardware inventory: {exc}", flush=True)


def main() -> None:
    # A reboot-pending marker from the previous boot is stale by definition
    # (we are up); clear it so the portal doesn't show "Rebooting…" forever.
    chat_ready.clear_reboot_pending()
    # Probe the GPU inventory once at startup so the expensive external tools
    # (nvidia-smi / llama-cli / vulkaninfo) run exactly once per boot, not on
    # every portal poll. Runs in the background so it never delays listen.
    threading.Thread(
        target=_safe_refresh_gpus, name="lemonseed-hardware-inventory", daemon=True
    ).start()
    chat_ready.ensure_chat_worker()
    data_services.ensure_data_worker()
    data_services.ensure_provision_worker()
    modules.ensure_module_worker()
    auth.ensure_auth_gate_worker()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"appliance-control-plane listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
