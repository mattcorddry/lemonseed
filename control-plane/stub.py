#!/usr/bin/env python3
"""Lemonseed control plane (bring-up + app shell).

Listens on 127.0.0.1:8080 behind nginx :80.
  - Before setup: no-auth first-boot / setup wizard
  - After setup: Lemonseed shell at / (toolbar + iframe → chat, settings, …)
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
import secrets
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

import audit
import auth
import catalog
import chat_ready
import data_services
import hardware
import journal
import modules
import owui_policy
import owui_portal
import sessions
import srp
import updates
import usermgmt
from catalog import plan_model

HOST = os.environ.get("APPLIANCE_CP_HOST", "127.0.0.1")
PORT = int(os.environ.get("APPLIANCE_CP_PORT", "8080"))
VERSION_FILE = Path("/run/appliance/version")
FIRST_BOOT_FILE = Path("/etc/appliance-first-boot")
SETUP_COMPLETE_FILE = Path("/etc/appliance-setup-complete")
SETUP_STATE_FILE = Path("/etc/appliance-setup.json")
HANDOFF_DONE_FILE = Path("/run/lemonseed/mdns-handoff-done")
# One journald channel for the access trail: journald rotates and retains it,
# and `journalctl -t appliance-control-plane-access` reads it back — while the
# service's own log stays readable (BM-3 review).
ACCESS_LOGGER = logging.getLogger("appliance-control-plane-access")
ACCESS_LOGGER.setLevel(logging.INFO)
ACCESS_LOGGER.addHandler(journal.JournalHandler("appliance-control-plane-access"))
ACCESS_LOGGER.propagate = False
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
# Vocabulary for the four-word sign-in codes an admin hands out. It ships as a
# data file whose *name carries the locale*, so words-fr_CA.txt can be added
# later without touching code, and the page fetches it only when it is about to
# build a code — ~12 KB that no ordinary page load pays for. Only the selection
# is secret, and the browser makes that with crypto.getRandomValues, so serving
# the vocabulary discloses nothing.
WORDLIST_LOCALE = "en_US"
WORDLIST_FILE = Path(__file__).resolve().with_name(f"words-{WORDLIST_LOCALE}.txt")
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


# --- Power controls (M7 P5) -------------------------------------------------
#
# Two actions, both admin-only at the gateway *and* in the control plane. The
# delay is not ceremony: the response has to reach the browser before the machine
# disappears under it, or the portal reports a failed request for an action that
# worked.
POWER_ACTIONS = ("reboot", "shutdown")
POWER_COMMANDS = {"reboot": "reboot", "shutdown": "poweroff"}
POWER_DELAY_SEC = 2.0


def schedule_power(action: str) -> None:
    """Reboot or power off on a daemon thread, a moment from now.

    Off the request thread for the reason above, and kept as one named function
    because it is the only thing in the control plane that can end a test
    machine's session: the tests patch this, and nothing else calls it.
    """

    def go() -> None:
        time.sleep(POWER_DELAY_SEC)
        command = POWER_COMMANDS[action]
        print(f"lemonseed-power: {command} requested from the portal", flush=True)
        run_best_effort(["systemctl", command])

    threading.Thread(target=go, name="lemonseed-power", daemon=True).start()


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


# --- One dialog component for the whole portal (M7) --------------------------

# window.prompt, window.confirm and window.alert were the first pass, and they are
# not good enough here. They paint in the browser's chrome rather than this theme,
# they carry one line of copy, a "type the username to confirm" prompt cannot
# disable its own OK button, and each has a different focus and Escape behaviour.
# Worse, they are *blocking*: a native dialog inside the settings iframe takes the
# whole page's event loop, which is why a confirm sitting over the chat frame
# reads as a freeze.
#
# So there is one component. It builds DOM nodes and never parses a string as
# markup, which keeps the rule the panels already follow (a display name is free
# text somebody typed) true on the way back out. Escape and a click outside mean
# "no", focus is trapped and then handed back, and the page cannot scroll out
# from under the box.
DIALOG_JS = r"""
const DIALOG_FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),'
  + 'select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
const dialogStack = [];
let dialogSeq = 0;

function dialogNode(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function dialogVisible(node) {
  return node.getClientRects().length > 0;
}

function dialogTop() { return dialogStack[dialogStack.length - 1] || null; }

function dialogScrollLock() {
  document.documentElement.classList.toggle('dialog-open', dialogStack.length > 0);
}

// Closes the dialog it is called from, not merely the top one: a callback held
// by an older box must not be able to close whoever happens to be on top.
function dialogDismiss(dlg, value) {
  const index = dialogStack.indexOf(dlg);
  if (index < 0) return;
  dialogStack.splice(index, 1);
  document.removeEventListener('keydown', dlg.onKey, true);
  dlg.mask.remove();
  dialogScrollLock();
  if (dlg.returnFocus && typeof dlg.returnFocus.focus === 'function') {
    dlg.returnFocus.focus();
  }
  dlg.settle(value);
}

// spec: {title, copy, body, build(api), actions, dismiss}
//   actions: [{label, kind, value, primary, disabled}] — `value` may be a
//            function, read when the button is clicked, which is how a prompt
//            returns what its field holds without a sentinel string.
//   build(api): called once the box is assembled, before it is shown. api is
//            {body, ok, choose} — append fields to api.body, wire gating to
//            api.ok, and call api.choose(value) to answer the dialog.
// Resolves the chosen action's value, or spec.dismiss when closed without one.
function dialog(spec) {
  return new Promise((resolve) => {
    dialogSeq += 1;
    const titleId = 'dialog-title-' + dialogSeq;
    const mask = dialogNode('div', 'dialog-mask');
    const box = dialogNode('div', 'dialog');
    // The mask is what goes in the document, so the box has to be inside it.
    // Missing this line paints an empty dark overlay, which is exactly what the
    // first harness run caught.
    mask.appendChild(box);
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-modal', 'true');
    box.setAttribute('aria-labelledby', titleId);
    const heading = dialogNode('h2', null, spec.title || '');
    heading.id = titleId;
    box.appendChild(heading);
    if (spec.copy) box.appendChild(dialogNode('p', 'dialog-copy', spec.copy));
    const bodySlot = dialogNode('div', 'dialog-body');
    box.appendChild(bodySlot);
    if (spec.body) bodySlot.appendChild(spec.body);

    const dlg = {
      mask: mask,
      returnFocus: document.activeElement,
      settle: resolve,
      quiet: spec.dismiss === undefined ? null : spec.dismiss,
    };
    function choose(value) { dialogDismiss(dlg, value); }

    const actions = dialogNode('div', 'dialog-actions');
    let ok = null;
    const chosen = spec.actions || [{label: 'Close', kind: 'primary', value: true}];
    chosen.forEach((a) => {
      const b = dialogNode('button', 'btn' + (a.kind ? ' ' + a.kind : ''), a.label);
      b.type = 'button';
      b.disabled = !!a.disabled;
      b.addEventListener('click', () => {
        choose(typeof a.value === 'function' ? a.value() : a.value);
      });
      actions.appendChild(b);
      if (a.primary) ok = b;
    });
    box.appendChild(actions);

    dlg.onKey = (ev) => {
      if (dialogTop() !== dlg) return;   // only the top box answers the keyboard
      if (ev.key === 'Escape') {
        ev.preventDefault();
        ev.stopPropagation();
        choose(dlg.quiet);
        return;
      }
      if (ev.key !== 'Tab') return;
      const items = Array.prototype.filter.call(
        box.querySelectorAll(DIALOG_FOCUSABLE),
        (n) => dialogVisible(n),
      );
      if (!items.length) { ev.preventDefault(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      if (ev.shiftKey && document.activeElement === first) {
        ev.preventDefault();
        last.focus();
      } else if (!ev.shiftKey && document.activeElement === last) {
        ev.preventDefault();
        first.focus();
      }
    };

    mask.addEventListener('mousedown', (ev) => {
      if (ev.target === mask) choose(dlg.quiet);
    });

    // build() runs before the box is shown, so a caller that gates its own OK
    // button never has a frame where an unarmed button is clickable.
    if (spec.build) spec.build({body: bodySlot, ok: ok, choose: choose});

    dialogStack.push(dlg);
    dialogScrollLock();
    document.body.appendChild(mask);
    document.addEventListener('keydown', dlg.onKey, true);
    const field = box.querySelector('input,textarea,select');
    if (field) field.focus();
    else if (ok) ok.focus();
    else {
      box.tabIndex = -1;
      box.focus();
    }
  });
}

// The one yes/no question. Every caller gets the same words for the same act.
function dialogConfirm(title, copy, options) {
  const opts = options || {};
  return dialog({
    title: title,
    copy: copy,
    dismiss: false,
    actions: [
      {label: opts.cancelLabel || 'Cancel', kind: 'ghost', value: false},
      {
        label: opts.confirmLabel || 'Continue',
        kind: opts.danger ? 'danger' : 'primary',
        value: true,
        primary: true,
      },
    ],
  });
}

// The box that replaces the browser's message box: the copy is the point, so it
// cannot be dismissed by clicking past it, and there is only one way out.
function dialogMessage(title, copy) {
  return dialog({
    title: title,
    copy: copy,
    dismiss: false,
    actions: [{label: 'OK', kind: 'primary', value: true, primary: true}],
  });
}

// The browser's text-input box, replaced. Resolves the new text, or null if it
// was cancelled.
function dialogPrompt(title, copy, options) {
  const opts = options || {};
  let field = null;
  return dialog({
    title: title,
    copy: copy,
    dismiss: null,
    actions: [
      {label: 'Cancel', kind: 'ghost', value: null},
      {
        label: opts.confirmLabel || 'Save',
        kind: 'primary',
        primary: true,
        value: () => (field ? field.value : ''),
      },
    ],
    build: (api) => {
      const label = dialogNode('label', null, opts.label || 'Value');
      label.htmlFor = 'dialog-field-' + dialogSeq;
      field = dialogNode('input');
      field.id = 'dialog-field-' + dialogSeq;
      field.type = opts.inputType || 'text';
      field.value = opts.value == null ? '' : String(opts.value);
      field.maxLength = opts.maxLength || 254;
      field.autocomplete = 'off';
      api.body.appendChild(label);
      api.body.appendChild(field);
      if (opts.hint) api.body.appendChild(dialogNode('p', 'owui-hint', opts.hint));
      field.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          api.choose(field.value);
        }
      });
    },
  });
}

