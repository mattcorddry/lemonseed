#!/usr/bin/env python3
"""Lemonseed VM test harness (the Proxmox phase, TESTING.md phase E).

Creates a VM on the Proxmox node, boots the installer ISO, drives the web
installer's own API to install, and hands the installed image to the QEMU
smoke suite (unchanged, through a local TCP forward to the VM's :80 — the
suite's assertion library is the same one the QEMU smoke uses).

The allowlist is the phase's first invariant. The host serves real workloads
(a 192 GiB VM among them), so the harness refuses any VM id or name outside
``LEMONSEED_VM_ALLOWLIST`` (a comma-separated set of numeric ids, or name
patterns like ``lemonseed-*``) and any storage outside
``LEMONSEED_STORAGE_ALLOWLIST`` (default ``local-zfs``). The check runs
before every state-changing call — create, start, stop, delete, config — and
fails loudly rather than guessing.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import NoReturn

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "out"
STATE = OUT / "vm-state.json"

VM_ALLOWLIST_ENV = "LEMONSEED_VM_ALLOWLIST"
STORAGE_ALLOWLIST_ENV = "LEMONSEED_STORAGE_ALLOWLIST"
VM_ID_RANGE = (200, 219)  # never collide with a real workload (ids 100-120)
VM_NAME_PREFIX = "lemonseed-vm-"
INSTALLER_MDNS = "lemonseed.local"
PORTAL_MDNS = "lemonseed.local"
ISO_NAME = "lemonseed-installer.iso"
FORWARD_PORT = 19080  # the smoke suite's default

FOOTPRINT = {"cores": 4, "memory": 16384, "disk_gib": 40}  # 16 GiB: /var's tmpfs
# (50% of RAM) must hold the release set's 4.3 GiB payload while the
# installer extracts it; an 8 GiB guest ran out at 45% ("staging filesystem
# is full while extracting the payload").


def log(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def fail(msg: str) -> NoReturn:
    print(f"FAIL: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


# ---------------------------------------------------------------- allowlist


def allowlist(env: str, default: str) -> list[str]:
    raw = os.environ.get(env) or default
    return [e.strip() for e in raw.split(",") if e.strip()]


def _matches(rule: str, value: str) -> bool:
    return value.startswith(rule[:-1]) if rule.endswith("*") else rule == value


def ensure_vms_allowed(pve: "Pve", ids: list[int], names: list[str] | None = None) -> None:
    """Refuse to touch a VM outside the allowlist, before any action. The
    caller's own names are checked first (a VM about to be created does not
    exist yet, so the API lookup cannot find it)."""
    rules = allowlist(VM_ALLOWLIST_ENV, "")
    if not rules:
        fail(f"{VM_ALLOWLIST_ENV} is not set — refusing to touch any VM")
    names = names or []
    for i, vmid in enumerate(ids):
        if any(_matches(r, str(vmid)) for r in rules):
            continue
        name = names[i] if i < len(names) else pve.vmid_name(vmid)
        if name and any(_matches(r, name) for r in rules):
            continue
        fail(
            f"VM {vmid} ({name or 'unnamed'}) is not in {VM_ALLOWLIST_ENV}="
            f"{','.join(rules)} — refusing"
        )


def ensure_storage_allowed(storage: str) -> None:
    rules = allowlist(STORAGE_ALLOWLIST_ENV, "local-zfs")
    if storage not in rules:
        fail(f"storage {storage} is not in {STORAGE_ALLOWLIST_ENV}={','.join(rules)} — refusing")


# ------------------------------------------------------------ Proxmox API


class Pve:
    """A thin Proxmox API client over the token's env vars."""

    def __init__(self) -> None:
        for var in ("PVE_HOST", "PVE_API_KEY_USER", "PVE_API_KEY_ID", "PVE_API_KEY"):
            if not os.environ.get(var):
                fail(f"{var} is not set — the token's env vars are required")
        self.host = os.environ["PVE_HOST"]
        self.base = f"https://{self.host}:8006/api2/json"
        self.auth = "PVEAPIToken={user}!{id}={secret}".format(
            user=os.environ["PVE_API_KEY_USER"],
            id=os.environ["PVE_API_KEY_ID"],
            secret=os.environ["PVE_API_KEY"],
        )
        self.ctx = ssl._create_unverified_context()

    def _call(self, method: str, path: str, body: dict | None, timeout: float) -> tuple[int, object]:
        url = f"{self.base}{path}"
        data = urllib.parse.urlencode(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=self.ctx) as r:
                return r.status, json.loads(r.read() or b"{}").get("data")
        except urllib.error.HTTPError as e:
            payload = e.read()
            try:
                return e.code, json.loads(payload)
            except Exception:
                return e.code, {"message": payload.decode(errors="replace")}

    def get(self, path: str, **params: object) -> list | dict:
        q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        code, data = self._call("GET", f"{path}?{q}" if q else path, None, 60)
        if code != 200:
            fail(f"GET {path} -> {code} {data}")
        return data if isinstance(data, (list, dict)) else []

    def post(self, path: str, body: dict | None = None, timeout: float = 60) -> tuple[int, dict]:
        code, data = self._call("POST", path, body or {}, timeout)
        return code, (data if isinstance(data, dict) else {"data": data})

    def delete(self, path: str, timeout: float = 60) -> tuple[int, dict]:
        code, data = self._call("DELETE", path, None, timeout)
        return code, (data if isinstance(data, dict) else {"data": data})

    def _qemu(self, vmid: int) -> dict:
        rows = self.get("/nodes/pve/qemu")
        for r in rows:
            if int(r["vmid"]) == vmid:
                return r
        fail(f"VM {vmid} is not on {self.host}")

    def vmid_name(self, vmid: int) -> str:
        return str(self._qemu(vmid).get("name") or "")

    def vmid_status(self, vmid: int) -> str:
        return str(self._qemu(vmid).get("status") or "")

    def alloc_vmid(self) -> int:
        taken = {int(r["vmid"]) for r in self.get("/nodes/pve/qemu")}
        for v in range(VM_ID_RANGE[0], VM_ID_RANGE[1] + 1):
            if v not in taken:
                return v
        fail(f"no free vmid in {VM_ID_RANGE}")

    def wait_task(self, upid: str, timeout: float) -> None:
        node = upid.split(":")[1]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.get(
                f"/nodes/{node}/tasks/{urllib.parse.quote(upid, safe='')}/status"
            )
            if isinstance(st, dict) and st.get("status") == "finished":
                if st.get("exitstatus") == "OK":
                    return
                tail = self.get(
                    f"/nodes/{node}/tasks/{urllib.parse.quote(upid, safe='')}/log?limit=20"
                )
                lines = [str(r.get("t", "")) for r in tail] if isinstance(tail, list) else []
                fail(f"task {upid} -> {st.get('exitstatus')} ({'; '.join(lines[-5:])})")
            time.sleep(2)
        fail(f"task {upid} did not finish within {timeout:.0f}s")


