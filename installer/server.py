#!/usr/bin/env python3
"""Lemonseed USB installer web service.

Listens on :80, serves a mobile-friendly confirm/install UI, and drives
install phases (download → repart → write → ESP → await USB removal).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from disks import list_candidate_disks, pick_default
from install import InstallError, image_base_url, run_install

HOST = os.environ.get("LEMONSEED_INSTALLER_HOST", "0.0.0.0")
PORT = int(os.environ.get("LEMONSEED_INSTALLER_PORT", "80"))
IMAGE_BASE = image_base_url()
STATIC = Path(__file__).resolve().parent / "static"

_lock = threading.Lock()
_state: dict[str, Any] = {
    "role": "installer",
    "phase": "idle",
    "progress": 0,
    "message": "Waiting to start",
    "error": None,
    "disk": None,
    "can_reboot": False,
    "version": None,
}

def _set(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)


def get_state() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def install_worker(disk_path: str, preserve_user_data: bool) -> None:
    def progress(**kwargs: Any) -> None:
        _set(**kwargs)

    try:
        run_install(disk_path, progress=progress, preserve_user_data=preserve_user_data)
    except InstallError as exc:
        _set(
            phase="error",
            error=str(exc),
            message="Install failed",
            can_reboot=False,
        )
        print(f"install error: {exc}", flush=True)
    except Exception as exc:  # noqa: BLE001 — surface to UI
        _set(
            phase="error",
            error=str(exc),
            message="Install failed",
            can_reboot=False,
        )
        traceback.print_exc()


def start_install(disk_path: str, preserve_user_data: bool) -> tuple[int, dict[str, Any]]:
    with _lock:
        if _state["phase"] not in ("idle", "error", "await_usb"):
            return 409, {"error": "install already in progress", "state": dict(_state)}
        _state.update(
            {
                "phase": "starting",
                "progress": 1,
                "message": "Starting install",
                "error": None,
                "disk": {"path": disk_path},
                "preserve_user_data": preserve_user_data,
                "can_reboot": False,
                "version": None,
            }
        )
    threading.Thread(
        target=install_worker, args=(disk_path, preserve_user_data), daemon=True
    ).start()
    return 202, {"ok": True, "state": get_state()}


def hard_reboot() -> None:
    """Immediate reset — installer rootfs is on the removable media.

    After the user ejects the CD/USB, graceful shutdown tries to use a gone
    SquashFS and floods the console with read errors. Sync, then sysrq-b.
    """
    try:
        Path("/proc/sys/kernel/sysrq").write_text("1\n", encoding="utf-8")
    except OSError:
        pass
    subprocess.run(["sync"], check=False)
    time.sleep(0.5)
    subprocess.run(["sync"], check=False)
    try:
        Path("/proc/sysrq-trigger").write_text("b", encoding="utf-8")
    except OSError:
        # Last resort if sysrq is blocked.
        subprocess.run(["systemctl", "--force", "--force", "reboot"], check=False)


def request_reboot() -> tuple[int, dict[str, Any]]:
    st = get_state()
    if not st.get("can_reboot"):
        return 409, {"error": "not ready to reboot", "state": st}
    # Let the HTTP response flush, then hard-reset.
    threading.Thread(target=lambda: (time.sleep(1), hard_reboot()), daemon=True).start()
    return 200, {"ok": True, "message": "Rebooting"}


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Lemonseed Installer</title>
  <link rel="stylesheet" href="/static/style.css" />
</head>
<body>
  <main class="shell">
    <header class="brand">
      <p class="wordmark">Lemonseed</p>
      <h1>Install</h1>
      <p class="lede">Choose a disk, confirm, and we’ll set up this machine.</p>
    </header>

    <section id="warn" class="warn hidden" role="alert"></section>

    <section id="disk-panel" class="panel">
      <h2>Target disk</h2>
      <div id="disks" class="disks"></div>
      <label id="preserve-row" class="preserve hidden">
        <input id="preserve-user-data" type="checkbox" checked />
        <span>
          <strong>Preserve existing user information on this appliance</strong>
          <small>Keep chats, settings, workers, and downloaded models. Lemonseed system software will be replaced.</small>
        </span>
      </label>
      <button id="start" class="cta" disabled>Install Lemonseed</button>
    </section>

    <section id="progress-panel" class="panel hidden">
      <h2 id="phase-title">Installing</h2>
      <p id="phase-msg" class="lede"></p>
      <div class="bar" aria-hidden="true"><div id="bar-fill"></div></div>
      <p id="phase-pct" class="pct">0%</p>
      <div id="reboot-box" class="reboot hidden">
        <p><strong>Remove the installer CD/USB</strong>, then click Reboot.</p>
        <button id="reboot" class="cta">Reboot</button>
      </div>
      <div id="wait-box" class="wait-boot hidden">
        <div class="spinner" role="status" aria-label="Waiting for appliance"></div>
      </div>
      <p id="err" class="err hidden"></p>
    </section>
  </main>
  <script src="/static/app.js"></script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "LemonseedInstaller/0.1"

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, (json.dumps(obj) + "\n").encode(), "application/json")

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode())

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
            return
        if path == "/api/disks":
            disks = list_candidate_disks()
            self._json(200, {"disks": disks, "default": pick_default(disks), "image_base": IMAGE_BASE})
            return
        if path == "/api/status":
            self._json(200, get_state())
            return
        if path == "/api/network":
            self._json(200, {"hostname": "lemonseed.local", "addresses": _addresses()})
            return
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            if ".." in rel or rel.startswith("/"):
                self._send(404, b"not found\n", "text/plain")
                return
            fp = STATIC / rel
            if not fp.is_file():
                self._send(404, b"not found\n", "text/plain")
                return
            ctype = "text/css" if fp.suffix == ".css" else "application/javascript" if fp.suffix == ".js" else "application/octet-stream"
            self._send(200, fp.read_bytes(), ctype)
            return
        self._send(404, b"not found\n", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            body = self._read_json()
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid json"})
            return
        if path == "/api/install":
            disk = body.get("disk") or ""
            if not disk:
                self._json(400, {"error": "disk required"})
                return
            preserve_user_data = bool(body.get("preserve_user_data"))
            code, payload = start_install(disk, preserve_user_data)
            self._json(code, payload)
            return
        if path == "/api/reboot":
            code, payload = request_reboot()
            self._json(code, payload)
            return
        self._json(404, {"error": "not found"})

    def log_message(self, fmt: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


def _addresses() -> list[str]:
    addrs: list[str] = []
    try:
        out = subprocess.check_output(
            ["ip", "-j", "-4", "addr", "show", "up"], text=True
        )
        for iface in json.loads(out):
            if iface.get("link_type") == "loopback":
                continue
            for a in iface.get("addr_info") or []:
                if a.get("family") == "inet" and a.get("local"):
                    addrs.append(a["local"])
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
        pass
    return addrs


def main() -> None:
    STATIC.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"lemonseed-installer listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