// The confirmation a destructive act needs. The button stays disabled until the
// word is typed, so there is no "you mistyped it" message to read and the only
// way to press it is to have written the name.
function dialogTypedConfirm(title, copy, word, options) {
  const opts = options || {};
  let field = null;
  let armed = null;
  return dialog({
    title: title,
    copy: copy,
    dismiss: false,
    actions: [
      {label: 'Cancel', kind: 'ghost', value: false},
      {label: opts.confirmLabel || 'Confirm', kind: 'danger', primary: true, disabled: true, value: true},
    ],
    build: (api) => {
      armed = api.ok;
      const label = dialogNode('label', null, opts.label || ('Type "' + word + '" to confirm'));
      label.htmlFor = 'dialog-field-' + dialogSeq;
      field = dialogNode('input');
      field.id = 'dialog-field-' + dialogSeq;
      field.type = 'text';
      field.autocomplete = 'off';
      field.spellcheck = false;
      api.body.appendChild(label);
      api.body.appendChild(field);
      if (opts.extra) api.body.appendChild(opts.extra);
      const check = () => {
        armed.disabled = field.value.trim().toLowerCase() !== String(word).toLowerCase();
      };
      field.addEventListener('input', check);
      field.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' && !armed.disabled) {
          ev.preventDefault();
          api.choose(true);
        }
      });
      check();
    },
  });
}
"""


# --- SRP-6a sign-in (M7 P2) -------------------------------------------------

# The browser half of SRP-6a, injected into *both* /login (handshake) and /setup
# (enrollment). Like PORTAL_FETCH_JS it lives in a module constant rather than a
# static file so it cannot drift from the server it talks to, and so a new nginx
# location is not needed (AGENTS.md keeps the gateway to exact-match CP routes).
#
# Why this is here at all: the appliance is HTTP-only forever (no FQDN, no
# usable CA), so the previous design put the password in a JSON body that anyone
# on the LAN could read. SRP removes the credential from the wire. What it does
# **not** buy, stated plainly because overselling it would be worse than not
# doing it: the session cookie is still a bearer token on plaintext HTTP, and an
# active on-path attacker can still rewrite this page. What it does stop is
# passive sniffing and the reuse of one wire capture as a password.
#
# SHA-256 is hand-rolled on purpose. window.crypto.subtle is undefined unless the
# page is a secure context, and this page never is, so "just use the browser's"'
# is not available. One code path, always this one — no subtle/fallback fork to
# get wrong. The KAT gate at the bottom is what makes a wrong digest loud instead
# of a mysterious "invalid credentials" for a correct password.
SRP_CLIENT_JS = r"""
var LemonseedSRP = (function () {
  'use strict';

  // SHA-256 round constants, K[i] = floor(2^32 * frac(cbrt(prime(i)))).
  var K = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2
  ];

  function rotr(x, n) { return ((x >>> n) | (x << (32 - n))) >>> 0; }

  function sha256(msg) {
    var total = (((msg.length + 8) >> 6) + 1) << 6;
    var m = new Uint8Array(total);
    m.set(msg);
    m[msg.length] = 0x80;
    var bits = msg.length * 8;
    var view = new DataView(m.buffer);
    view.setUint32(total - 8, Math.floor(bits / 0x100000000));
    view.setUint32(total - 4, bits >>> 0);

    var H = [
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
      0x1f83d9ab, 0x5be0cd19
    ];
    var w = new Uint32Array(64);
    for (var off = 0; off < total; off += 64) {
      for (var i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4);
      for (i = 16; i < 64; i++) {
        var s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
        var s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      var a = H[0], b = H[1], c = H[2], d = H[3];
      var e = H[4], f = H[5], g = H[6], h = H[7];
      for (i = 0; i < 64; i++) {
        var S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        var ch = (e & f) ^ (~e & g);
        var t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
        var S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        var maj = (a & b) ^ (a & c) ^ (b & c);
        var t2 = (S0 + maj) >>> 0;
        h = g; g = f; f = e;
        e = (d + t1) >>> 0;
        d = c; c = b; b = a;
        a = (t1 + t2) >>> 0;
      }
      for (i = 0; i < 8; i++) H[i] = (H[i] + [a, b, c, d, e, f, g, h][i]) >>> 0;
    }
    var out = new Uint8Array(32);
    var ov = new DataView(out.buffer);
    for (i = 0; i < 8; i++) ov.setUint32(i * 4, H[i]);
    return out;
  }

  function utf8(s) { return new TextEncoder().encode(s); }

  function toHex(bytes) {
    var s = '';
    for (var i = 0; i < bytes.length; i++) {
      s += (bytes[i] >> 4).toString(16) + (bytes[i] & 15).toString(16);
    }
    return s;
  }

  function fromHex(hex) {
    if (typeof hex !== 'string' || hex.length % 2 || !/^[0-9a-fA-F]*$/.test(hex)) {
      throw new Error('not hexadecimal');
    }
    var out = new Uint8Array(hex.length / 2);
    for (var i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
    return out;
  }

  function concat() {
    var n = 0, i;
    for (i = 0; i < arguments.length; i++) n += arguments[i].length;
    var out = new Uint8Array(n), at = 0;
    for (i = 0; i < arguments.length; i++) { out.set(arguments[i], at); at += arguments[i].length; }
    return out;
  }

  // RFC 5054 2.1 implicit conversion: minimal length, non-zero top byte. A and B
  // and S are hashed in this form; only u and the multiplier use pad(). Mixing
  // the two produces an implementation that agrees with itself and nothing else.
  function intToBytes(v) {
    if (v === 0n) return new Uint8Array(0);
    var hex = v.toString(16);
    if (hex.length % 2) hex = '0' + hex;
    return fromHex(hex);
  }

  function bytesToInt(bytes) {
    var v = 0n;
    for (var i = 0; i < bytes.length; i++) v = (v << 8n) | BigInt(bytes[i]);
    return v;
  }

  function shaHex() { return toHex(sha256(concat.apply(null, arguments))); }
  function shaInt() { return BigInt('0x' + shaHex.apply(null, arguments)); }

  // RFC 5054 Appendix A group 3 (2048-bit), generator 2. Pinned, never generated:
  // 3.2 is explicit that trusting N comes from matching a published list.
  var GROUP_N_HEX =
    'AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC319294' +
    '3DB56050A37329CBB4A099ED8193E0757767A13DD52312AB4B03310D' +
    'CD7F48A9DA04FD50E8083969EDB767B0CF6095179A163AB3661A05FB' +
    'D5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740ADBF4FF74' +
    '7359D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A' +
    '436C6481F1D2B9078717461A5B9D32E688F87748544523B524B0D57D' +
    '5EA77A2775D2ECFA032CFBDBF52FB3786160279004E57AE6AF874E73' +
    '03CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DBFBB6' +
    '94B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F' +
    '9E4AFF73';
  var N = BigInt('0x' + GROUP_N_HEX);
  var G = 2n;
  var N_BYTES = 256;

  function pad(v) {
    var bytes = intToBytes(v);
    if (bytes.length > N_BYTES) throw new Error('value wider than N');
    var out = new Uint8Array(N_BYTES);
    out.set(bytes, N_BYTES - bytes.length);
    return out;
  }

  function modPow(base, exp, mod) {
    var result = 1n;
    base = ((base % mod) + mod) % mod;
    while (exp > 0n) {
      if (exp & 1n) result = (result * base) % mod;
      base = (base * base) % mod;
      exp >>= 1n;
    }
    return result;
  }

  // k is the SRP-6 multiplier; without it a hostile server can mount a 2-for-1
  // guessing attack. Derived, never transcribed.
  var K_MULT = shaInt(intToBytes(N), pad(G));

  function xFromPassword(salt, identity, password) {
    // x = H(s | H(I | ":" | P)). The identity inside the inner hash is what stops
    // one verifier being portable across two accounts.
    return shaInt(salt, sha256(utf8(identity + ':' + password)));
  }

  function randomBytes(n) {
    if (typeof crypto === 'undefined' || !crypto.getRandomValues) {
      throw new Error('no source of randomness available');
    }
    var out = new Uint8Array(n);
    crypto.getRandomValues(out);
    return out;
  }

  function randomPrivate() {
    // RFC 5054 3.1: at least 256 bits, and strictly below N.
    for (var tries = 0; tries < 8; tries++) {
      var v = bytesToInt(randomBytes(32));
      if (v > 0n && v < N) return v;
    }
    throw new Error('could not draw a private value');
  }

  function enroll(identity, password) {
    var salt = randomBytes(16);
    var v = modPow(G, xFromPassword(salt, identity, password), N);
    return { salt: toHex(salt), verifier: v.toString(16) };
  }

  function authenticate(identity, password, saltHex, bHex) {
    var salt = fromHex(saltHex);
    if (salt.length !== 16) throw new Error('salt has the wrong length');
    if (typeof bHex !== 'string' || !/^[0-9a-fA-F]+$/.test(bHex)) {
      throw new Error('server value is not hexadecimal');
    }
    var B = BigInt('0x' + bHex);
    // RFC 5054 2.5.3: the client MUST abort if B % N == 0. Skipping this is how
    // an implementation ends up believing a session key nobody else holds.
    if (B <= 0n || B >= N || B % N === 0n) throw new Error('bad server value');

    var a = randomPrivate();
    var A = modPow(G, a, N);
    var u = shaInt(pad(A), pad(B));
    if (u === 0n) throw new Error('scrambler is zero');
    var x = xFromPassword(salt, identity, password);
    var base = (B - K_MULT * modPow(G, x, N)) % N;
    if (base < 0n) base += N;
    var S = modPow(base, a + u * x, N);
    var key = sha256(intToBytes(S));
    var m1 = sha256(concat(intToBytes(A), intToBytes(B), intToBytes(S)));
    var m2 = sha256(concat(intToBytes(A), m1, key));
    return { A: A.toString(16), M1: toHex(m1), expectM2: toHex(m2) };
  }

  function verifyServer(expectedHex, gotHex) {
    if (typeof gotHex !== 'string' || gotHex.length !== expectedHex.length) return false;
    var diff = 0;
    for (var i = 0; i < expectedHex.length; i++) {
      diff |= expectedHex.charCodeAt(i) ^ gotHex.charCodeAt(i);
    }
    return diff === 0;
  }

  // FIPS 180-4 known-answer vectors. Runs at load; a failure here means every
  // digest this page produces is wrong, so both forms refuse to submit.
  var KAT_VECTORS = [
    ['', 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'],
    ['abc', 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'],
    ['abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq',
     '248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1']
  ];

  function selfTest() {
    try {
      for (var i = 0; i < KAT_VECTORS.length; i++) {
        if (toHex(sha256(utf8(KAT_VECTORS[i][0]))) !== KAT_VECTORS[i][1]) return false;
      }
      return GROUP_N_HEX.length === 512 && K_MULT > 0n;
    } catch (err) {
      return false;
    }
  }

  return {
    ok: selfTest(),
    enroll: enroll,
    authenticate: authenticate,
    verifyServer: verifyServer,
    sha256Hex: function (s) { return toHex(sha256(utf8(s))); },
    sha256HexBytes: function (hex) { return toHex(sha256(fromHex(hex))); }
  };
})();
"""


# Sign-in glue for page_login. Separate from SRP_CLIENT_JS so its braces stay
# single: PORTAL_JS-style doubling inside the page f-string is exactly the kind
# of thing that breaks silently, and a constant needs none of it.
#
# Note what is *not* here: the password never enters any object that is
# serialised. It is read once, handed to LemonseedSRP, and dropped.
SRP_LOGIN_JS = r"""
(function () {
  'use strict';
  // The gate can land this page inside the shell's iframe, and a page inside a
  // page is unusable, so break out to the top frame first (M6.6 D3).
  if (window.top !== window.self) {
    window.top.location = window.location.href;
  }

  function show(card, form, message) {
    let err = card.querySelector('.login-error');
    if (!err) {
      err = document.createElement('p');
      err.className = 'login-error';
      card.insertBefore(err, form);
    }
    err.textContent = message;
  }

  async function post(path, payload) {
    const res = await fetch(path, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'fetch'},
      body: JSON.stringify(payload),
    });
    let data = {};
    try { data = await res.json(); } catch (err) { data = {}; }
    return {ok: res.ok, status: res.status, data: data};
  }

  // D-11: a reset hands out a code that must be exchanged for a password of the
  // user's own. ``srp/verify`` answers with ``rotate_required`` and deliberately
  // no cookie, so the exchange happens *before* anyone is signed in — a code
  // handed over a shoulder should not open a live session on the way to being
  // changed. Two phases, same page: prove the current password, then replace it.
  let rotating = false;
  let who = '';

  function askNewPassword(form) {
    if (form.querySelector('.rotate-fields')) return;
    const box = document.createElement('div');
    box.className = 'rotate-fields';
    const hint = document.createElement('p');
    hint.className = 'lede';
    hint.textContent = 'Choose a new password to finish signing in.';
    box.appendChild(hint);
    [['new_password', 'New password'], ['confirm_password', 'Repeat it']].forEach((f) => {
      const wrap = document.createElement('div');
      wrap.className = 'field';
      const label = document.createElement('label');
      label.textContent = f[1];
      label.htmlFor = f[0];
      const input = document.createElement('input');
      input.id = f[0];
      input.name = f[0];
      input.type = 'password';
      input.autocomplete = 'new-password';
      input.required = true;
      wrap.appendChild(label);
      wrap.appendChild(input);
      box.appendChild(wrap);
    });
    const submit = form.querySelector('button[type=submit]');
    form.insertBefore(box, submit);
    submit.textContent = 'Set password and sign in';
  }

  async function finishRotation(form) {
    const next = form.elements.new_password.value || '';
    const again = form.elements.confirm_password.value || '';
    if (next.length < 8) return 'Your new password must be at least 8 characters.';
    if (next !== again) return 'The two passwords do not match.';
    const current = form.elements.password.value || '';
    const challenge = await post('/_lemonseed/api/srp/challenge', {username: who});
    if (!challenge.ok) return challenge.data.detail || 'Sign in failed.';
    const c = challenge.data;
    // Two derivations, neither of which is a password the appliance sees: a proof
    // that the caller knows the *current* one, and the verifier of the *new* one.
    const proof = LemonseedSRP.authenticate(who, current, c.salt, c.B);
    const pair = LemonseedSRP.enroll(who, next);
    const done = await post('/_lemonseed/api/srp/rotate', {
      cid: c.cid, A: proof.A, M1: proof.M1, salt: pair.salt, verifier: pair.verifier,
    });
    if (!done.ok) return done.data.detail || 'The password was not changed.';
    if (!LemonseedSRP.verifyServer(proof.expectM2, done.data.M2)) {
      return 'The appliance could not prove its identity. Try again.';
    }
    window.top.location = done.data.target || '/';
    return null;
  }

  document.getElementById('login-form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    const card = document.querySelector('.login-card');
    // The KAT gate. If SHA-256 is wrong here, M1 cannot match and every correct
    // password reads as "invalid credentials"; refusing and saying so is the
    // only honest outcome, so this check is not optional.
    if (!window.LemonseedSRP || !LemonseedSRP.ok) {
      show(card, form, 'This browser failed a security self-check, so signing in is disabled.');
      return;
    }
    if (rotating) {
      try {
        const problem = await finishRotation(form);
        if (problem) show(card, form, problem);
      } catch (err) {
        show(card, form, 'The appliance is not answering yet. Try again in a moment.');
      }
      return;
    }
    const username = (form.elements.username.value || '').trim();
    const password = form.elements.password.value || '';
    const rd = (form.elements.rd && form.elements.rd.value) || '/';
    try {
      const challenge = await post('/_lemonseed/api/srp/challenge', {username: username});
      if (!challenge.ok) {
        show(card, form, challenge.data.detail || 'Sign in failed.');
        return;
      }
      const c = challenge.data;
      const proof = LemonseedSRP.authenticate(username, password, c.salt, c.B);
      const done = await post('/_lemonseed/api/srp/verify',
                              {cid: c.cid, A: proof.A, M1: proof.M1});
      if (!done.ok) {
        show(card, form, done.data.detail || 'Sign in failed.');
        return;
      }
      // Mutual authentication. A sign-in that skips this has proved something
      // *to* the appliance and received nothing back.
      if (!LemonseedSRP.verifyServer(proof.expectM2, done.data.M2)) {
        show(card, form, 'The appliance could not prove its identity. Try again.');
        return;
      }
      if (done.data.rotate_required) {
        who = username;
        rotating = true;
        askNewPassword(form);
        return;
      }
      window.top.location = done.data.target || '/';
    } catch (err) {
      show(card, form, 'The appliance is not answering yet. Try again in a moment.');
    }
  });
})();
"""

# Enrollment glue for page_setup. The browser derives (salt, verifier) and posts
# those; the password field is disabled before the native submit runs, so it is
# not in the request body at all.
SRP_ENROLL_JS = r"""
(function () {
  'use strict';
  const form = document.getElementById('setup-form');
  if (!form) return;

  function fail(message) {
    const err = document.getElementById('setup-srp-error');
    if (err) err.textContent = message;
  }

  form.addEventListener('submit', function (ev) {
    const user = document.getElementById('admin_username');
    if (!user) return;  // No admin bootstrap on this variant of the form.
    const pw = document.getElementById('admin_password');
    if (!window.LemonseedSRP || !LemonseedSRP.ok) {
      ev.preventDefault();
      fail('This browser failed a security self-check, so setup cannot continue.');
      return;
    }
    // The password policy now lives *here*. The server never sees the password,
    // so it structurally cannot enforce one; this is the only place it can live.
    if ((pw.value || '').length < 8) {
      ev.preventDefault();
      fail('Admin password must be at least 8 characters.');
      return;
    }
    let pair;
    try {
      pair = LemonseedSRP.enroll(user.value.trim(), pw.value);
    } catch (err) {
      ev.preventDefault();
      fail('Could not prepare sign-in data: ' + (err && err.message ? err.message : err));
      return;
    }
    document.getElementById('admin_srp_salt').value = pair.salt;
    document.getElementById('admin_srp_verifier').value = pair.verifier;
    // A disabled input is not submitted, so the plaintext leaves the document
    // entirely rather than merely being ignored by the server.
    pw.disabled = true;
  });
})();
"""


# The one place that changes the caller's own password. Two screens need it (the
# admin's account panel and every user's My Profile page), and a four-step SRP
# sequence duplicated across both is exactly the kind of thing that drifts.
#
# It takes the page's own `post` so the helper stays agnostic about how a refusal
# becomes a sentence: both callers use a `post` that throws an Error carrying
# text meant for a human. Throwing is deliberate — a rotation that failed halfway
# must not be able to leave the caller believing a password changed.
ROTATE_JS = r"""
window.LemonseedRotate = (function () {
  'use strict';

  // Throws instead of returning false, so the caller cannot forget the check.
  async function change(post, username, current, next) {
    const c = await post('/_lemonseed/api/srp/challenge', { username: username });
    // Two derivations, and neither is a password the appliance can see: a proof
    // of the current one, and the verifier of the new one (D-5).
    const proof = LemonseedSRP.authenticate(username, current, c.salt, c.B);
    const pair = LemonseedSRP.enroll(username, next);
    const done = await post('/_lemonseed/api/srp/rotate', {
      cid: c.cid, A: proof.A, M1: proof.M1, salt: pair.salt, verifier: pair.verifier,
    });
    // M2 is the appliance proving itself. If it cannot, nothing was changed.
    if (!LemonseedSRP.verifyServer(proof.expectM2, done.M2)) {
      throw new Error('The appliance could not prove its identity. Nothing was changed.');
    }
  }

  return { change: change };
})();
"""

# Who you are, in the corner of the bar. Every signed-in person needs to see
# their own name, sign out, and reach their profile, so this lives apart from the
# admin-only account panel and is emitted on every signed-in page.
ACCOUNT_JS = r"""
(function () {
  'use strict';

  const btn = document.getElementById('nav-account');
  const pop = document.getElementById('account-pop');
  if (!btn || !pop) return;

  function open(on) {
    pop.hidden = !on;
    btn.setAttribute('aria-expanded', on ? 'true' : 'false');
  }

  btn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    open(pop.hidden);
  });
  // The popover is a plain element, so dismissing it is our job: a click
  // anywhere else, or Escape, closes it.
  document.addEventListener('click', (ev) => {
    if (!pop.hidden && !pop.contains(ev.target)) open(false);
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') open(false);
  });

  document.getElementById('nav-logout').addEventListener('click', async () => {
    // Tear the chat frame down before navigating, when this page has one. It
    // carries its own Open WebUI session, so leaving it loaded keeps a
    // signed-in, interactive chat on screen for as long as the browser takes to
    // paint /login (M6.6 step 4). The profile page has no frame, hence the test.
    if (typeof window.retireChatFrame === 'function') window.retireChatFrame();
    // /logout answers 302 and fetch() follows it, so the cookie is cleared by the
    // request itself; the navigation is what actually leaves the page.
    try {
      await portalFetch('/logout', { method: 'POST' });
    } catch (err) {
      // A failed logout still gets the user out: the cookie is HttpOnly and
      // idle-expiring, and a stuck signed-in page is worse.
    }
    window.top.location = '/login';
  });

  // One request, three consequences: who the popover says you are, whether the
  // Settings gear is yours to see (non-admins never get the system panel), and
  // whether "Edit profile" is worth offering on a page that already is one.
  (async function fill() {
    const res = await portalFetch('/_lemonseed/api/profile');
    if (!res.ok) return;
    const me = (await res.json()).user || {};
    const name = document.getElementById('pop-name');
    const who = document.getElementById('pop-user');
    if (name) name.textContent = me.display_name || me.username || '';
    if (who) who.textContent = me.username || '';
    const gear = document.getElementById('nav-settings');
    // A non-admin reaching /settings/ is refused at the gateway; hiding the gear
    // is honesty, not access control.
    if (gear) gear.hidden = !(me.groups || []).includes('admin');
    // A newer version is admin news (P5): the red dot rides the gear, and the
    // SYSTEM panel is where the buttons live. One extra request for admins;
    // members never see the gear and never ask.
    if (gear && !(me.groups || []).includes('admin')) {
      portalFetch('/_lemonseed/api/update').then(async (res) => {
        if (!res.ok) return;
        const state = (await res.json());
        if (state.available) gear.classList.add('update-dot');
      }).catch(() => {});
    }
    const link = document.getElementById('pop-profile');
    if (link && document.getElementById('profile-panel')) link.hidden = true;
  })();
})();
"""

# Users and groups (M7 P3/P4): the Users & Groups pane of /settings/content/,
# rendered for admins. Three rules hold this screen together:
#   * Passwords are never typed into it and never leave it. Creating an account
#     and resetting one both derive (salt, verifier) in THIS document and post
#     only that, so the appliance never sees a password it could leak. The
#     one-time code is generated here for the same reason (D-10, D-27).
#   * Every row is built with textContent, never innerHTML, and the glyphs are
#     built with createElementNS. A display name is free text a household types
#     in; treating it as markup would be an injected script in their own session.
#   * Every question goes through the one dialog component, and a row action the
#     control plane would refuse is rendered disabled with the reason in its
#     title (D-39, D-40). The server still refuses; the panel only says so first.
ACCOUNTS_JS = r"""
(function () {
  'use strict';

  let people = [];
  let groups = [];
  let firstAdmin = '';
  let attributeKeys = [];
  let me = '';
  let busy = false;
  let auditLog = [];

  // The username rule, from USERS-AND-GROUPS.md D-25 (POSIX-shaped, lower-cased,
  // 2-16 characters). The control plane enforces it again and owns the reserved
  // list; this only stops the dialog offering Save on a name that cannot work.
  const USERNAME_RE = /^[a-z][a-z0-9_.-]{1,15}$/;

  function note(message, bad) {
    const el = document.getElementById('acct-note');
    if (!el) return;
    el.textContent = message || '';
    el.hidden = !message;
    el.className = 'owui-status' + (bad ? ' bad' : '');
  }

  function setBusy(on) {
    busy = on;
    document.querySelectorAll('#accounts button').forEach((b) => { b.disabled = on; });
  }

  // A refusal carries a sentence written for the person reading it, so render
  // the server's words rather than the HTTP status.
  async function post(path, payload) {
    const res = await portalFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await portalError(res, 'That did not work.'));
    return res.json();
  }

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  // Emptying a container by removing its children, so this panel has exactly one
  // rendering rule: nothing here ever parses a string as markup, which leaves no
  // path from a name a household typed to a script tag in their own session.
  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  // --- icons ------------------------------------------------------------------
  // One <path> of subpaths, built with createElementNS rather than from a markup
  // string, because the rule above is what keeps a typed display name inert. The
  // visible label is a title plus a screen-reader span, so the glyph is never the
  // only way to find out what a button does.
  const ICONS = {
    edit: 'M4.5 19.5h4L19 9l-4-4L4.5 15.5z M14.5 5.5l4 4',
    code: 'M8.5 12a3.5 3.5 0 1 0 0 7a3.5 3.5 0 1 0 0-7 M10.9 13.1L19 5'
      + ' M16.5 7.5l1.8 1.8 M14.3 9.7l1.8 1.8',
    remove: 'M4.5 6.5h15 M9.5 4h5 M6.5 6.5l1 13.5h9l1-13.5 M10.3 10v7 M13.7 10v7',
    members: 'M9.6 8.6a3.2 3.2 0 1 0 6.4 0a3.2 3.2 0 1 0-6.4 0'
      + ' M3.8 19.6c0-3.2 2.6-5.2 5.8-5.2s5.8 2 5.8 5.2'
      + ' M16 5.8a3.2 3.2 0 0 1 0 5.9 M17.4 14.6c2.1.7 3.4 2.5 3.4 4.7',
    settings: 'M4 7h7.6 M16.8 7H20 M14.2 4.8a2.2 2.2 0 1 0 0 4.4a2.2 2.2 0 1 0 0-4.4'
      + ' M4 12h2.8 M12 12H20 M9.4 9.8a2.2 2.2 0 1 0 0 4.4a2.2 2.2 0 1 0 0-4.4'
      + ' M4 17h7.6 M16.8 17H20 M14.2 14.8a2.2 2.2 0 1 0 0 4.4a2.2 2.2 0 1 0 0-4.4',
    add: 'M12 5v14 M5 12h14',
  };
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function svgIcon(name) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '15');
    svg.setAttribute('height', '15');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '1.7');
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', ICONS[name] || '');
    svg.appendChild(path);
    return svg;
  }

  // A row action. `blocked` is a sentence: bare-metal review found buttons that
  // the appliance would refuse, which reads as a broken panel. Greyed out, with
  // the reason in the title, the refusal is already explained.
  function iconButton(icon, label, title, onClick, blocked) {
    const b = el('button', 'icon-btn' + (icon === 'remove' ? ' danger' : ''));
    b.type = 'button';
    b.appendChild(svgIcon(icon));
    b.appendChild(el('span', 'sr-only', label));
    b.title = blocked || title;
    if (blocked) {
      b.disabled = true;
      b.setAttribute('aria-disabled', 'true');
    } else {
      b.addEventListener('click', onClick);
    }
    return b;
  }

  // A "Title | control" row, which is the shape every dialog in this panel uses.
  function fieldRow(body, label, value, options) {
    const opts = options || {};
    const row = el('div', 'dialog-field');
    const text = el('label', null, label);
    const input = el('input');
    input.type = 'text';
    input.value = value == null ? '' : String(value);
    input.maxLength = opts.maxLength || 254;
    input.autocomplete = 'off';
    if (opts.readonly) {
      input.readOnly = true;
      input.title = opts.readonly;
    }
    text.htmlFor = input.id = 'field-' + label.toLowerCase().replace(/[^a-z0-9]+/g, '-');
    row.appendChild(text);
    row.appendChild(input);
    body.appendChild(row);
    if (opts.hint) body.appendChild(el('p', 'owui-hint', opts.hint));
    return input;
  }

  function checkRow(body, label, checked, blocked) {
    const row = el('label', 'dialog-check');
    const input = el('input');
    input.type = 'checkbox';
    input.checked = !!checked;
    row.appendChild(input);
    row.appendChild(el('span', null, label));
    if (blocked) {
      input.disabled = true;
      row.title = blocked;
    }
    body.appendChild(row);
    return input;
  }

  // --- the one question a table row can ask ----------------------------------
  // Every "what do you want to call it" in this panel goes through dialogPrompt,
  // and every irreversible one through dialogTypedConfirm, so there is one set of
  // words for each and no browser-native box anywhere.

  // Sign-in codes are four words from the appliance's curated vocabulary. The
  // list arrives on first use rather than at page load, so the ~12 KB is paid
  // only by whoever generates a code, and the URL's file name carries a locale
  // so another vocabulary can appear beside it without a code change. The words
  // themselves are public; only the *draw* has to happen here (D-27).
  const OTP_WORDS = 4;
  const WORDLIST_MIN = 512;
  let wordbook = null;

  async function loadWordbook() {
    if (wordbook) return wordbook;
    const res = await portalFetch('/_lemonseed/api/wordlist');
    if (!res.ok) throw new Error(await portalError(res, 'Could not load the word list.'));
    const words = (await res.text()).split('\n').filter((w) => w);
    if (words.length < WORDLIST_MIN) {
      throw new Error('The sign-in code word list is too small to be safe.');
    }
    wordbook = words;
    return words;
  }

  // Uniform index in [0, words.length). The cutoff is the largest multiple of
  // the list size that fits in 32 bits, and a draw past it is retried: folding
  // with plain modulo biases the low indices, which would quietly weaken every
  // code we hand out. Retry cost is under one extra draw in a thousand.
  function wordIndex(words) {
    const limit = Math.floor(4294967296 / words.length) * words.length;
    const draw = new Uint32Array(1);
    for (;;) {
      crypto.getRandomValues(draw);
      if (draw[0] < limit) return draw[0] % words.length;
    }
  }

  // Synchronous and takes its vocabulary as an argument on purpose: the entropy
  // source is the only thing it touches, so a test can pin crypto and prove that
  // no timestamp or counter contributes a byte.
  function makeOtp(words) {
    if (!Array.isArray(words) || words.length < WORDLIST_MIN) {
      throw new Error('The sign-in code word list is unavailable.');
    }
    const parts = [];
    for (let i = 0; i < OTP_WORDS; i += 1) parts.push(words[wordIndex(words)]);
    return parts.join('-');
  }

  function derive(username, code) {
    if (!window.LemonseedSRP || !LemonseedSRP.ok) {
      throw new Error('This browser failed a security self-check, so passwords cannot be set here.');
    }
    return LemonseedSRP.enroll(username, code);
  }

  // navigator.clipboard exists only in a secure context, and the portal is
  // plain HTTP on a LAN address, so the async API is usually absent here. The
  // textarea/execCommand path is deprecated but universal, and both run over
  // HTTP, so the button never reports success it did not achieve.
  function copyText(text, done) {
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(() => done(true), () => done(false));
      return;
    }
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.setAttribute('readonly', '');
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch (err) {
      ok = false;
    }
    ta.remove();
    done(ok);
  }

  // The code is shown once, in a box with a Copy button, and there is no way to
  // read it again: it is not stored anywhere, which is the same reason the
  // appliance never sees the words it is made of.
  function showCode(username, code, extraCopy) {
    const body = el('div');
    body.appendChild(el('p', 'otp-code', code));
    const copy = el('button', 'btn ghost', 'Copy');
    copy.type = 'button';
    copy.addEventListener('click', () => {
      copy.disabled = true;
      copyText(code, (ok) => {
        copy.disabled = false;
        copy.textContent = ok ? 'Copied' : 'Select it and copy it by hand';
      });
    });
    body.appendChild(copy);
    body.appendChild(el('p', 'owui-hint',
      'This code is not stored on the appliance, so write it down or copy it now.'));
    return dialog({
      title: 'Sign-in code for ' + username,
      copy: extraCopy || ('Give this to ' + username + '. It works once, and they choose '
        + 'their own password the first time they sign in.'),
      dismiss: false,
      body: body,
      actions: [{label: 'Done', kind: 'primary', value: true, primary: true}],
    });
  }

  // --- accounts ---------------------------------------------------------------

  function isAdmin(u) {
    return (u.groups || []).includes('admin');
  }

  // D-2: the appliance must always have someone who can administer it, so the
  // last enabled admin cannot demote themselves. A disabled admin does not count,
  // which is the same reading the server's guard uses; the panel says so before
  // anyone clicks Save.
  function isLastEnabledAdmin(u) {
    if (!isAdmin(u)) return false;
    return !people.some((other) => other !== u && isAdmin(other) && !other.disabled);
  }

  function userDialog(user) {
    const isNew = !user;
    const u = user || {};
    const self = !isNew && u.username === me;
    const fields = {};
    let armed = null;
    return dialog({
      title: isNew ? 'Add a user' : 'Edit ' + u.username,
      copy: isNew
        ? 'They get a sign-in code once, then choose their own password. You never '
          + 'need to know what it is, and it never passes through this page.'
        : 'Their sign-in name cannot change: it is what their chats and settings belong to.',
      dismiss: null,
      actions: [
        {label: 'Cancel', kind: 'ghost', value: null},
        {label: 'Save', kind: 'primary', primary: true, disabled: isNew,
         value: () => ({
           username: fields.username.value.trim().toLowerCase(),
           displayName: fields.displayName.value.trim(),
           email: fields.email.value.trim(),
           isAdmin: fields.isAdmin.checked,
           disabled: fields.isDisabled.checked,
         })},
      ],
      build: (api) => {
        armed = api.ok;
        fields.username = fieldRow(api.body, 'Sign-in name', isNew ? '' : u.username, {
          maxLength: 16,
          readonly: isNew ? null : 'The sign-in name cannot be changed',
          hint: isNew ? 'Lower-case letters, digits and . _ - , starting with a letter, '
            + '2 to 16 characters. Names are case-insensitive.' : null,
        });
        fields.displayName = fieldRow(api.body, 'Full name', u.display_name, {maxLength: 80});
        fields.email = fieldRow(api.body, 'E-mail address', u.email, {
          maxLength: 254,
          hint: 'Kept on the appliance. The chat interface uses '
            + (isNew ? 'their name' : u.username) + '@localhost as its own address.',
        });
        fields.isAdmin = checkRow(api.body, 'Administrator', isAdmin(u),
          (!isNew && isLastEnabledAdmin(u))
            ? 'This is the last admin who can sign in. Give someone else '
              + 'Administrator first.'
            : null);
        fields.isDisabled = checkRow(api.body, 'Cannot sign in', !!u.disabled,
          self ? 'You cannot disable the account you are signed in as.' : null);
        if (isNew) {
          // Same reason the typed delete confirm disables its own button: the
          // only way to press Save is to have written something that can work.
          const check = () => {
            armed.disabled = !USERNAME_RE.test(fields.username.value.trim().toLowerCase());
          };
          fields.username.addEventListener('input', check);
          check();
        }
      },
    });
  }

  async function saveUser(user, values) {
    if (!values || !values.username) return false;
    if (!user) {
      const code = makeOtp(await loadWordbook());
      const pair = derive(values.username, code);
      await post('/_lemonseed/api/users/create', {
        username: values.username,
        display_name: values.displayName,
        email: values.email,
        is_admin: values.isAdmin,
        salt: pair.salt,
        verifier: pair.verifier,
      });
      await showCode(values.username, code);
      return true;
    }
    // One field per request, so "which of these did you change" has exactly one
    // answer in the audit log (the control plane refuses two at once).
    const username = user.username;
    if (values.displayName !== (user.display_name || '')) {
      await post('/_lemonseed/api/users/update',
                 {username: username, display_name: values.displayName});
    }
    if (values.email !== (user.email || '')) {
      await post('/_lemonseed/api/users/update', {username: username, email: values.email});
    }
    if (values.isAdmin !== isAdmin(user)) {
      await post('/_lemonseed/api/users/groups', {
        username: username,
        groups: values.isAdmin
          ? Array.from(new Set((user.groups || []).concat('admin')))
          : (user.groups || []).filter((g) => g !== 'admin'),
      });
    }
    if (values.disabled !== !!user.disabled) {
      await post('/_lemonseed/api/users/disabled', {username: username, disabled: values.disabled});
    }
    return true;
  }

  function userRow(u) {
    const row = el('tr');

    const nameCell = el('td', 'col-user');
    nameCell.appendChild(el('span', 'user-name', u.username));
    const flags = [];
    if (u.disabled) flags.push('cannot sign in');
    if (u.password_reset_required) flags.push('must choose a new one');
    if (flags.length) nameCell.appendChild(el('span', 'acct-sub', flags.join(' · ')));
    row.appendChild(nameCell);

    const name = el('td');
    name.appendChild(el('span', null, u.display_name || ''));
    const memberOf = (u.groups || []).filter((g) => g !== 'users');
    if (memberOf.length) name.appendChild(el('span', 'acct-sub', memberOf.join(', ')));
    row.appendChild(name);

    const email = el('td', 'col-email', u.email || '');
    row.appendChild(email);

    const actions = el('td', 'col-actions');
    actions.appendChild(iconButton('edit', 'Edit ' + u.username, 'Edit this account', async () => {
      run(async () => { if (await saveUser(u, await userDialog(u))) note('Saved.'); }, null);
    }));
    actions.appendChild(iconButton('code', 'Give ' + u.username + ' a new sign-in code',
      'Replace their code: their current password stops working', async () => {
      const sure = await dialogConfirm(
        'Give ' + u.username + ' a new sign-in code?',
        (u.username === me
          ? 'This is the account you are signed in as. '
          : '')
        + 'Their current stops working, along with every device already signed in, '
        + 'and they set a new one at the next sign-in.',
        {confirmLabel: 'Generate a code'});
      if (!sure) return;
      run(async () => {
        const code = makeOtp(await loadWordbook());
        const pair = derive(u.username, code);
        await post('/_lemonseed/api/users/reset-password',
                   {username: u.username, salt: pair.salt, verifier: pair.verifier});
        await showCode(u.username, code);
      }, null);
    }));
    actions.appendChild(iconButton('remove', 'Delete ' + u.username, 'Delete this account', async () => {
      const go = await dialogTypedConfirm(
        'Delete ' + u.username + '?',
        'The account and their chat data go with it. This cannot be undone.',
        u.username, {confirmLabel: 'Delete account'});
      if (!go) return;
      run(() => post('/_lemonseed/api/users/delete', {username: u.username}), 'Account deleted.');
    }, u.username === firstAdmin
      ? 'The first admin cannot be deleted.'
      : (u.username === me ? 'You cannot delete the account you are signed in as.' : null)));
    row.appendChild(actions);
    return row;
  }

  // --- groups -----------------------------------------------------------------

  function groupMembers(g) {
    return (g.members || people.filter((u) => (u.groups || []).includes(g.name))
      .map((u) => u.username));
  }

  function membersDialog(g) {
    const current = groupMembers(g);
    const boxes = [];
    const body = el('div', 'dialog-scroll');
    people.forEach((u) => {
      const row = el('label', 'dialog-check');
      const box = el('input');
      box.type = 'checkbox';
      box.checked = current.includes(u.username);
      row.appendChild(box);
      row.appendChild(el('span', null, u.display_name && u.display_name !== u.username
        ? u.username + ' — ' + u.display_name
        : u.username));
      // Admin membership is the Administrator tick on the account, so the two
      // surfaces cannot disagree about who is an admin (D-2).
      if (g.name === 'admin') {
        box.disabled = isAdmin(u);
        if (isAdmin(u)) row.title = 'Admin membership is set on the account.';
      }
      body.appendChild(row);
      boxes.push({username: u.username, box: box});
    });
    if (!boxes.length) body.appendChild(el('p', 'owui-hint', 'There are no accounts yet.'));
    return dialog({
      title: 'Members of ' + g.name,
      copy: 'Whoever is in this group gets the settings that go with it.',
      dismiss: null,
      body: body,
      actions: [
        {label: 'Cancel', kind: 'ghost', value: null},
        {label: 'Save', kind: 'primary', primary: true,
         value: () => boxes.filter((b) => b.box.checked).map((b) => b.username)},
      ],
    });
  }

  // Today every attribute the appliance declares is a switch, so a checkbox is
  // the honest control. A key that wants a list or a string needs its own row
  // type; inventing a text box for a value the server will not accept that way
  // would be worse than not offering it.
  function groupSettingsDialog(g) {
    const boxes = [];
    const body = el('div');
    attributeKeys.forEach((key) => {
      const on = !!(g.attributes || {})[key];
      const row = el('label', 'dialog-check');
      const box = el('input');
      box.type = 'checkbox';
      box.checked = on;
      row.appendChild(box);
      row.appendChild(el('span', null, key.replace(/_/g, ' ')));
      body.appendChild(row);
      boxes.push({key: key, box: box, was: on});
    });
    if (!boxes.length) {
      body.appendChild(el('p', 'owui-hint', 'This group has no settings to change.'));
    }
    return dialog({
      title: 'Settings for ' + g.name,
      copy: 'These apply to everyone in the group.',
      dismiss: null,
      body: body,
      actions: [
        {label: 'Cancel', kind: 'ghost', value: null},
        {label: 'Save', kind: 'primary', primary: true, value: () => boxes},
      ],
    });
  }

  function groupRow(g) {
    const row = el('tr');
    const name = el('td', 'col-user', g.name);
    if (g.system) name.appendChild(el('span', 'acct-sub', 'built in'));
    row.appendChild(name);
    const members = groupMembers(g);
    row.appendChild(el('td', null, members.length + (members.length === 1 ? ' member' : ' members')));

    const actions = el('td', 'col-actions');
    if (!g.system) {
      actions.appendChild(iconButton('members', 'Members of ' + g.name,
        'Who is in this group', async () => {
        run(async () => {
          const chosen = await membersDialog(g);
          if (!chosen) return;
          const before = groupMembers(g);
          for (const username of before) {
            if (!chosen.includes(username)) {
              await post('/_lemonseed/api/groups/member',
                         {group: g.name, username: username, add: false});
            }
          }
          for (const username of chosen) {
            if (!before.includes(username)) {
              await post('/_lemonseed/api/groups/member',
                         {group: g.name, username: username, add: true});
            }
          }
        }, 'Membership saved.');
      }));
      actions.appendChild(iconButton('settings', 'Settings for ' + g.name,
        'The settings this group carries', async () => {
        run(async () => {
          const boxes = await groupSettingsDialog(g);
          if (!boxes) return;
          for (const item of boxes) {
            if (item.was === item.box.checked) continue;
            if (item.box.checked) {
              await post('/_lemonseed/api/groups/attribute',
                         {group: g.name, key: item.key, value: true});
            } else {
              await post('/_lemonseed/api/groups/attribute-remove',
                         {group: g.name, key: item.key});
            }
          }
        }, 'Group settings saved.');
      }));
      actions.appendChild(iconButton('remove', 'Delete ' + g.name, 'Delete this group', async () => {
        const go = await dialogTypedConfirm(
          'Delete the group ' + g.name + '?',
          'Nobody loses an account; the memberships and the settings on this group '
          + 'are removed.',
          g.name, {confirmLabel: 'Delete group'});
        if (!go) return;
        run(() => post('/_lemonseed/api/groups/delete', {group: g.name}), 'Group deleted.');
      }));
    }
    row.appendChild(actions);
    return row;
  }

  // --- audit log --------------------------------------------------------------

  function auditRow(e) {
    const row = el('div', 'row');
    row.appendChild(el('span', 'name', (e.at || '').slice(0, 16).replace('T', ' ')
      + ' · ' + (e.actor || '') + ' → ' + (e.target || '')));
    row.appendChild(el('span', 'state ok', e.action || ''));
    return row;
  }

  function csvField(value) {
    const text = value == null ? '' : (typeof value === 'object' ? JSON.stringify(value) : String(value));
    return '"' + text.replace(/"/g, '""') + '"';
  }

  // The download is built in the browser from the rows already fetched: the log
  // is small, it is admin-only, and a CSV endpoint would be a second way to read
  // the same table for the gateway to have to gate.
  function downloadCsv() {
    const lines = ['timestamp,actor,target,action,detail'];
    auditLog.forEach((e) => {
      lines.push([e.at, e.actor, e.target, e.action, e.detail].map(csvField).join(','));
    });
    const url = URL.createObjectURL(new Blob([lines.join('\r\n') + '\r\n'],
      {type: 'text/csv;charset=utf-8'}));
    const link = el('a');
    link.href = url;
    link.download = 'lemonseed-audit-log.csv';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function auditDialog() {
    const body = el('div', 'dialog-scroll');
    if (!auditLog.length) body.appendChild(el('p', 'owui-hint', 'Nothing recorded yet.'));
    auditLog.forEach((e) => body.appendChild(auditRow(e)));
    const answer = await dialog({
      title: 'Recent changes',
      copy: 'Who changed what, in the appliance’s own words. Kept for a year.',
      dismiss: null,
      body: body,
      actions: [
        {label: 'Download CSV', kind: 'ghost', value: 'csv'},
        {label: 'Close', kind: 'primary', value: null, primary: true},
      ],
    });
    if (answer === 'csv') downloadCsv();
  }

  async function loadAudit() {
    const box = document.getElementById('acct-audit-log');
    if (!box) return;
    const res = await portalFetch('/_lemonseed/api/audit');
    clear(box);
    if (!res.ok) {
      box.appendChild(el('p', 'owui-hint', await portalError(res, 'No log available.')));
      return;
    }
    auditLog = (await res.json()).events || [];
    if (!auditLog.length) {
      box.appendChild(el('p', 'owui-hint', 'Nothing recorded yet.'));
      return;
    }
    auditLog.slice(0, 5).forEach((e) => box.appendChild(auditRow(e)));
    if (auditLog.length > 5) {
      box.appendChild(el('p', 'owui-hint', 'Showing the 5 newest of ' + auditLog.length + '.'));
    }
  }

  // --- the screen -------------------------------------------------------------

  function render() {
    const list = clear(document.getElementById('acct-people'));
    people.forEach((u) => list.appendChild(userRow(u)));
    const glist = clear(document.getElementById('acct-groups'));
    groups.forEach((g) => glist.appendChild(groupRow(g)));
  }

  async function load() {
    const who = await portalFetch('/_lemonseed/api/profile');
    if (who.ok) {
      const self = await who.json();
      me = (self.user && self.user.username) || '';
    }
    const res = await portalFetch('/_lemonseed/api/users');
    if (!res.ok) {
      note(await portalError(res, 'Accounts are unavailable.'), true);
      return;
    }
    const data = await res.json();
    people = data.users || [];
    groups = data.groups || [];
    firstAdmin = data.first_admin || '';
    attributeKeys = data.attributes || [];
    render();
    await loadAudit();
  }

  // run() wraps a change: one busy flag for the panel, the server's own sentence
  // on a refusal, and a re-read afterwards so the table shows what the appliance
  // actually has rather than what the click asked for.
  function run(fn, done) {
    if (busy) return Promise.resolve();
    return (async () => {
      setBusy(true);
      note('');
      try {
        await fn();
        if (done) note(done);
        await load();
      } catch (err) {
        note(err.message || 'That did not work.', true);
      } finally {
        setBusy(false);
      }
    })();
  }

  function decorate(button, icon) {
    if (!button) return;
    button.prepend(svgIcon(icon));
  }

  function wire() {
    decorate(document.getElementById('acct-add-user'), 'add');
    decorate(document.getElementById('group-add-button'), 'add');

    const add = document.getElementById('acct-add-user');
    if (add) {
      add.addEventListener('click', () => {
        run(async () => {
          const values = await userDialog(null);
          if (!values) return;
          if (!USERNAME_RE.test(values.username)) {
            throw new Error('That sign-in name cannot be used.');
          }
          if (await saveUser(null, values)) note('Account added.');
        }, null);
      });
    }
    const newGroup = document.getElementById('group-add-button');
    if (newGroup) {
      newGroup.addEventListener('click', () => {
        run(async () => {
          const name = await dialogPrompt('Add a group',
            'Lower-case, no spaces. It appears in the list straight away.',
            {label: 'Group name', confirmLabel: 'Add group', maxLength: 32});
          if (!name) return;
          await post('/_lemonseed/api/groups/create', {group: name.trim().toLowerCase()});
        }, 'Group added.');
      });
    }
    const audit = document.getElementById('acct-audit');
    if (audit) audit.addEventListener('click', () => { auditDialog(); });
  }

  if (document.getElementById('accounts')) {
    wire();
    load().catch((err) => note(err.message, true));
  }
})();
"""

# My Profile (/profile/), reachable by anyone signed in. Three things live here
# and nothing else: your name and address, your password, and a read-only look at
# your role and last sign-in. The Settings panel is an admin's room; this is
# everybody's.
PROFILE_JS = r"""
(function () {
  'use strict';

  let me = null;

  const noteEl = document.getElementById('profile-note');

  function note(text, bad) {
    noteEl.textContent = text || '';
    noteEl.className = bad ? 'owui-status bad' : 'owui-status';
    // An empty status pill still draws its border, so an idle page shows a
    // mystery box; keep it out of the layout until it has something to say.
    noteEl.hidden = !text;
  }

  function setBusy(on) {
    document.querySelectorAll('#profile-panel button, #profile-panel input').forEach((b) => {
      b.disabled = on;
    });
  }

  async function post(path, payload) {
    const res = await portalFetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(await portalError(res, 'That did not work.'));
    return res.json();
  }

  // The appliance stamps UTC; showing a browser-local reinterpretation of it
  // would be a guess about a time zone the appliance never claimed.
  function stamp(iso) {
    const text = String(iso || '');
    return text.length >= 16 ? text.slice(0, 16).replace('T', ' ') + ' UTC' : '';
  }

  function fill(user) {
    me = user;
    document.getElementById('profile-name').value = user.display_name || '';
    document.getElementById('profile-email').value = user.email || '';
    // Plain text only, always: a display name is free text somebody typed.
    document.getElementById('profile-user').value = user.username || '';
    document.getElementById('profile-role').textContent =
      (user.groups || []).indexOf('admin') >= 0 ? 'Administrator' : 'Member';
    document.getElementById('profile-last').textContent = stamp(user.last_login) || 'Never';
  }

  async function load() {
    const res = await portalFetch('/_lemonseed/api/profile');
    if (!res.ok) {
      note(await portalError(res, 'Your details are unavailable.'), true);
      return;
    }
    fill((await res.json()).user || {});
  }

  async function save(ev) {
    ev.preventDefault();
    if (!me) return;
    const name = document.getElementById('profile-name').value.trim();
    const email = document.getElementById('profile-email').value.trim();
    // One field per request, because that is what the route accepts, and because
    // a refusal then names the field that caused it instead of the form.
    const wanted = [];
    if (name !== (me.display_name || '')) wanted.push(['display_name', name]);
    if (email !== (me.email || '')) wanted.push(['email', email]);
    if (!wanted.length) {
      note('Nothing to save.');
      return;
    }
    setBusy(true);
    note('');
    try {
      for (let i = 0; i < wanted.length; i += 1) {
        await post('/_lemonseed/api/profile', { [wanted[i][0]]: wanted[i][1] });
      }
      note('Saved.');
      await load();
    } catch (err) {
      note(err.message || 'That change could not be saved.', true);
    } finally {
      setBusy(false);
    }
  }

  async function changePassword(ev) {
    ev.preventDefault();
    if (!window.LemonseedSRP || !LemonseedSRP.ok) {
      note('This browser failed a security self-check, so passwords cannot be changed here.', true);
      return;
    }
    if (!me) return;
    const current = document.getElementById('pw-current').value;
    const next = document.getElementById('pw-next').value;
    const again = document.getElementById('pw-again').value;
    if (next.length < 8) { note('Your new password must be at least 8 characters.', true); return; }
    if (next !== again) { note('The two new passwords do not match.', true); return; }
    if (current === next) { note('That is the same password.', true); return; }
    setBusy(true);
    note('');
    try {
      await LemonseedRotate.change(post, me.username, current, next);
      // Everything else was torn down by the rotation, including this session;
      // the new cookie came back with the response, so say only what happened.
      document.getElementById('password-form').reset();
      note('Password changed.');
    } catch (err) {
      note(err.message || 'The password was not changed.', true);
    } finally {
      setBusy(false);
    }
  }

  document.getElementById('profile-form').addEventListener('submit', save);
  document.getElementById('password-form').addEventListener('submit', changePassword);
  load();
})();
"""

# In-flight SRP handshakes. Process-local on purpose: ``b`` is never persisted,
# so a control-plane restart costs a client one retry instead of putting a
# handshake secret on disk next to the verifiers.
PENDING_HANDSHAKES = srp.PendingStore()

# Account- and group-management actions, as the trailing path segment of
# /_lemonseed/api/users/<action>. Listed rather than dispatched by reflection:
# an unknown action must 404 from a fixed set, not reach a function someone
# added later and never meant to be reachable over HTTP.
USER_ACTIONS = frozenset(
    {"create", "update", "groups", "disabled", "reset-password", "delete"}
)
GROUP_ACTIONS = frozenset(
    {"create", "delete", "member", "attribute", "attribute-remove"}
)


def _srp_fold_secret() -> bytes:
    """Seed for the unknown-account simulation, with a stable fallback.

    Before the first admin exists there is no session secret and no account to
    enumerate, so the fallback only has to be *consistent*, not secret. Deriving
    it from a constant keeps the simulated path well-formed rather than raising
    inside a challenge handler.
    """
    return auth.fold_secret() or b"lemonseed-srp-fold-secret-unbootstrapped"


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
    srp_enroll_js = ""
    if auth_admin_required:
        # The password input is still here, but its value never leaves the
        # browser: SRP_ENROLL_JS derives (salt, verifier), writes them into the
        # hidden fields, and disables the password input before the native
        # submit runs, so it is not in the body at all.
        admin_fields = """