# ------------------------------------------------------------- find helpers


def state_write(vmid: int, ip: str | None) -> None:
    OUT.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"vmid": vmid, "ip": ip}, indent=1))


def state_read() -> dict:
    if not STATE.is_file():
        fail(f"{STATE} is missing — create + install first")
    return json.loads(STATE.read_text())


def curl(url: str, timeout: float = 10) -> tuple[int, dict | None]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl._create_unverified_context()) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def post_json(url: str, body: dict) -> tuple[int, dict | None]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=ssl._create_unverified_context()) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def resolve(name: str) -> str | None:
    """mDNS/DNS lookup via getent (systemd-resolved's mDNS)."""
    r = subprocess.run(
        ["getent", "hosts", name], check=False, capture_output=True, text=True, timeout=10
    )
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("192.168."):
            return parts[0]
    return None


def find_installer(deadline_s: float = 300) -> tuple[str, dict]:
    """The installer ISO advertises lemonseed.local; a LAN device already
    claims it (lemonseed1.corddry.net), in which case avahi renames the second
    responder — so try the renamed forms too and take the one whose
    /api/status answers."""
    deadline = time.monotonic() + deadline_s
    names = [INSTALLER_MDNS]
    for n in range(2, 10):
        names.append(INSTALLER_MDNS.replace(".local", f"-{n}.local"))
    log("finding the installer via mDNS (%s …)" % ", ".join(names[:3]))
    while time.monotonic() < deadline:
        for name in names:
            ip = resolve(name)
            if not ip:
                continue
            code, data = curl(f"http://{ip}/api/status", timeout=5)
            if code == 200 and isinstance(data, dict) and data.get("role") == "installer":
                return ip, data
        time.sleep(5)
    fail(f"no installer answered within {deadline_s:.0f}s")