<label for="admin_display_name">Your name</label>
<input id="admin_display_name" name="admin_display_name" maxlength="80" autocomplete="off" placeholder="Optional"/>
<label for="admin_username">Admin username</label>
<input id="admin_username" name="admin_username" maxlength="16" autocomplete="off" value="admin" required/>
<label for="admin_password">Admin password</label>
<input id="admin_password" name="admin_password" type="password" minlength="8" maxlength="128" autocomplete="new-password" required/>
<input id="admin_srp_salt" name="admin_srp_salt" type="hidden"/>
<input id="admin_srp_verifier" name="admin_srp_verifier" type="hidden"/>
<p class="err" id="setup-srp-error"></p>
"""
        srp_enroll_js = f"<script>{SRP_CLIENT_JS}</script><script>{SRP_ENROLL_JS}</script>"
    review_html = f"""<section class="panel">
<h2>{heading}</h2>
<form id="setup-form" method="POST" action="/setup" autocomplete="off">
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
{srp_enroll_js}
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
<script>{SRP_CLIENT_JS}</script>
<script>{SRP_LOGIN_JS}</script>
</body></html>"""
    return body.encode()


def app_bar(name: str, *, view: str = "chat", metrics: bool = True) -> str:
    """The appliance's top bar.

    Two documents render it (the app shell and the profile page), so it is one
    function rather than two copies that drift the first time a control moves.

    ``metrics`` is off on pages that have no script to fill the bars; a load
    meter pinned at zero would be a lie, not decoration.
    """
    chat_active = "active" if view == "chat" else ""
    gear_active = "active" if view == "settings" else ""
    meter = """
<div class="metrics" aria-hidden="true">
  <div class="metric" title="System load (1 min)">
    <label>Load</label>
    <div class="bar"><span id="metric-load" style="width:0%"></span></div>
  </div>
  <div class="metric" title="Storage used on /var/lib/lemonseed">
    <label>Storage</label>
    <div class="bar"><span id="metric-storage" style="width:0%"></span></div>
  </div>
</div>""" if metrics else ""

    return f"""<header class="app-bar">
<img class="brand-logo" src="/_lemonseed/brand/lemon-64.png" width="28" height="28" alt=""/>
<p class="title">{name}</p>
<nav class="nav" aria-label="Main">
  <a href="/" id="nav-chat" class="{chat_active}" data-view="chat">Chat</a>
  <span class="disabled" title="Coming soon">Modules</span>
</nav>
<span class="spacer"></span>{meter}
<a href="/settings/" id="nav-settings" class="gear {gear_active}" title="Settings" data-view="settings">⚙</a>
<span class="pop-wrap">
<button id="nav-account" class="gear" title="Account" aria-label="Account" aria-haspopup="true" aria-expanded="false"><svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path fill="currentColor" d="M12 12a5 5 0 1 0-5-5a5 5 0 0 0 5 5Zm0 2c-4.4 0-8 2.2-8 5v1h16v-1c0-2.8-3.6-5-8-5Z"/></svg></button>
<span id="account-pop" class="popover" hidden>
  <span class="pop-name" id="pop-name"></span>
  <span class="pop-user" id="pop-user"></span>
  <a href="/profile/" id="pop-profile">Edit profile</a>
  <button id="nav-logout" type="button">Log out</button>