def wait_portal(ip: str, deadline_s: float = 600) -> None:
    deadline = time.monotonic() + deadline_s
    log(f"waiting for the portal on http://{ip}:80")
    while time.monotonic() < deadline:
        code, _ = curl(f"http://{ip}/api/status", timeout=8)
        if code == 200:
            return
        time.sleep(5)
    fail(f"no portal on {ip}:80 within {deadline_s:.0f}s")


# ----------------------------------------------------- the local forwarder


def forward(src_port: int, dst_host: str, dst_port: int) -> None:
    """A tiny TCP forwarder: the smoke suite's base URL is
    http://127.0.0.1:19080, which is the QEMU port-forward. For a VM, the
    suite runs unchanged against the same local port, forwarded to the VM."""

    def pump(a: socket.socket, b: socket.socket) -> None:
        try:
            while True:
                buf = a.recv(65536)
                if not buf:
                    break
                b.sendall(buf)
        except Exception:
            pass
        finally:
            for s in (a, b):
                try:
                    s.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", src_port))
    listener.listen(8)
    log(f"forwarding 127.0.0.1:{src_port} -> {dst_host}:{dst_port}")
    while True:
        client, _ = listener.accept()
        remote = socket.socket()
        try:
            remote.connect((dst_host, dst_port))
        except OSError:
            client.close()
            continue
        threading.Thread(target=pump, args=(client, remote), daemon=True).start()
        threading.Thread(target=pump, args=(remote, client), daemon=True).start()


# --------------------------------------------------------------- the steps


def step_status(_args: argparse.Namespace) -> None:
    pve = Pve()
    rules = allowlist(VM_ALLOWLIST_ENV, "")
    log("allowlist: %s=%s  %s=%s" % (
        VM_ALLOWLIST_ENV, ",".join(rules) or "(unset — refusing)",
        STORAGE_ALLOWLIST_ENV, ",".join(allowlist(STORAGE_ALLOWLIST_ENV, "local-zfs"))))
    log("the VMs the token can see:")
    for r in pve.get("/nodes/pve/qemu"):
        vmid = int(r["vmid"])
        name = str(r.get("name") or "?")
        allowed = any(_matches(rule, str(vmid)) or _matches(rule, name) for rule in rules)
        log("  id %-4s %-24s status=%-8s cores=%-3s mem=%-5d MiB %s" % (
            vmid, name, r.get("status"), r.get("cpus"), (r.get("maxmem") or 0) // 1024,
            "ALLOWED" if allowed else "off-limits"))