</span>
</span>
</header>"""


def page_shell(snap: dict[str, Any], *, view: str = "chat") -> bytes:
    """Post-setup app shell: persistent toolbar + keep-alive iframes."""
    setup = load_setup()
    name = escape(str(setup.get("name") or "Lemonseed"))
    mods = snap.get("modules") or modules.modules_snapshot()
    _owui = mods.get("open_webui") or {}
    if view not in ("chat", "settings", "modules"):
        view = "chat"

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
{app_bar(name, view=view)}
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
<script>{ACCOUNT_JS}</script>
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
      workflow: 'Rebooting',
      task: 'The appliance is rebooting — this page will reconnect automatically…',
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
// Log out lives in the account popover (ACCOUNT_JS), which retires the chat
// frame before navigating; signing out is not the shell's own concern.
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


def page_profile(snap: dict[str, Any]) -> bytes:
    """My Profile (``/profile/``): the one page every signed-in person can reach.

    Deliberately small, and deliberately not a shrunken Settings panel. A
    non-admin gets chat and this page, so everything on it is something they can
    act on for themselves; role and last sign-in are shown because people ask,
    not because they are actionable here.

    The username is rendered into a disabled field rather than a label so it can
    be selected and copied, which is what people want from it, and it is filled
    from the API response rather than from a cookie the browser cannot read.
    """
    setup = load_setup()
    name = escape(str(setup.get("name") or "Lemonseed"))
    # A member cannot reach the settings panel, so this row is the only place they
    # can see which model the appliance is answering from. Read-only on purpose:
    # the model is one choice for the whole household, and whoever changes it
    # changes it for everyone, so that button belongs to an admin.
    chat = chat_ready.chat_snapshot()
    model = escape(str(chat.get("display_name") or chat.get("model_id") or ""))
    model_state = "ok" if model else "wait"
    model_label = model or "not chosen yet"
    body = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{name} — Profile</title>{brand_head_links()}<link rel="stylesheet" href="/_lemonseed/lemonseed.css"/>
</head><body>
<main class="app-shell">
{app_bar(name, metrics=False)}
<div class="frame-wrap">
<section class="panel profile-panel" id="profile-panel">
<h2>My profile</h2>
<p class="owui-status" id="profile-note" hidden></p>
<form id="profile-form">
<div class="field"><label for="profile-user">Username</label><input id="profile-user" type="text" maxlength="16" autocomplete="off" disabled/></div>
<div class="field"><label for="profile-name">Name</label><input id="profile-name" name="display_name" type="text" maxlength="80" autocomplete="off"/></div>
<div class="field"><label for="profile-email">E-mail</label><input id="profile-email" name="email" type="text" maxlength="254" autocomplete="off"/></div>
<button type="submit" class="btn primary">Save</button>
</form>
</section>
<section class="panel profile-panel" id="profile-password-panel">
<h2>Change password</h2>
<p class="lede">Lemonseed never sends your password over the network; passwords are converted to
single-use passcodes before transmission to the Lemonseed appliance.</p>
<form id="password-form">
<div class="field"><label for="pw-current">Current password</label><input id="pw-current" name="current-password" type="password" autocomplete="current-password" required/></div>
<div class="field"><label for="pw-next">New password</label><input id="pw-next" name="new-password" type="password" autocomplete="new-password" minlength="8" required/></div>
<div class="field"><label for="pw-again">New password again</label><input id="pw-again" name="confirm-password" type="password" autocomplete="new-password" minlength="8" required/></div>
<button type="submit" class="btn primary">Change password</button>
</form>
<h3 class="subhead">Account</h3>
<div class="row"><span class="name">Role</span><span class="state" id="profile-role">…</span></div>
<div class="row"><span class="name">Last sign-in</span><span class="state" id="profile-last">…</span></div>
<div class="row"><span class="name">Chat model</span><span class="state {model_state}" id="profile-model">{model_label}</span></div>
</section>
</div>
</main>
<script>{PORTAL_FETCH_JS}</script>
<script>{SRP_CLIENT_JS}</script>
<script>{ROTATE_JS}</script>
<script>{ACCOUNT_JS}</script>
<script>{PROFILE_JS}</script>
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
  // the CP, and the dialog keeps its own OK disabled until it is typed.
  const phrase = document.getElementById('owui-reset').dataset.confirm || '';
  const go = await dialogTypedConfirm(
    'Reset the chat interface settings?',
    'Lemonseed rebuilds the chat interface settings from its own defaults. Chats, '
    + 'models and accounts are kept; settings changed inside Open WebUI are lost.',
    phrase, { confirmLabel: 'Reset settings' });
  if (!go) return;
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


# --- The settings control panel: one rail, five panes (M7) -------------------

# Bare-metal review finding: every control the admin has was stacked in one tall
# column inside a 42rem iframe, so "change the model" and "add a person" were two
# scrolls apart and nothing read as a control panel. This is a rail of tabs down
# the left and one pane of controls on the right.
#
# The rail lives *inside* /settings/content rather than in the portal shell on
# purpose. The chat view is an Open WebUI iframe that already owns a left rail,
# and both of them auto-hide; two rails side by side is a mess you only find by
# using the box. The shell keeps its top bar for chat / settings / profile, and
# only the settings document grows a rail of its own.

# Hand-drawn 24x24 stroke icons, not an icon font and not emoji: they inherit
# currentColor so the selected tab stays legible, they cost no request at page
# load, and an emoji renders in whichever font that OS happens to have.
def _icon(paths: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor"'
        ' stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"'
        f' aria-hidden="true">{paths}</svg>'
    )


ICON_GENERAL = _icon(
    '<path d="M4 7h7.6"/><path d="M16.8 7H20"/><circle cx="14.2" cy="7" r="2.2"/>'
    '<path d="M4 12h2.8"/><path d="M12 12H20"/><circle cx="9.4" cy="12" r="2.2"/>'
    '<path d="M4 17h7.6"/><path d="M16.8 17H20"/><circle cx="14.2" cy="17" r="2.2"/>'
)
ICON_MODELS = _icon(
    '<rect x="7" y="7" width="10" height="10" rx="2"/>'
    '<rect x="10.4" y="10.4" width="3.2" height="3.2" rx="0.8"/>'
    '<path d="M9.8 4v3M14.2 4v3M9.8 17v3M14.2 17v3'
    'M4 9.8h3M4 14.2h3M17 9.8h3M17 14.2h3"/>'
)
ICON_PEOPLE = _icon(
    '<circle cx="9.6" cy="8.6" r="3.2"/>'
    '<path d="M3.8 19.6c0-3.2 2.6-5.2 5.8-5.2s5.8 2 5.8 5.2"/>'
    '<path d="M16 5.8a3.2 3.2 0 0 1 0 5.9"/>'
    '<path d="M17.4 14.6c2.1.7 3.4 2.5 3.4 4.7"/>'
)
ICON_CHAT = _icon(
    '<path d="M20.4 14.5a2.4 2.4 0 0 1-2.4 2.4H9.1l-4.6 3.5V6.9a2.4 2.4 0 0 1 2.4-2.4h11.1'
    'a2.4 2.4 0 0 1 2.4 2.4z"/>'
)
ICON_MODULES = _icon(
    '<rect x="4" y="4" width="7" height="7" rx="1.6"/><rect x="13" y="4" width="7" height="7" rx="1.6"/>'
    '<rect x="4" y="13" width="7" height="7" rx="1.6"/><rect x="13" y="13" width="7" height="7" rx="1.6"/>'
)
ICON_POWER = _icon('<path d="M12 3.6v8"/><path d="M17.9 7.4a8 8 0 1 1-11.8.04"/>')

# (pane suffix, rail label, icon). The order is the rail's order; the label must
# fit one line at the narrowest desktop width, because a wrapped rail label makes
# every row a different height and reads as broken.
SETTINGS_TABS: tuple[tuple[str, str, str], ...] = (
    ("general", "General", ICON_GENERAL),
    ("models", "AI Models", ICON_MODELS),
    ("people", "Users & Groups", ICON_PEOPLE),
    ("chat", "Open WebUI", ICON_CHAT),
    ("modules", "Modules", ICON_MODULES),
)
SETTINGS_FIRST_TAB = SETTINGS_TABS[0][0]


def settings_rail() -> str:
    """The rail's buttons. aria-selected is all false here; the pane the server
    renders visible and the selection the script makes are reconciled by
    ``SETTINGS_JS`` on load, before the first paint the admin can see.
    """
    return "\n".join(
        f'<button type="button" class="rail-item" role="tab" id="tab-{pane}"'
        f' data-pane="{pane}" aria-controls="pane-{pane}" aria-selected="false">'
        f'{icon}<span class="rail-label">{escape(label)}</span></button>'
        for pane, label, icon in SETTINGS_TABS
    )


# Plain strings (not f-strings) on purpose: JS braces stay single.
SETTINGS_JS = r"""
// Rail switching. Every pane is already in the served HTML and only one is
// unhidden, so the page reads with JS off and the smoke test can assert on
// markup no browser had to produce.
const SETTINGS_PANES = ['general', 'models', 'people', 'chat', 'modules'];
const SETTINGS_TAB_KEY = 'lemonseed-settings-tab';

function selectSettingsTab(pane, remember) {
  if (!SETTINGS_PANES.includes(pane)) pane = 'general';
  for (const name of SETTINGS_PANES) {
    const tab = document.getElementById('tab-' + name);
    const box = document.getElementById('pane-' + name);
    if (!tab || !box) continue;
    const on = name === pane;
    tab.setAttribute('aria-selected', on ? 'true' : 'false');
    tab.tabIndex = on ? 0 : -1;
    box.hidden = !on;
  }
  // Not on first load: a fresh browser tab should start at General, and writing
  // the default here would make that permanent.
  if (remember !== false) {
    try { sessionStorage.setItem(SETTINGS_TAB_KEY, pane); } catch (err) {}
  }
}

function settingsTabFromUrl() {
  const hash = (window.location.hash || '').replace('#', '');
  if (SETTINGS_PANES.includes(hash)) return hash;
  try {
    const saved = sessionStorage.getItem(SETTINGS_TAB_KEY);
    if (SETTINGS_PANES.includes(saved)) return saved;
  } catch (err) {}
  return 'general';
}

function initSettingsRail() {
  const rail = document.querySelector('.settings-rail');
  if (!rail) return;
  rail.addEventListener('click', (ev) => {
    const item = ev.target.closest('.rail-item');
    if (item) selectSettingsTab(item.dataset.pane);
  });
  // A tablist with a roving tabindex: Tab leaves the rail, arrows move inside it.
  rail.addEventListener('keydown', (ev) => {
    const item = ev.target.closest('.rail-item');
    if (!item) return;
    const items = Array.from(rail.querySelectorAll('.rail-item'));
    const at = items.indexOf(item);
    let next = -1;
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowRight') next = (at + 1) % items.length;
    else if (ev.key === 'ArrowUp' || ev.key === 'ArrowLeft') next = (at - 1 + items.length) % items.length;
    else if (ev.key === 'Home') next = 0;
    else if (ev.key === 'End') next = items.length - 1;
    if (next < 0) return;
    ev.preventDefault();
    items[next].focus();
    selectSettingsTab(items[next].dataset.pane);
  });
  selectSettingsTab(settingsTabFromUrl(), false);
}
initSettingsRail();
"""

# Power controls (P5). Only the two the appliance can do honestly today; update
# affordances stay in P5's later phases. Admin-only at the gateway *and* in the
# control plane, so a member gets a 403 rather than a greyed-out mystery.
POWER_JS = r"""
function powerNote(text, bad) {
  const note = document.getElementById('power-note');
  if (!note) return;
  note.textContent = text || '';
  note.hidden = !text;
  note.className = 'owui-status' + (bad ? ' bad' : '');
}

function powerDisable() {
  // The box is on its way out. Leaving the buttons live invites a second click
  // that cannot do anything except fail confusingly.
  for (const id of ['power-reboot', 'power-shutdown']) {
    const button = document.getElementById(id);
    if (button) button.disabled = true;
  }
}

async function powerWatch(action) {
  // "It is rebooting" is only true once the box stops answering, so say it when
  // the poll goes quiet and reload when it answers again. /api/chat, not the
  // open /api/status: this page already polls it, and the open subset is the
  // installer's handoff, not the portal's (test_portal_fetch holds that line).
  let offline = false;
  const timer = setInterval(async () => {
    const res = await portalFetch('/api/chat');
    if (!res.ok) {
      if (offline) return;
      offline = true;
      powerNote(action === 'shutdown'
        ? 'The appliance is off. Press its power button to start it again.'
        : 'The appliance is rebooting. This page reconnects when it is back.',
        action !== 'shutdown');
      return;
    }
    if (offline) {
      clearInterval(timer);
      window.top.location.reload();
    }
  }, 2000);
}

const POWER_COPY = {
  reboot: {
    title: 'Reboot the appliance?',
    copy: 'Anything running now stops, and the appliance comes back on its own in '
      + 'a minute or two. Chats, models and settings are not changed.',
    confirm: 'Reboot now',
    refused: 'The appliance refused to reboot.',
  },
  shutdown: {
    title: 'Shut down the appliance?',
    copy: 'The appliance will stay off, with no lights and no network, until someone '
      + 'presses its power button. Chats, models and settings are not changed.',
    confirm: 'Shut down',
    refused: 'The appliance refused to shut down.',
  },
};

async function powerAct(action) {
  const copy = POWER_COPY[action];
  if (!copy) return;
  const go = await dialogConfirm(copy.title, copy.copy, {
    confirmLabel: copy.confirm, cancelLabel: 'Stay on', danger: action === 'shutdown',
  });
  if (!go) return;
  powerNote('');
  let res = null;
  try {
    res = await portalFetch('/_lemonseed/api/power', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: action}),
    });
  } catch (err) {
    res = null;
  }
  if (res === null) {
    powerNote('Could not reach the control plane.', true);
    return;
  }
  if (!res.ok) {
    // 409 carries the sentence naming the work in flight (a model download, an
    // Open WebUI window). Rendering it verbatim is the whole point of the refusal.
    powerNote(await portalError(res, copy.refused), true);
    return;
  }
  powerDisable();
  powerNote(action === 'shutdown' ? 'Shutting down…' : 'Rebooting…');
  powerWatch(action);
}