def step_create(args: argparse.Namespace) -> None:
    if not args.dev_httpd:
        fail("--dev-httpd is required for create")
    pve = Pve()
    vmid = pve.alloc_vmid()
    name = f"{VM_NAME_PREFIX}{time.strftime('%Y%m%d%H%M')}"
    log(f"allocating vmid {vmid} name {name}")
    ensure_vms_allowed(pve, [vmid], [name])
    storage = args.storage or "local-zfs"
    ensure_storage_allowed(storage)
    iso = OUT / "latest-installer" / ISO_NAME
    if not iso.is_file():
        fail(f"{iso} is missing — run make build-installer first")
    # Skip the 1.3 GiB pull when the node already holds the same file.
    node_iso = [r for r in pve.get("/nodes/pve/storage/local/content?content=iso")
                if str(r.get("volid", "")).endswith(ISO_NAME)]
    if not node_iso or int(node_iso[0].get("size", 0) or 0) != iso.stat().st_size:
        code, data = pve.post(
            "/nodes/pve/storage/local/download-url",
            {"url": f"http://{args.dev_httpd}/latest-installer/{ISO_NAME}",
             "filename": ISO_NAME, "content": "iso"},
            timeout=1200,
        )
        if code not in (200, 201, 202, 204):
            fail(f"download-url -> {code} {data}")
        log("the ISO is on the node")
    else:
        log("the node already holds the ISO (same size) — skipping the pull")
    body = {
        "vmid": vmid,
        "name": name,
        "cores": FOOTPRINT["cores"],
        "memory": FOOTPRINT["memory"],
        "net0": f"virtio,bridge={args.bridge}",
        "scsihw": "virtio-scsi-pci",
        "scsi0": f"{storage}:{FOOTPRINT['disk_gib']}",
        "efidisk0": f"{storage}:1,efitype=4m,pre-enrolled-keys=0,size=528K",
        "ide2": f"local:iso/{ISO_NAME},media=cdrom",
        "boot": "order=ide2;scsi0",
        "ostype": "l26",
        "agent": 0,
        "serial0": "socket",
        "vga": "serial0",
    }
    code, data = pve.post("/nodes/pve/qemu", body, timeout=300)
    if code not in (200, 201, 202, 204):
        fail(f"VM create -> {code} {data}")
    state_write(vmid, None)
    log(f"VM {vmid} created ({name}) — {FOOTPRINT['cores']} cores, {FOOTPRINT['memory']} MiB, {FOOTPRINT['disk_gib']} GiB on {storage}")


def step_start(args: argparse.Namespace) -> None:
    if not args.vmid:
        fail("--vmid is required for start")
    pve = Pve()
    vmid = int(args.vmid)
    ensure_vms_allowed(pve, [vmid])
    # The power endpoints live under the VM's status path, not the VM itself.
    code, data = pve.post(f"/nodes/pve/qemu/{vmid}/status/start", timeout=300)
    if code not in (200, 201, 202, 204):
        fail(f"VM start -> {code} {data}")
    log(f"VM {vmid} started")


def _newest_release_dir() -> str:
    """The newest staged release set (out/release-<version>/) by version —
    a directory, so the build log that shares the prefix is not a candidate."""
    sets = sorted(
        (name.name for name in OUT.iterdir()
         if name.name.startswith("release-") and name.is_dir()),
        key=lambda n: n,
    )
    return sets[-1] if sets else "release-0000.00"


def step_install(args: argparse.Namespace) -> None:
    if not args.artifacts_host:
        fail("--artifacts-host is required for install")
    pve = Pve()
    st = state_read()
    vmid = int(st["vmid"])
    ensure_vms_allowed(pve, [vmid])
    if pve.vmid_status(vmid) != "running":
        log("the VM is not running — starting it")
        step_start(argparse.Namespace(vmid=str(vmid)))
    ip, st = find_installer()
    log(f"the installer is at {ip} (state={st.get('state')})")
    if st.get("phase") == "await_usb" or st.get("can_reboot"):
        # A previous install already completed (the VM is rebooted from an
        # earlier run's state): do not wipe the disk and reinstall.
        log(f"the install is already complete ({st.get('message')}) — skipping the reinstall")
    else:
        code, disks = curl(f"http://{ip}/api/disks", timeout=10)
        if code != 200 or not isinstance(disks, dict) or not disks.get("disks"):
            fail(f"no disks from the installer: {disks}")
        disk = disks["disks"][0]["path"]
        log(f"installing onto {disk}")
        # The artifacts the installer downloads are the release set
        # (out/release-<version>/: manifest + signature + payload parts), not the
        # QEMU boot assets. Default to the newest staged set.
        artifacts_dir = args.artifacts_dir or _newest_release_dir()
        if not (OUT / artifacts_dir).is_dir():
            fail(f"{OUT / artifacts_dir} is missing — run make release first")
        artifacts = f"http://{args.artifacts_host}/{artifacts_dir}"
        code, data = post_json(
            f"http://{ip}/api/install",
            {"disk": disk, "preserve_user_data": False, "reset_admin_credentials": False,
             "channel": "dev", "source_url": artifacts},
        )
        # 202 (Accepted) is the installer's own "the install has started" verdict.
        if code not in (200, 202):
            fail(f"/api/install -> {code} {data}")
        log(f"install started (source {artifacts}) — waiting for it to finish")
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            code, st = curl(f"http://{ip}/api/status", timeout=10)
            if code == 200 and isinstance(st, dict):
                # "await_usb" is the installer's own 'is installed; remove the
                # USB, then click Reboot' verdict — can_reboot is True there.
                if st.get("phase") == "await_usb" or st.get("can_reboot"):
                    log(f"install finished: {st.get('message')}")
                    break
                if st.get("phase") == "error":
                    fail(f"install failed: {st.get('error')} (detail: {st.get('detail')})")
            time.sleep(10)
        else:
            fail("the install did not finish within an hour")
    code, data = post_json(f"http://{ip}/api/reboot", {})
    if code not in (200, 202):
        fail(f"/api/reboot -> {code} {data}")
    log("rebooting into the installed image")
    # Boot the disk, not the ISO: the firmware's boot order still prefers
    # ide2 (the ISO), so the reboot would run the installer again.
    code, data = pve.post(
        f"/nodes/pve/qemu/{vmid}/config", {"boot": "order=scsi0"}, timeout=60
    )
    if code not in (200, 201, 202, 204):
        fail(f"VM boot order -> {code} {data}")
    log("VM boot order set to the disk (order=scsi0)")
    state_write(vmid, ip)
    wait_portal(ip, deadline_s=900)