function initPower() {
  for (const action of ['reboot', 'shutdown']) {
    const button = document.getElementById('power-' + action);
    if (!button) continue;
    button.addEventListener('click', () => { powerAct(action); });
  }
}
initPower();
"""


# The SYSTEM panel's Version section (M7 P5): running vs latest, Check now,
# Download and install, and the failed-update banner. The updater is run as a
# job server-side; this only renders the snapshot and starts jobs.
UPDATE_JS = r"""
(function () {
  'use strict';

  const note = document.getElementById('update-note');
  const failedBox = document.getElementById('update-failed');
  const checkButton = document.getElementById('update-check');
  const install = document.getElementById('update-install');
  const priorRow = document.getElementById('version-prior-row');
  const rollback = document.getElementById('update-rollback');
  const channelSelect = document.getElementById('update-channel');
  if (!note || !failedBox || !checkButton || !install || !priorRow || !rollback) return;

  // The one status line under the buttons. It was named after the power
  // panel's powerNote and the rename never landed here — hence the silent
  // dead panel on the first error (bare-metal review, BM-3).
  function noteIt(text, bad) {
    if (!note) return;
    note.textContent = text;
    note.classList.toggle('bad', !!bad);
    note.hidden = !text;
  }

  let poll = null;
  let known = null;      // the last snapshot render() saw
  let watching = null;   // the install dialog's poll, independent of render's

  function busy(s) {
    const state = (s.job && s.job.state) || 'idle';
    return state === 'running' || state === 'downloading' || state === 'writing'
      || state === 'rebooting';
  }

  function render(s) {
    known = s;
    const job = s.job || {state: 'idle', detail: ''};
    document.getElementById('version-running').textContent = s.running || 'unknown';
    document.getElementById('version-latest').textContent = s.latest || 'not checked';
    if (s.failed) {
      failedBox.textContent = 'The update to ' + s.failed.intended + ' did not land; the '
        + 'appliance is running ' + (s.failed.running || 'an earlier version') + '.';
      failedBox.hidden = false;
    } else {
      failedBox.hidden = true;
    }
    const working = busy(s);
    install.disabled = !s.available || working;
    checkButton.disabled = working;
    priorRow.hidden = !s.prior;
    if (s.prior) document.getElementById('version-prior').textContent = s.prior;
    // The other slot is not always older: after a rollback it holds the newer
    // release, so the affordance reads Roll forward and the row is the newer
    // version, not the prior one.
    const forward = s.rollback_kind === 'forward';
    document.getElementById('version-prior-name').textContent =
      forward ? 'Newer version' : 'Prior version';
    rollback.textContent = forward ? 'Roll forward' : 'Roll back';
    channelSelect.value = s.channel || 'stable';
    channelSelect.disabled = working;
    // Hidden while a failed-update banner is up: the failed attempt is what
    // the other slot holds, and rolling back onto it makes no sense until the
    // banner is dismissed (Retry or Cancel).
    rollback.hidden = !s.rollback_eligible || !!s.failed;
    rollback.disabled = working;
    if (working) {
      noteIt(job.detail || 'Working…');
      if (!poll) poll = setInterval(refresh, 2000);
    } else {
      if (job.state === 'failed') noteIt(job.detail || 'The update failed.', true);
      else if (job.state === 'rebooting') noteIt('Rebooting to apply the update…');
      else noteIt('');   // done/idle: the in-progress note goes with the job
      if (poll) {
        clearInterval(poll);
        poll = null;
      }
    }
  }

  async function refresh() {
    let state = null;
    try {
      const res = await portalFetch('/_lemonseed/api/update');
      if (res.ok) state = await res.json();
    } catch (err) {
      // The box may be on its way down mid-job; the going-away page covers it.
      return;
    }
    if (state) render(state);
  }

  async function post(url, body) {
    let res = null;
    try {
      res = await portalFetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body || {}),
      });
    } catch (err) {
      res = null;
    }
    if (res === null) {
      noteIt('Could not reach the control plane.', true);
      return false;
    }
    if (!res.ok) {
      noteIt(await portalError(res, 'That did not work.'), true);
      return false;
    }
    return true;
  }

  checkButton.addEventListener('click', async () => {
    // Say something the moment the button is pressed: the check runs as a
    // server-side job, so the answer arrives through the poll, not this POST.
    checkButton.disabled = true;
    noteIt('Checking for an update…');
    if (await post('/_lemonseed/api/update/check')) refresh();
    else checkButton.disabled = false;
  });

  // One dialog held open across download, write, and reboot (bare-metal
  // review, BM-3). The updater's own stdout percentages ride the job status,
  // so the bar is the updater's opinion, not a fake timer.
  // `onRetry` re-arms the flow after a failure: the failed dialog offers
  // Retry/Cancel (bare-metal review, BM-4) because at that point the job is
  // over — Hide made sense while something was still running, and the
  // server-side retry path cancels the leftover attempt record first.
  function openProgressDialog(onRetry, headline) {
    const heading = headline || 'Updating the appliance';
    const line = document.createElement('p');
    line.className = 'dialog-copy';
    line.textContent = 'Starting the update…';
    const bar = document.createElement('div');
    bar.className = 'progress';
    const fill = document.createElement('span');
    fill.style.width = '0%';
    bar.appendChild(fill);
    let close = null;
    dialog({
      title: heading,
      body: (function () {
        const wrap = document.createElement('div');
        wrap.appendChild(line);
        wrap.appendChild(bar);
        return wrap;
      })(),
      dismiss: null,
      // Hide, not Cancel: the job keeps running either way, and the page
      // still redirects to the sign-in screen when the box comes back.
      actions: [{label: 'Hide', kind: 'ghost', value: false}],
      build: function (api) {
        close = function () { api.choose(false); };
      },
    });
    return {
      set: function (text, pct) {
        line.textContent = text;
        if (pct !== null && pct !== undefined) fill.style.width = pct + '%';
      },
      fail: function (text) {
        line.textContent = text;
        line.classList.add('bad');
        bar.hidden = true;
        if (close) close();
        dialog({
          title: 'Update failed',
          copy: text,
          dismiss: null,
          actions: [
            {label: 'Cancel', kind: 'ghost', value: false},
            {label: 'Retry', kind: 'primary', value: true},
          ],
        }).then(function (retry) {
          if (retry && onRetry) onRetry();
        });
      },
      close: function () {
        if (close) close();
      },
    };
  }

  function watchInstall(ui) {
    let sawRebooting = false;
    const timer = setInterval(async () => {
      let s = null;
      try {
        const res = await portalFetch('/_lemonseed/api/update');
        if (res.ok) s = await res.json();
      } catch (err) {
        // The box going down mid-reboot is the expected silence.
      }
      if (!s) {
        if (sawRebooting) return;   // keep waiting through the outage
        return;
      }
      const job = s.job || {};
      const state = job.state || 'idle';
      if (state === 'failed') {
        clearInterval(timer);
        ui.fail(job.detail || 'The update failed; nothing was changed on boot.');
        return;
      }
      if (state === 'done') {
        // A finished job (e.g. a check another panel action started) is not
        // ours to watch; close the dialog instead of parking at "Working…".
        clearInterval(timer);
        ui.close();
        return;
      }
      if (state === 'rebooting') {
        sawRebooting = true;
        ui.set('Update finished; rebooting to apply it…', 100);
        return;
      }
      if (sawRebooting) {
        // The status API answers again after the outage: the new version is
        // up. Sessions survive the reboot, but the update deserves a clean
        // sign-in, so the browser goes to the login screen (BM-3).
        clearInterval(timer);
        window.top.location = '/login';
        return;
      }
      if (state === 'downloading' || state === 'writing' || state === 'running') {
        ui.set(job.detail || 'Working…', job.progress);
      }
    }, 2000);
  }

  install.addEventListener('click', async () => {
    if (!known || !known.available) return;
    const copy = 'The appliance will be updated from ' + known.running + ' to '
      + known.latest + '. The update will be downloaded and the appliance will '
      + 'reboot automatically; this can take a few minutes, which will interrupt '
      + 'active sessions. Chats, models and settings are always preserved.';
    const go = await dialogConfirm('Download and install the update?', copy,
                                   {confirmLabel: 'Install'});
    if (!go) return;
    runUpdate(false);
  });

  rollback.addEventListener('click', async () => {
    if (!known || !known.rollback_eligible) return;
    const forward = known.rollback_kind === 'forward';
    const verb = forward ? 'Roll forward' : 'Roll back';
    const copy = 'The appliance will boot release ' + known.prior
      + ' and reboot automatically. Chats, models and settings are preserved; '
      + 'the release you are leaving stays published, but update checks will '
      + 'not offer it again until a newer one appears.';
    const go = await dialogConfirm(verb + ' to ' + known.prior + '?', copy,
                                   {confirmLabel: verb});
    if (!go) return;
    const ui = openProgressDialog(function () {},
      forward ? 'Rolling the appliance forward…' : 'Rolling back the appliance…');
    if (await post('/_lemonseed/api/update/rollback')) {
      watchInstall(ui);
    } else {
      ui.fail(note.textContent || 'The appliance refused to start the rollback.');
    }
  });

  // The channel switch is a settings change, not an update: it takes effect
  // on the next check, and switching to Beta is confirmed because early
  // releases are the point of the channel.
  channelSelect.addEventListener('change', async () => {
    const value = channelSelect.value;
    const current = (known && known.channel) || 'stable';
    if (!value || value === current) return;
    if (value === 'beta') {
      const go = await dialogConfirm('Switch to the Beta channel?',
        'The Lemonseed Beta release channel contains early releases that may '
        + 'not be fully tested; use at your own risk.',
        {confirmLabel: 'Switch to Beta', cancelLabel: 'Remain on Stable'});
      if (!go) { channelSelect.value = 'stable'; return; }
    }
    const ui = await post('/_lemonseed/api/update/channel', {channel: value});
    if (!ui.ok) {
      channelSelect.value = current;
      noteIt(ui.data.detail || 'The appliance refused to change the channel.', true);
      return;
    }
    refresh();
  });

  // One attempt: open the progress box, start the job, watch it. A failed job
  // hands the user Retry/Cancel; Retry comes back here with the retry flag,
  // which makes the updater cancel the leftover attempt record before it
  // re-applies — and the download is re-fetched over the corrupt file.
  async function runUpdate(retry) {
    const ui = openProgressDialog(function () { runUpdate(true); });
    if (await post('/_lemonseed/api/update', retry ? {retry: true} : {})) {
      watchInstall(ui);
    } else {
      ui.fail(note.textContent || 'The appliance refused to start the update.');
    }
  }

  refresh();
})();
"""


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
    # The Version section renders the running version server-side (the same
    # value /api/system carries); "latest" is filled by the panel's JS, which
    # polls /_lemonseed/api/update — the one place the check result lives.
    running_version = escape(str(snap.get("version") or "unknown"))

    body = f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Settings</title>{brand_head_links()}<link rel="stylesheet" href="/_lemonseed/lemonseed.css"/>
</head><body>
<main class="settings-app">
<nav class="settings-rail" role="tablist" aria-label="Settings" aria-orientation="vertical">
{settings_rail()}
</nav>
<div class="settings-panes">
<div class="pane" id="pane-general" role="tabpanel" aria-labelledby="tab-general" tabindex="-1">
<section class="panel">
<h2>Lemonade AI Engine</h2>
<div class="row"><span class="name">status</span><span class="state {chat_class}" id="chat-state">{chat_label}</span></div>
<div class="row"><span class="name">model</span><span class="state ok" id="chat-model">{display}</span></div>
<p class="lede" id="chat-lede">{chat_lede}</p>
</section>
<section class="panel">
<h2>Status</h2>
<div class="row"><span class="name">setup</span><span class="state ok">complete</span></div>
<div class="row"><span class="name">timezone</span><span class="state ok">{setup.get("timezone","UTC")}</span></div>
{gpu_rows}
</section>
<section class="panel" id="power">
<h2>Power</h2>
<p class="lede">Reboot the appliance, or shut it down. A shutdown leaves it off, with no
lights and no network, until someone presses the power button on the box.</p>
<p class="owui-status" id="power-note" hidden></p>
<div class="owui-actions">
<button type="button" id="power-reboot">{ICON_POWER}<span>Reboot</span></button>
<button type="button" class="danger" id="power-shutdown">{ICON_POWER}<span>Shut down</span></button>
</div>
</section>
<section class="panel" id="software-update">
<h2>Software update</h2>
<div class="row"><span class="name">Current Version</span><span class="state" id="version-running">{running_version}</span></div>
<div class="row" id="version-prior-row" hidden><span class="name" id="version-prior-name">Prior version</span><span class="state" id="version-prior">—</span></div>
<div class="row"><span class="name">Latest available</span><span class="state" id="version-latest">checking…</span></div>
<div class="row"><span class="name">Release channel</span><span class="state"><select id="update-channel"><option value="stable">Stable</option><option value="beta">Beta</option></select></span></div>
<p class="owui-status bad" id="update-failed" hidden></p>
<div class="owui-actions">
<button type="button" id="update-check">Check now</button>
<button type="button" id="update-install" disabled>Download and install</button>
<button type="button" id="update-rollback" hidden>Roll back</button>
</div>
<p class="owui-status" id="update-note" hidden></p>
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
</div>
<div class="pane" id="pane-models" role="tabpanel" aria-labelledby="tab-models" tabindex="-1" hidden>
<section class="panel" id="model-picker">
<h2>Large Language Model</h2>
<p class="lede">AI requires a Large Language Model to power the chat system. Select a model from the list below that best fits your needs, and Lemonseed will download it for you. These are large files and may take a long time to download on slower internet connections.</p>
<div id="model-cards" class="model-cards"></div>
</section>
</div>
<div class="pane" id="pane-chat" role="tabpanel" aria-labelledby="tab-chat" tabindex="-1" hidden>
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
</div>
<div class="pane" id="pane-people" role="tabpanel" aria-labelledby="tab-people" tabindex="-1" hidden>
<section class="panel" id="accounts">
<h2>Users</h2>
<p class="owui-status" id="acct-note" hidden></p>
<div class="table-wrap">
<table class="grid">
<thead><tr><th scope="col">Username</th><th scope="col">Name</th><th scope="col">E-mail</th><th scope="col" class="col-actions"><span class="sr-only">Actions</span></th></tr></thead>
<tbody id="acct-people"></tbody>
</table>
</div>
<div class="owui-actions">
<button type="button" id="acct-add-user">Add user</button>
</div>
</section>
<section class="panel" id="groups-panel">
<h2>Groups</h2>
<p class="owui-hint">System groups are built in. A custom group is a way to hand the same settings to several people at once.</p>
<div class="table-wrap">
<table class="grid">
<thead><tr><th scope="col">Group</th><th scope="col">Members</th><th scope="col" class="col-actions"><span class="sr-only">Actions</span></th></tr></thead>
<tbody id="acct-groups"></tbody>
</table>
</div>
<div class="owui-actions">
<button type="button" id="group-add-button">Add group</button>
</div>
</section>
<section class="panel" id="audit-panel">
<h2>Recent changes</h2>
<div id="acct-audit-log"></div>
<div class="owui-actions">
<button type="button" id="acct-audit">See more history</button>
</div>
</section>
</div>
<div class="pane" id="pane-modules" role="tabpanel" aria-labelledby="tab-modules" tabindex="-1" hidden>
<section class="panel">
<h2>Modules</h2>
<p class="lede">Modules are the apps the appliance runs for you. The chat interface is the
first of them; the others appear here as they become installable.</p>
</section>
</div>
</div>
</main>
<script>{DIALOG_JS}</script>
<script>{PORTAL_FETCH_JS}</script>
<script>{UPDATE_JS}</script>
<script>{SETTINGS_JS}</script>
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
      dialogMessage('Could not change the model',
        body.detail || 'The appliance refused (HTTP ' + res.status + ').');
      return;
    }}
    // Re-render to reflect the new selection; the status row shows progress.
    loadModelPicker();
  }} catch (_err) {{ dialogMessage('Could not change the model', 'Could not reach the control plane.'); }}
}}

loadModelPicker();
</script>
<script>{SRP_CLIENT_JS}</script>
<script>{ACCOUNTS_JS}</script>
<script>{OWUI_CONFIG_JS}</script>
<script>{POWER_JS}</script>
</body></html>"""
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
    name: str,
    timezone: str,
    admin_username: str = "",
    admin_srp_salt: str = "",
    admin_srp_verifier: str = "",
    admin_display_name: str = "",
) -> None:
    name = name.strip()[:64]
    timezone = timezone.strip()[:64] or "UTC"
    if not name:
        raise ValueError("name required")
    if timezone not in TIMEZONES:
        raise ValueError("select a valid timezone")
    admin_username = auth.normalize_username(admin_username)
    admin_display_name = (admin_display_name or "").strip()
    admin_srp_salt = (admin_srp_salt or "").strip()
    admin_srp_verifier = (admin_srp_verifier or "").strip()
    if auth.admin_bootstrap_required() and not admin_username:
        raise ValueError("admin username is required")
    if auth.admin_bootstrap_required() and not (admin_srp_salt and admin_srp_verifier):
        # Reached when the form was submitted without running the enrollment
        # script — a no-JS browser, or a client that stripped the fields. It is
        # not a fallback: there is no server-side path that takes a password.
        raise ValueError(
            "Signing in requires JavaScript. Please enable JavaScript and try again."
        )
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
    if admin_username and admin_srp_salt and admin_srp_verifier:
        # Synchronous first-admin bootstrap: waits bounded for Postgres, then
        # stores the verifier, generates the session secret, and flips the gate.
        created = auth.create_admin(
            admin_username,
            admin_srp_salt,
            admin_srp_verifier,
            display_name=admin_display_name,
        )
        # D-20a: a reset that enrolled a different username deleted the former
        # admin rows in that transaction. Their Open WebUI accounts go now, after
        # commit — and the CP rows are gone whichever way this returns, because a
        # former admin must not be able to sign back in. Refusals are logged
        # rather than raised: the install already succeeded and the accounts are
        # already powerless, so failing the setup page would be misleading.
        problems = usermgmt.cleanup_owui_accounts(
            created.get("owui_cleanup") or [], actor=audit.ACTOR_SYSTEM
        )
        for problem in problems:
            print(
                f"setup: Open WebUI kept the account of former admin"
                f" {problem['username']}: {problem['error']}",
                flush=True,
            )
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

    def _json_body(self, raw: bytes) -> Optional[dict[str, Any]]:
        """Parse a JSON request body, or answer 400 and return None.

        Shared by both SRP phases so neither can disagree about what a malformed
        request costs the caller.
        """
        try:
            parsed = json.loads(raw.decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"detail": "Expected a JSON body."})
            return None
        if not isinstance(parsed, dict):
            self._json(400, {"detail": "Expected a JSON object."})
            return None
        return parsed

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

    def _session_cookie(self, token: str) -> str:
        """The one place the session cookie is assembled.

        Extracted so the SRP path and anything added later cannot drift on
        ``HttpOnly``/``SameSite``, which is the kind of divergence that looks fine
        in review and is a real finding later.
        """
        return f"{auth.SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else "unknown"

    def _handle_login_post(self) -> None:
        """POST /login no longer accepts credentials.

        405 with a readable reason rather than 404: the ``= /login`` gateway
        location stays alive so a no-JS browser gets an explanation instead of a
        dead end, and the ``/login`` document still renders. There is simply no
        server-side path that takes a password any more.
        """
        self._send(
            405,
            b"Signing in requires JavaScript.\n",
            "text/plain; charset=utf-8",
            extra_headers=[("Allow", "GET")],
        )

    def _handle_srp_challenge(self, raw: bytes) -> None:
        """POST /_lemonseed/api/srp/challenge — SRP's first move.

        Answers ``(salt, B)`` for a name, and *never* says whether the name
        exists. RFC 5054 2.5.1.3: an unknown account is simulated with a
        deterministically folded salt, so ``B`` is formed the same way on both
        paths and this endpoint is not an account-enumeration oracle.

        The username is normalised here (D-33) because ``I`` is hashed into ``x``:
        ``John`` and ``john`` must derive the *same* verifier or the person typing
        their own name the way it is written gets a failure that no amount of
        retrying can fix. The browser normalises identically, on every path.
        """
        data = self._json_body(raw)
        if data is None:
            return
        username = auth.normalize_username(data.get("username", ""))
        ip = self._client_ip()
        # Rate-limited too. It is not an authentication failure, so without this
        # it would be an unthrottled way to burn CPU on modular exponentiation.
        if not auth.rate_limit_check(ip):
            self._json(429, {"detail": "Too many attempts. Wait a minute and try again."})
            return
        if not username or len(username) > 64:
            self._json(401, {"detail": "Invalid username or password."})
            return

        user = auth.srp_load_user(username)
        if user is not None and user["srp_verifier"] and user["srp_salt"]:
            salt_hex = user["srp_salt"]
            verifier = int(user["srp_verifier"], 16)
        else:
            # Unknown name, or a row that was never enrolled. Both take this
            # branch so the two are indistinguishable from outside.
            salt_hex = srp.fold_salt(_srp_fold_secret(), username).hex()
            verifier = srp.make_verifier(
                srp.SERVER_GROUP, srp.hash_to_int(srp.SERVER_GROUP.hash_name, bytes.fromhex(salt_hex))
            )

        try:
            b_public, pending = srp.begin_handshake(srp.SERVER_GROUP, username, verifier)
        except srp.SrpError as exc:
            self.log_message("srp challenge failed: %s", exc)
            self._json(401, {"detail": "Invalid username or password."})
            return
        cid = secrets.token_urlsafe(16)
        PENDING_HANDSHAKES.put(cid, pending)
        self._json(200, {"cid": cid, "salt": salt_hex, "B": f"{b_public:x}"})

    def _handle_srp_verify(self, raw: bytes) -> None:
        """POST /_lemonseed/api/srp/verify — second move, and the session.

        The identity comes from the *pending handshake*, not from this request,
        so a client cannot name a different user in phase two than it named in
        phase one. The row is re-read here, which is what makes disabling an
        account take effect even mid-handshake.
        """
        data = self._json_body(raw)
        if data is None:
            return
        ip = self._client_ip()
        if not auth.rate_limit_check(ip):
            self._json(429, {"detail": "Too many attempts. Wait a minute and try again."})
            return

        cid = str(data.get("cid", ""))
        pending = PENDING_HANDSHAKES.take(cid)
        if pending is None:
            # Expired, already used, or invented. Single-use is what makes a
            # replayed verify fail.
            auth.rate_limit_record_failure(ip)
            self._json(401, {"detail": "That sign-in attempt expired. Try again."})
            return

        def reject() -> None:
            auth.rate_limit_record_failure(ip)
            self._json(401, {"detail": "Invalid username or password."})

        try:
            a_public = srp.parse_group_value(data.get("A"), srp.SERVER_GROUP, "A")
            proof = srp.parse_proof(data.get("M1"), srp.SERVER_GROUP)
        except srp.SrpError:
            reject()
            return

        user = auth.srp_load_user(pending.username)
        if user is None or not user["srp_verifier"]:
            # Unknown, unenrolled, or deleted mid-handshake. Still run the full
            # arithmetic against the synthetic verifier the challenge used, then
            # discard the result. Skipping it would return measurably faster and
            # hand back the enumeration oracle the challenge path works to hide.
            try:
                srp.finish_handshake(srp.SERVER_GROUP, pending, a_public, proof)
            except srp.SrpError:
                pass
            reject()
            return

        # Re-read must agree with what the challenge was built on. A row rewritten
        # between the two phases invalidates the handshake rather than silently
        # completing against a verifier the user no longer has.
        try:
            if int(user["srp_verifier"], 16) != pending.verifier:
                reject()
                return
        except ValueError:
            reject()
            return

        try:
            m2 = srp.finish_handshake(srp.SERVER_GROUP, pending, a_public, proof)
        except srp.SrpError:
            reject()
            return

        auth.rate_limit_reset(ip)

        if user["password_reset_required"]:
            # D-11: a credential an admin handed out must not produce a session.
            # M2 is still sent — the client needs it to know the appliance knows
            # the one-time password — but there is no Set-Cookie, so an unrotated
            # account cannot reach the portal, the APIs or Open WebUI. The
            # alternative (issue a session and gate it) needs every surface to
            # honour the flag, and any surface that forgets becomes a way to sit
            # in a one-time account forever.
            self._json(200, {"M2": m2.hex(), "rotate_required": True})
            return

        try:
            token = auth.create_session(
                {
                    "username": user["username"],
                    "display_name": user["display_name"],
                    "email": user["email"],
                    "groups": user["groups"],
                }
            )
        except sessions.SessionStoreUnavailable:
            # Fail closed: a cookie we cannot validate later is access we cannot
            # expire or revoke. Sign-in is where to say so out loud.
            self._json(
                503,
                {"detail": "Sign-in is unavailable: the session store is not reachable."},
            )
            return
        except ValueError:
            reject()
            return

        # A fetch() cannot read a 302's Location, so this always answers JSON and
        # the client navigates _top itself. Same cookie either way.
        usermgmt.record_login(user["username"])
        self._json(
            200,
            {
                "M2": m2.hex(),
                "target": auth.safe_redirect_target(str(data.get("rd", "/")))
                if data.get("rd")
                else "/",
            },
            extra_headers=[("Set-Cookie", self._session_cookie(token))],
        )

    def _handle_srp_rotate(self, raw: bytes) -> None:
        """POST /_lemonseed/api/srp/rotate — change your own password (D-5).

        Two credentials arrive in one request and only one of them is a secret:
        ``A``/``M1`` prove the caller knows the **current** password, and
        ``salt``/``verifier`` are the browser's derivation of the **new** one. The
        appliance sees no password in either half, which is the whole point —
        "set someone's password" is not an operation that exists here, only
        "replace one verifier with another, after proving the first".

        The old proof is validated before anything is written, and the write is
        one guarded statement (``WHERE … AND srp_verifier = %s``), so every
        failure — wrong current password, a second tab that rotated first, a dead
        connection — leaves the old pair intact. There is no state in which an
        account has no password (D-6).

        A new session cookie comes back with the response: rotation bumps
        ``token_generation``, which kills every *other* session for the account,
        including the one issuing this request. Making the user sign in again
        after changing their own password would be a punishing interface for no
        security gain, since this request already proved the password.
        """
        data = self._json_body(raw)
        if data is None:
            return
        ip = self._client_ip()
        if not auth.rate_limit_check(ip):
            self._json(429, {"detail": "Too many attempts. Wait a minute and try again."})
            return

        pending = PENDING_HANDSHAKES.take(str(data.get("cid", "")))
        if pending is None:
            auth.rate_limit_record_failure(ip)
            self._json(401, {"detail": "That sign-in attempt expired. Try again."})
            return

        def reject() -> None:
            auth.rate_limit_record_failure(ip)
            self._json(401, {"detail": "Invalid username or password."})

        try:
            a_public = srp.parse_group_value(data.get("A"), srp.SERVER_GROUP, "A")
            proof = srp.parse_proof(data.get("M1"), srp.SERVER_GROUP)
        except srp.SrpError:
            reject()
            return

        user = auth.srp_load_user(pending.username)
        if user is None or not user["srp_verifier"]:
            # Same timing discipline as verify: run the arithmetic against the
            # synthetic verifier, then discard it, so a missing account is not
            # measurable.
            try:
                srp.finish_handshake(srp.SERVER_GROUP, pending, a_public, proof)
            except srp.SrpError:
                pass
            reject()
            return
        try:
            if int(user["srp_verifier"], 16) != pending.verifier:
                reject()
                return
        except ValueError:
            reject()
            return

        try:
            m2 = srp.finish_handshake(srp.SERVER_GROUP, pending, a_public, proof)
        except srp.SrpError:
            reject()
            return

        # Past this point the current password is proven, so a bad *new* pair is
        # a client bug and deserves a real message rather than "wrong password".
        auth.rate_limit_reset(ip)
        try:
            usermgmt.rotate_password(
                pending.username,
                f"{pending.verifier:x}",
                str(data.get("salt", "")),
                str(data.get("verifier", "")),
                actor=pending.username,
            )
        except ValueError as exc:
            self._json(400, {"detail": str(exc)[:200]})
            return

        # A first sign-in with a one-time code lands here rather than in
        # srp/verify, so this is the one place a rotation login can be stamped;
        # skipping it left every OTP-created account showing "Never".
        usermgmt.record_login(pending.username)

        fresh = usermgmt.get(pending.username) or {}
        try:
            token = auth.create_session(
                {
                    "username": pending.username,
                    "display_name": fresh.get("display_name", user["display_name"]),
                    "groups": fresh.get("groups", user["groups"]),
                }
            )
        except (sessions.SessionStoreUnavailable, ValueError):
            # The password *did* change. Saying "sign-in unavailable" here is
            # accurate and unavoidable; telling them nothing happened would be a
            # lie they would act on by retrying with the old password.
            self._json(
                200,
                {
                    "M2": m2.hex(),
                    "password_changed": True,
                    "detail": "Your password changed. Sign in again to continue.",
                },
            )
            return
        self._json(
            200,
            {
                "M2": m2.hex(),
                "password_changed": True,
                "target": auth.safe_redirect_target(str(data.get("rd", "/")))
                if data.get("rd")
                else "/",
            },
            extra_headers=[("Set-Cookie", self._session_cookie(token))],
        )

    def _actor(self) -> str:
        """The signed-in username nginx forwarded, or ``""`` when there is none.

        Audit rows need a real actor. Reading it from the forwarded header rather
        than from the request body is what makes "who did this" unforgeable by
        the caller: nginx sets the header from the session, and the CP's own
        ``session-verify`` response is the only thing that can populate it.
        """
        return auth.normalize_username(self.headers.get("X-Forwarded-User", ""))

    def _manage_users(self, raw: bytes, action: str) -> None:
        """One shape for every account-management POST (admin-gated).

        Lives in one place on purpose: the actor, the JSON contract, and the
        mapping from a primitive's ``ValueError`` to a ``400 {"detail": …}`` are
        the same for all of them, and an audit trail or an error shape that
        differs per route is how a portal ends up lying on one screen and telling
        the truth on another.
        """
        data = self._json_body(raw)
        if data is None:
            return
        actor = self._actor()
        username = auth.normalize_username(data.get("username", ""))
        try:
            if action == "create":
                result = usermgmt.create(
                    username,
                    str(data.get("display_name", "")),
                    str(data.get("email", "")),
                    str(data.get("salt", "")),
                    str(data.get("verifier", "")),
                    is_admin=bool(data.get("is_admin")),
                    actor=actor,
                )
            elif action == "update":
                # Exactly one field at a time: "which of these did you change"
                # has to have one answer in the audit log.
                if "display_name" in data and "email" in data:
                    raise ValueError("change one field at a time")
                if "display_name" in data:
                    result = usermgmt.set_display_name(
                        username, str(data.get("display_name", "")), actor=actor
                    )
                elif "email" in data:
                    result = usermgmt.set_email(
                        username, str(data.get("email", "")), actor=actor
                    )
                else:
                    raise ValueError("nothing to change")
            elif action == "groups":
                groups = data.get("groups")
                if not isinstance(groups, list):
                    raise ValueError("groups must be a list of names")
                result = usermgmt.set_groups(username, [str(g) for g in groups], actor=actor)
            elif action == "disabled":
                result = usermgmt.set_disabled(
                    username, bool(data.get("disabled")), actor=actor
                )
            elif action == "reset-password":
                result = usermgmt.admin_reset_password(
                    username,
                    str(data.get("salt", "")),
                    str(data.get("verifier", "")),
                    actor=actor,
                )
            elif action == "delete":
                result = usermgmt.delete(username, actor=actor)
            else:  # pragma: no cover — unreachable from the route table
                raise ValueError(f"unknown account action {action!r}")
        except ValueError as exc:
            # Every guard and validation error lands here, written for a person.
            self._json(400, {"detail": str(exc)[:300]})
            return
        except Exception as exc:  # noqa: BLE001 — HTTP boundary
            self.log_message("user management %s failed: %s", action, exc)
            self._json(500, {"detail": "That change could not be saved. Try again."})
            return
        self._json(200, result)

    def _manage_groups(self, raw: bytes, action: str) -> None:
        """Group management POSTs (admin-gated). Same shape as ``_manage_users``."""
        data = self._json_body(raw)
        if data is None:
            return
        actor = self._actor()
        group = str(data.get("group", "")).strip().lower()
        username = auth.normalize_username(data.get("username", ""))
        try:
            if action == "create":
                result = usermgmt.create_group(group, actor=actor)
            elif action == "delete":
                result = usermgmt.delete_group(group, actor=actor)
            elif action == "member":
                add = bool(data.get("add"))
                result = (
                    usermgmt.add_member(group, username, actor=actor)
                    if add
                    else usermgmt.remove_member(group, username, actor=actor)
                )
            elif action == "attribute":
                if "value" not in data:
                    raise ValueError("a value is required")
                result = usermgmt.set_group_attribute(
                    group, str(data.get("key", "")), data.get("value"), actor=actor
                )
            elif action == "attribute-remove":
                result = usermgmt.delete_group_attribute(
                    group, str(data.get("key", "")), actor=actor
                )
            else:  # pragma: no cover — unreachable from the route table
                raise ValueError(f"unknown group action {action!r}")
        except ValueError as exc:
            self._json(400, {"detail": str(exc)[:300]})
            return
        except Exception as exc:  # noqa: BLE001 — HTTP boundary
            self.log_message("group management %s failed: %s", action, exc)
            self._json(500, {"detail": "That change could not be saved. Try again."})
            return
        self._json(200, result)

    def _handle_users_get(self) -> None:
        """GET /_lemonseed/api/users — accounts and groups for the admin panel.

        Sign-in data is not in this payload. ``srp_salt`` and ``srp_verifier``
        stay in the columns that only the handshake reads; a list endpoint is
        exactly where a verifier must never appear, because it is the one
        response a browser caches, logs, and screenshots.
        """
        users = usermgmt.list_users()
        if not users and not auth.pg_ready():
            self._json(
                503,
                {"detail": "Accounts are unavailable: the account database is not ready."},
            )
            return
        self._json(
            200,
            {
                "users": users,
                "groups": usermgmt.list_groups(),
                # The portal needs to know which row it may never delete, and it
                # must learn that from the appliance rather than by assuming the
                # username ``admin`` (D-22).
                "first_admin": usermgmt.first_admin(),
                "attributes": sorted(usermgmt.ATTRIBUTE_KEYS),
            },
        )

    def _handle_profile_get(self) -> None:
        """GET /_lemonseed/api/profile — the caller's own account."""
        me = usermgmt.get(self._actor())
        if me is None:
            self._json(404, {"detail": "You are not signed in."})
            return
        self._json(200, {"user": me})

    def _handle_profile_post(self, raw: bytes) -> None:
        """POST /_lemonseed/api/profile — change your own name or address.

        Deliberately a separate route from the admin surface: it acts only on the
        caller's own username, taken from the session header, so a member can edit
        their own profile without any ability to name someone else.
        """
        data = self._json_body(raw)
        if data is None:
            return
        me = self._actor()
        if not me:
            self._json(401, {"detail": "You are not signed in."})
            return
        try:
            if "display_name" in data:
                result = usermgmt.set_display_name(
                    me, str(data.get("display_name", "")), actor=me
                )
            elif "email" in data:
                result = usermgmt.set_email(me, str(data.get("email", "")), actor=me)
            else:
                raise ValueError("nothing to change")
        except ValueError as exc:
            self._json(400, {"detail": str(exc)[:300]})
            return
        except Exception as exc:  # noqa: BLE001 — HTTP boundary
            self.log_message("profile update failed: %s", exc)
            self._json(500, {"detail": "That change could not be saved. Try again."})
            return
        self._json(200, result)

    def _handle_wordlist_get(self) -> None:
        """GET /_lemonseed/api/wordlist — the sign-in-code vocabulary, as text.

        Served verbatim so the client can split on newlines; the page asks for it
        once, only when it is about to generate a code.
        """
        try:
            body = WORDLIST_FILE.read_bytes()
        except OSError as exc:
            self.log_message("word list unreadable: %s", exc)
            self._json(500, {"detail": "The sign-in code word list is unavailable."})
            return
        self._send(200, body, "text/plain; charset=utf-8")

    def _handle_audit_get(self, query: dict[str, list[str]]) -> None:
        """GET /_lemonseed/api/audit[?target=name] — who did what, newest first."""
        self._json(
            200,
            {
                "events": audit.recent(
                    limit=min(500, max(1, int((query.get("limit") or ["200"])[0]))),
                    target=auth.normalize_username((query.get("target") or [""])[0]),
                ),
                "retention_days": audit.AUDIT_RETENTION_DAYS,
            },
        )

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

    def _handle_power_post(self, raw: bytes) -> None:
        """Reboot or shut down the appliance (admin-only).

        The refusal is the interesting part. A reboot in the middle of a model
        download leaves a truncated weights file that the next boot will try to
        load, and one in the middle of an Open WebUI window leaves the managed
        settings half-applied. So "reboot now" during that work is answered with a
        sentence naming the work at 409, rather than with a reboot that happens to
        land between two steps.
        """
        data = self._json_body(raw)
        if data is None:
            return
        action = data.get("action")
        if action not in POWER_ACTIONS:
            self._json(400, {"detail": "Expected \"reboot\" or \"shutdown\"."})
            return
        blocker = chat_ready.in_flight_work() or modules.in_flight_work() or updates.in_flight()
        if blocker:
            self._json(
                409,
                {"detail": f"Not yet: {blocker}. Try again when it finishes.",
                 "reason": "in flight"},
            )
            return
        if action == "reboot":
            # Written before the reboot is requested, so the shell's own poll
            # reports "rebooting" instead of a progress bar that quietly stops.
            # The CP deletes the marker at startup.
            chat_ready.mark_reboot_pending("an admin requested a reboot from the portal")
        audit.record(self._actor() or audit.ACTOR_CP, "appliance", f"power.{action}")
        # Answered first, then scheduled: this response is the only thing the
        # portal has to tell whether the appliance refused or is simply gone.
        self._json(200, {"state": "ok", "action": action})
        schedule_power(action)

    def _handle_update_get(self) -> None:
        """GET /_lemonseed/api/update — the SYSTEM panel's update state."""
        self._json(200, updates.snapshot(appliance_version()))

    def _handle_update_check_post(self) -> None:
        """POST /_lemonseed/api/update/check — *Check now*, as a job.

        The check is a network fetch against the release channel, so it runs in
        the background and the panel polls; a fetch that cannot complete (the
        WAN down, the channel unreachable) is a job failure the panel shows,
        not a hung request.
        """
        reason = updates.start_check()
        if reason:
            self._json(409, {"detail": reason, "reason": "in flight"})
            return
        self._json(202, {"state": "running", "kind": "check"})

    def _handle_update_post(self, raw: bytes) -> None:
        """POST /_lemonseed/api/update — download, write, and reboot (D4).

        Answered 202 with the job started; the panel polls the snapshot, whose
        status runs downloading / writing / rebooting / failed. ``retry`` is
        the failed-update banner's *Try again*: cancel-then-apply, which is the
        only way past ``refuse_dirty_state`` after a failed attempt.
        """
        data = self._json_body(raw)
        if data is None:
            return
        retry = bool(data.get("retry"))
        reason = updates.start_install(retry=retry)
        if reason:
            self._json(409, {"detail": reason, "reason": "in flight"})
            return
        action = "update.retry" if retry else "update.install"
        audit.record(self._actor() or audit.ACTOR_CP, "appliance", action)
        self._json(202, {"state": "running", "kind": "install"})

    def _handle_update_rollback_post(self) -> None:
        """POST /_lemonseed/api/update/rollback — boot the prior slot (M8 P6).

        The updater's verb does the eligibility checks root-side (the registry
        is the authority) and the job surfaces any refusal verbatim; auditing
        here keeps the panel's actions on the same footing as install.
        """
        running = appliance_version()
        reason = updates.start_rollback(running)
        if reason:
            self._json(409, {"detail": reason, "reason": "ineligible or in flight"})
            return
        audit.record(self._actor() or audit.ACTOR_CP, "appliance", "update.rollback")
        self._json(202, {"state": "running", "kind": "rollback"})

    def _handle_update_channel_post(self, raw: bytes) -> None:
        """POST /_lemonseed/api/update/channel — switch the release channel.

        Admin-gated like every other update action. The settings store
        validates, stores in cp_meta, and materializes
        /var/lib/lemonseed/channel for the DB-free root jobs; the next check
        resolves through the new channel.
        """
        try:
            data = json.loads(raw.decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "bad request"})
            return
        channel = data.get("channel") if isinstance(data, dict) else None
        if channel not in ("stable", "beta"):
            self._json(400, {"error": "channel must be 'stable' or 'beta'"})
            return
        if not updates.settings.set_setting("release_channel", channel):
            self._json(500, {"error": "could not save the release channel"})
            return
        audit.record(
            self._actor() or audit.ACTOR_CP,
            "appliance",
            "update.channel",
            {"channel": channel},
        )
        self._json(200, {"channel": channel})

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
        query = parse_qs(urlparse(self.path).query)
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
        if path == "/_lemonseed/api/users":
            # Admin-only here *and* in nginx, because the gateway is the piece
            # that can be misconfigured by a future edit and the CP is not.
            if not self._require_admin():
                return
            self._handle_users_get()
            return
        if path == "/_lemonseed/api/audit":
            if not self._require_admin():
                return
            self._handle_audit_get(query)
            return
        if path == "/_lemonseed/api/wordlist":
            # Admin-only in nginx too: this exists to hand out sign-in codes,
            # which only an admin generates.
            if not self._require_admin():
                return
            self._handle_wordlist_get()
            return
        if path == "/_lemonseed/api/update":
            if not self._require_admin():
                return
            self._handle_update_get()
            return
        if path == "/_lemonseed/api/profile":
            self._handle_profile_get()
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
        if path in ("/profile", "/profile/"):
            # Every signed-in user's own page. Unlike /settings/ there is no
            # admin gate here, and that is the whole design: a non-admin gets
            # chat and this page, so rotation cannot be an admin-only affordance.
            if redirect_off_factory_mdns(self, "/profile/"):
                return
            if not setup_complete() or auth.reset_marker_present():
                self.send_response(302)
                self.send_header("Location", "/setup")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if not self._actor():
                self.send_response(302)
                self.send_header("Location", "/login?next=/profile/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            # Nothing on this page reads the boot snapshot, so none is taken.
            self._send(200, page_profile(boot_snapshot()), "text/html; charset=utf-8")
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
            self._handle_login_post()
            return
        if path == "/_lemonseed/api/srp/challenge":
            self._handle_srp_challenge(raw)
            return
        if path == "/_lemonseed/api/srp/verify":
            self._handle_srp_verify(raw)
            return
        if path == "/_lemonseed/api/srp/rotate":
            self._handle_srp_rotate(raw)
            return
        if path == "/logout":
            self._handle_logout()
            return
        if path == "/_lemonseed/api/profile":
            # Self-service: no admin gate, and the route ignores any username in
            # the body. It edits whoever the session says the caller is.
            self._handle_profile_post(raw)
            return
        if path.startswith("/_lemonseed/api/users/"):
            if not self._require_admin():
                return
            action = path[len("/_lemonseed/api/users/") :]
            if action in USER_ACTIONS:
                self._manage_users(raw, action)
                return
            self._json(404, {"error": "not found"})
            return
        if path.startswith("/_lemonseed/api/groups/"):
            if not self._require_admin():
                return
            action = path[len("/_lemonseed/api/groups/") :]
            if action in GROUP_ACTIONS:
                self._manage_groups(raw, action)
                return
            self._json(404, {"error": "not found"})
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
        if path == "/_lemonseed/api/power":
            # Only an admin may stop the appliance, in nginx and here.
            if not self._require_admin():
                return
            self._handle_power_post(raw)
            return
        if path == "/_lemonseed/api/update/check":
            if not self._require_admin():
                return
            self._handle_update_check_post()
            return
        if path == "/_lemonseed/api/update":
            if not self._require_admin():
                return
            self._handle_update_post(raw)
            return
        if path == "/_lemonseed/api/update/rollback":
            if not self._require_admin():
                return
            self._handle_update_rollback_post()
            return
        if path == "/_lemonseed/api/update/channel":
            if not self._require_admin():
                return
            self._handle_update_channel_post(raw)
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
                str(data.get("admin_srp_salt", "")),
                str(data.get("admin_srp_verifier", "")),
                str(data.get("admin_display_name", "")),
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

    def log_message(self, format: str, *args: Any) -> None:
        # Diagnostics only (disconnects, handler failures). Access lines go to
        # the file via log_request; these belong on stderr so journald carries
        # them with the service's error priority, not as request noise.
        print(f"{self.address_string()} - {format % args}", file=sys.stderr, flush=True)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        """One access line, to the access journald channel — never the service log.

        The remote address is the client nginx saw (X-Forwarded-For; the CP
        hears every request as 127.0.0.1), and the actor is the signed-in
        username nginx forwarded — the same trust the authz decisions already
        place in those headers. Sending is best-effort: a log line must never
        break a request.
        """
        # The gate's internal target fires once per gated request; the gated
        # request itself is the line worth keeping (at verify time there is no
        # user yet and the client is always loopback). Logging both doubles the
        # channel with no information — and the verify is the one path that
        # fires constantly (BM-4 review).
        if getattr(self, "path", "").split("?", 1)[0] == "/_lemonseed/session-verify":
            return
        forwarded = str(self.headers.get("X-Forwarded-For") or "")
        ip = forwarded.split(",")[0].strip() or (self.client_address[0] if self.client_address else "-")
        user = self._actor() or "-"
        # user/client ride in the message itself so a plain `journalctl -t`
        # shows them; the structured fields stay for programmatic readers.
        ACCESS_LOGGER.info(
            f'"{self.requestline}" {code} {size} user={user} client={ip}',
            extra={"CLIENT": ip, "USER": user,
                   "REQUEST": self.requestline, "CODE": str(code)},
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