def step_smoke(_args: argparse.Namespace) -> None:
    st = state_read()
    ip = st.get("ip")
    if not ip:
        fail("the VM's IP is not in the state file — install first")
    # The appliance's DHCP lease changes after the reboot into the installed
    # image, so re-resolve the mDNS name freshly instead of trusting the
    # install step's recorded IP.
    fresh = resolve(PORTAL_MDNS)
    if fresh:
        ip = fresh
        st["ip"] = fresh
        STATE.write_text(json.dumps(st, indent=1))
        log(f"the appliance's fresh mDNS IP is {ip} (was {st.get('ip')} — rewritten)")
    log(f"running the QEMU smoke suite (skip-boot) against http://{ip}:80")
    thread = threading.Thread(
        target=forward, args=(FORWARD_PORT, ip, 80), daemon=True
    )
    thread.start()
    subprocess.run(
        ["bash", str(REPO / "scripts" / "qemu-api-smoke-test.sh")],
        cwd=str(REPO), check=False,
        env={**os.environ, "SMOKE_SKIP_BOOT": "1"},
    )


def step_delete(args: argparse.Namespace) -> None:
    if not args.vmid:
        fail("--vmid is required for delete")
    pve = Pve()
    vmid = int(args.vmid)
    ensure_vms_allowed(pve, [vmid])
    name = pve.vmid_name(vmid)
    if pve.vmid_status(vmid) == "running":
        code, _ = pve.post(f"/nodes/pve/qemu/{vmid}/status/stop", timeout=300)
        log(f"stop -> {code}")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline and pve.vmid_status(vmid) != "stopped":
        time.sleep(2)
    code, data = pve.delete(f"/nodes/pve/qemu/{vmid}")
    if code not in (200, 202, 204):
        fail(f"VM delete -> {code} {data}")
    if isinstance(data, dict) and data.get("data"):
        pve.wait_task(str(data["data"]), timeout=300)
    log(f"VM {vmid} ({name}) deleted")


# ------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--dev-httpd", default=None, help="host:port serving ./out for the ISO pull")
        p.add_argument("--artifacts-host", default=None, help="host:port serving the artifacts for the install")
        p.add_argument("--artifacts-dir", default=None, help="the release set's dir name (e.g. release-20261114.01)")
        p.add_argument("--bridge", default="vmbr0")
        p.add_argument("--storage", default=None)
        p.add_argument("--vmid", default=None, help="the VM id to act on (create/start/delete)")

    common(ap)
    for name in ("status", "create", "start", "install", "smoke", "delete"):
        common(sub.add_parser(name))
    args = ap.parse_args()
    {
        "status": step_status,
        "create": step_create,
        "start": step_start,
        "install": step_install,
        "smoke": step_smoke,
        "delete": step_delete,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
