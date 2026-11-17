#!/usr/bin/env python3
"""Lemonseed VM test harness — the appliance smoke phase (TESTING.md, one block).

Runs the whole appliance contract against a real VM under this host's libvirt:
define + boot the installer ISO from the default NAT network's storage, drive
the web installer's own API to install, and hand the installed image to the
QEMU smoke suite unchanged (through a local TCP forward to the VM's :80 — the
suite's assertion library is the same one `make test-qemu` uses).

Everything talks to libvirt through its **native Python bindings** (`import
libvirt`, python3-libvirt) — no virsh callouts. IP discovery reads the NAT
network's DHCP leases (`net.DHCPLeases()`), keyed on the VM's own MAC, which is
what killed the mDNS-discovery class of failures: a lease maps one machine to
one IP, while an mDNS name answers with whichever box claimed it last.

The allowlist is still the phase's first invariant: this hypervisor may serve
other workloads some day, so the harness refuses any domain whose name is not
covered by ``LEMONSEED_VM_ALLOWLIST`` (default ``lemonseed-*``) and only ever
creates volumes named after an allowed domain in the storage pool.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import NoReturn

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "out"
STATE = OUT / "vm-state.json"

LIBVIRT_URI = os.environ.get("LEMONSEED_LIBVIRT_URI", "qemu:///system")
NETWORK_NAME = "default"  # the libvirt NAT network (virbr0); DHCP leases live here
POOL_NAME = os.environ.get("LEMONSEED_STORAGE_POOL", "default")

VM_ALLOWLIST_ENV = "LEMONSEED_VM_ALLOWLIST"
VM_NAME_PREFIX = "lemonseed-vm-"
INSTALLER_MDNS = "lemonseed.local"
ISO_NAME = "lemonseed-installer.iso"
FORWARD_PORT = 19080  # the smoke suite's default
MDNS_IFACE = "virbr0"  # the NAT bridge the appliance multicasts on

FOOTPRINT = {"vcpu": 4, "memory_mib": 16384, "disk_gib": 40}  # 16 GiB: /var's tmpfs
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


def ensure_name_allowed(name: str) -> None:
    rules = allowlist(VM_ALLOWLIST_ENV, "lemonseed-*")
    if not any(_matches(r, name) for r in rules):
        fail(
            f"domain {name} is not covered by {VM_ALLOWLIST_ENV}"
            f"={','.join(rules)} — refusing"
        )


def ensure_pool_allowed(pool_name: str) -> None:
    """Only our own prefixed volumes may ever be created; the pool itself is
    local, so a wrong pool costs disk space at worst — but stay explicit."""
    if not pool_name:
        fail("storage pool name is empty — refusing")


def _qemu_permissions() -> str:
    """Volume permissions that let QEMU open the file. libvirtd on this host
    does not chown dynamically (dynamic_ownership is off with AppArmor), so a
    volume created root:root 0600 leaves qemu-system-x86_64 with EACCES at
    boot. Owner/group come from the actual `libvirt-qemu` user; when it does
    not exist (unusual setups), fall back to world-readable."""
    try:
        import pwd

        uid = pwd.getpwnam("libvirt-qemu").pw_uid
        gid = pwd.getpwnam("libvirt-qemu").pw_gid
        return f"<permissions><mode>0640</mode><owner>{uid}</owner><group>{gid}</group></permissions>"
    except KeyError:
        return "<permissions><mode>0644</mode></permissions>"


# ---------------------------------------------------------------- libvirt


class Host:
    """A thin native-bindings client: domain lifecycle, volumes, DHCP leases."""

    def __init__(self) -> None:
        try:
            import libvirt  # (python3-libvirt; see TESTING.md requirements)
        except ImportError as e:
            fail(f"the libvirt Python bindings are missing ({e}) — install python3-libvirt")
        self.libvirt = libvirt
        try:
            self.conn = libvirt.open(LIBVIRT_URI)
        except libvirt.libvirtError as e:
            fail(
                f"cannot connect to {LIBVIRT_URI}: {e} — if this is a fresh"
                " membership in the `libvirt` group, log out and back in first"
                " (a running desktop session keeps its old supplementary groups)"
            )

    # -- domain ------------------------------------------------------------

    def domain(self, name: str):
        try:
            return self.conn.lookupByName(name)
        except self.libvirt.libvirtError:
            return None

    def state_of(self, dom) -> str:
        state, _reason = dom.state()  # (state, reason); see VIR_DOMAIN_* enums
        names = {
            1: "running", 3: "paused", 4: "shutting-down", 5: "stopped",
            7: "crashed", 8: "suspended",
        }
        return names.get(state, f"state{state}")

    def start(self, name: str) -> None:
        ensure_name_allowed(name)
        dom = self.domain(name)
        if dom is None:
            fail(f"domain {name} does not exist — create first")
        if self.state_of(dom) != "running":
            dom.create()
        log(f"VM {name} started")

    def destroy_if_running(self, name: str) -> None:
        ensure_name_allowed(name)
        dom = self.domain(name)
        if dom is None:
            return
        if self.state_of(dom) == "running":
            try:
                dom.destroy()
            except self.libvirt.libvirtError as e:
                fail(f"cannot stop {name}: {e}")
            log(f"VM {name} stopped")

    # -- volumes -----------------------------------------------------------

    def volume(self, name: str):
        pool = self.conn.storagePoolLookupByName(POOL_NAME)
        try:
            return pool.storageVolLookupByName(name)
        except self.libvirt.libvirtError:
            return None

    def create_disk_volume(self, name: str, size_gib: int) -> None:
        ensure_name_allowed(name)
        if self.volume(name) is not None:
            log(f"volume {name} already exists — reusing it")
            return
        pool = self.conn.storagePoolLookupByName(POOL_NAME)
        vol_xml = (
            f"<volume><name>{name}</name>"
            f"<capacity unit='G'>{size_gib}</capacity>"
            f"<target><format type='qcow2'/>{_qemu_permissions()}</target></volume>"
        )
        pool.createXML(vol_xml, 0)

    def upload_iso(self, iso_path: Path) -> None:
        """The installer ISO reaches the pool by stream upload from this host —
        no HTTP hop, no node-side pull. Reused when the size already matches."""
        ensure_name_allowed(ISO_NAME.rsplit(".", 1)[0])
        pool = self.conn.storagePoolLookupByName(POOL_NAME)
        iso_size = iso_path.stat().st_size
        try:
            vol = pool.storageVolLookupByName(ISO_NAME)
            if vol.info()[1] == iso_size:  # (type, capacity-bytes, …) — exact match
                log("the pool already holds the ISO (same size) — skipping the upload")
                return
        except self.libvirt.libvirtError:
            pass
        try:
            old = pool.storageVolLookupByName(ISO_NAME)
            old.delete(0)  # a stale ISO of a different build — replace it
        except self.libvirt.libvirtError:
            pass
        vol_xml = (
            f"<volume><name>{ISO_NAME}</name>"
            f"<capacity unit='bytes'>{iso_size}</capacity>"
            f"<target><format type='raw'/>{_qemu_permissions()}</target></volume>"
        )
        vol = pool.createXML(vol_xml, 0)
        st = self.conn.newStream(0)
        vol.upload(st, 0, iso_size, 0)

        def sink(stream, _n, opaque):
            return f.read(opaque[0])

        with open(iso_path, "rb") as f:
            st.sendAll(sink, (1024 * 1024,))
            st.finish()
        log("the ISO is in the pool")

    def delete_volumes(self, name: str) -> None:
        ensure_name_allowed(name)
        for vol_name in (f"{name}-disk.qcow2",):
            vol = self.volume(vol_name)
            if vol is not None:
                vol.delete(0)
                log(f"volume {vol_name} deleted")

    # -- network -----------------------------------------------------------

    def lease_ip(self, mac: str) -> str | None:
        """The VM's DHCP lease on the NAT network, keyed by its own MAC —
        never an mDNS name (an mDNS claim answers with whichever box took it)."""
        net = self.conn.networkLookupByName(NETWORK_NAME)
        for lease in net.DHCPLeases():
            if str(lease.get("mac", "")).lower() == mac.lower():
                ipaddr = lease.get("ipaddr")
                if ipaddr:
                    return str(ipaddr)
        return None

    def wait_lease(self, mac: str, deadline_s: float) -> str:
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            ip = self.lease_ip(mac)
            if ip:
                return ip
            time.sleep(3)
        fail(f"no DHCP lease for {mac} on {NETWORK_NAME} within {deadline_s:.0f}s")


def domain_mac(host: "Host", dom) -> str:
    """The NIC MAC from the domain's XML — live config first (a running
    domain always carries it), then the inactive definition."""
    active = getattr(host.libvirt, "VIR_DOMAIN_XML_ACTIVE", 1)
    for flags in (active, 0):
        m = re.search(r"mac address=['\"]([0-9a-fA-F:]+)['\"]", dom.XMLDesc(flags))
        if m:
            return m.group(1)
    fail(f"domain {dom.name()} has no NIC MAC — is an interface defined?")


# ------------------------------------------------------- domain XML (create)


def domain_xml(name: str) -> str:
    """UEFI (firmware=efi autoselect), hd before cdrom — an empty disk falls
    through to the installer ISO, and after the install the same boot order
    boots the installed image, which retires the pve-era boot-order rewrite."""
    return f"""<domain type='kvm'>
  <name>{name}</name>
  <memory unit='MiB'>{FOOTPRINT['memory_mib']}</memory>
  <vcpu placement='static'>{FOOTPRINT['vcpu']}</vcpu>
  <os firmware='efi'>
    <type arch='x86_64' machine='q35'>hvm</type>
    <!-- The appliance's bootloader is not Microsoft-signed: booting with SB
         on (libvirt's autoselection picks OVMF_CODE_4M.ms.fd) strands the
         guest in the firmware. The pve harness made the same choice via
         efidisk0 pre-enrolled-keys=0. -->
    <firmware>
      <feature enabled='no' name='secure-boot'/>
    </firmware>
    <boot dev='hd'/>
    <boot dev='cdrom'/>
  </os>
  <features><acpi/><apic/></features>
  <cpu mode='host-model'/>
  <clock offset='utc'/>
  <!-- AppArmor confinement without virt-aa-helper's generated per-disk rules
       denies every image open (the .files include is never written on this
       host), so the dev-box VM runs unconfined and leans on DAC instead: the
       volumes are owned by libvirt-qemu with 0640 via _qemu_permissions(). -->
  <seclabel type='none' model='none'/>
  <on_poweroff>destroy</on_poweroff><on_reboot>restart</on_reboot><on_crash>destroy</on_crash>
  <devices>
    <disk type='volume' device='disk'>
      <driver name='qemu' type='qcow2'/>
      <source pool='{POOL_NAME}' volume='{name}-disk.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='volume' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source pool='{POOL_NAME}' volume='{ISO_NAME}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
    </disk>
    <interface type='network'>
      <!-- The appliance kernel is a NixOS modules-shrunk build: it carries
           virtio_net but no rtl8139, and libvirt's default NIC model is
           rtl8139 — a guest that boots with no working NIC at all. -->
      <source network='{NETWORK_NAME}'/>
      <model type='virtio'/>
    </interface>
    <console type='pty'><target type='serial' port='0'/></console>
  </devices>
</domain>"""


# ------------------------------------------------------------- find helpers


def state_write(name: str, ip: str | None) -> None:
    OUT.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"domain": name, "ip": ip}, indent=1))


def state_read() -> dict:
    if not STATE.is_file():
        fail(f"{STATE} is missing — create + install first")
    return json.loads(STATE.read_text())


def curl(url: str, timeout: float = 10) -> tuple[int, dict | None]:
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def _ssl_ctx():
    import ssl

    return ssl._create_unverified_context()


def post_json(url: str, body: dict) -> tuple[int, dict | None]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx()) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


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
    host = Host()
    rules = allowlist(VM_ALLOWLIST_ENV, "lemonseed-*")
    log(f"allowlist: {VM_ALLOWLIST_ENV}={','.join(rules)}  pool={POOL_NAME}")
    log(f"domains on {LIBVIRT_URI}:")
    for dom in sorted(host.conn.listAllDomains(), key=lambda d: d.name()):
        allowed = any(_matches(rule, dom.name()) for rule in rules)
        m = re.search(r"mac address=['\"]([0-9a-fA-F:]+)['\"]", dom.XMLDesc(0))
        mac = m.group(1) if m else "-"
        ip = host.lease_ip(mac) if mac != "-" and allowed else None
        log("  %-28s %-12s %s%s" % (
            dom.name(), host.state_of(dom),
            f"ip={ip} " if ip else "",
            "" if allowed else "(off-limits)"))


def step_create(args: argparse.Namespace) -> None:
    host = Host()
    name = f"{VM_NAME_PREFIX}{time.strftime('%Y%m%d%H%M')}"
    ensure_name_allowed(name)
    ensure_pool_allowed(POOL_NAME)
    iso = OUT / "latest-installer" / ISO_NAME
    if not iso.is_file():
        fail(f"{iso} is missing — run make build-installer first")
    dom = host.domain(name)
    if dom is not None:
        log(f"domain {name} already exists — reusing it")
        state_write(name, None)
        return
    log(f"creating {name}: {FOOTPRINT['vcpu']} vCPU, {FOOTPRINT['memory_mib']} MiB,"
        f" {FOOTPRINT['disk_gib']} GiB qcow2 in pool {POOL_NAME}")
    host.create_disk_volume(f"{name}-disk.qcow2", FOOTPRINT["disk_gib"])
    host.upload_iso(iso)
    try:
        host.conn.defineXML(domain_xml(name))
    except host.libvirt.libvirtError as e:
        fail(f"define {name} -> {e}")
    state_write(name, None)
    log(f"VM {name} defined — start it with: vm-test.py start")


def step_start(args: argparse.Namespace) -> None:
    st = state_read()
    name = args.vmid or st.get("domain")
    if not name:
        fail("no domain to start — create first (or pass its name)")
    host = Host()
    host.start(name)
    dom = host.domain(name)
    # The MAC is stable for the VM's lifetime: read it from the live XML,
    # so every later lease lookup keys on exactly this NIC.
    mac = domain_mac(host, dom)
    log(f"VM {name} booted ({mac}) — waiting for its DHCP lease")
    ip = host.wait_lease(mac, deadline_s=300)
    state_write(name, ip)
    log(f"{name} is at {ip}")


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
    host = Host()
    st = state_read()
    name = str(st["domain"])
    ensure_name_allowed(name)
    dom = host.domain(name)
    if dom is None:
        fail(f"domain {name} is missing — create first")
    if host.state_of(dom) != "running":
        log("the VM is not running — starting it")
        step_start(argparse.Namespace(vmid=name))
    mac = domain_mac(host, dom)
    ip = host.lease_ip(mac)
    if not ip:
        fail(f"no DHCP lease for {name} ({mac}) — start it again")
    deadline = time.monotonic() + 300
    installer_status: dict = {}
    while time.monotonic() < deadline:
        code, data = curl(f"http://{ip}/api/status", timeout=5)
        if code == 200 and isinstance(data, dict) and data.get("role") == "installer":
            installer_status = data
            break
        time.sleep(5)
    else:
        fail(f"no installer answered on {ip} within 300s")
    log(f"the installer is at {ip} (state={installer_status.get('state')})")
    if installer_status.get("phase") == "await_usb" or installer_status.get("can_reboot"):
        # A previous install already completed (the VM is rebooted from an
        # earlier run's state): do not wipe the disk and reinstall.
        log(f"the install is already complete ({installer_status.get('message')}) — skipping the reinstall")
    else:
        code, disks = curl(f"http://{ip}/api/disks", timeout=10)
        if code != 200 or not isinstance(disks, dict) or not disks.get("disks"):
            fail(f"no disks from the installer: {disks}")
        disk = disks["disks"][0]["path"]
        log(f"installing onto {disk}")
        # The artifacts the installer downloads are the release set
        # (out/release-<version>/), served by this host's artifact server.
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
            code, st_now = curl(f"http://{ip}/api/status", timeout=10)
            if code == 200 and isinstance(st_now, dict):
                # "await_usb" is the installer's own 'is installed; remove the
                # USB, then click Reboot' verdict — can_reboot is True there.
                if st_now.get("phase") == "await_usb" or st_now.get("can_reboot"):
                    log(f"install finished: {st_now.get('message')}")
                    break
                if st_now.get("phase") == "error":
                    fail(f"install failed: {st_now.get('error')} (detail: {st_now.get('detail')})")
            time.sleep(10)
        else:
            fail("the install did not finish within an hour")
    # The installer's own verdict is "remove the USB, then click Reboot" —
    # and the same applies to us: with the ISO still attached, OVMF's NVRAM
    # boot entry from the first boot wins over our hd-first order and the
    # reboot lands back in the installer. Eject before rebooting.
    try:
        dom.updateDeviceFlags(
            "<disk type='volume' device='cdrom'>"
            "<driver name='qemu' type='raw'/>"
            "<target dev='sda' bus='sata'/><readonly/></disk>", 0)
        log("the installer ISO is ejected")
    except host.libvirt.libvirtError as e:
        fail(f"ejecting the ISO -> {e}")
    code = data = None
    for attempt in range(12):
        code, data = post_json(f"http://{ip}/api/reboot", {})
        if code in (200, 202):
            break
        time.sleep(5)
    if code not in (200, 202):
        fail(f"/api/reboot -> {code} {data}")
    log("rebooting into the installed image")
    # The lease is stable across reboots — same MAC, same network — and the
    # recorded IP below stays THE VM's IP. But HTTP 200 alone is not enough:
    # the installer answers it too, so wait for the control plane's own role.
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        code, st_now = curl(f"http://{ip}/api/status", timeout=8)
        if code == 200 and isinstance(st_now, dict) and st_now.get("role") != "installer":
            break
        time.sleep(5)
    else:
        fail(f"the installed image did not come back on {ip} within 600s "
             f"(last status: {st_now})")
    state_write(name, ip)
    log(f"{name} is serving the portal at http://{ip}:80")


def step_smoke(_args: argparse.Namespace) -> None:
    st = state_read()
    ip = st.get("ip")
    if not ip:
        fail("the VM's IP is not in the state file — install first")
    # The recorded IP is the install step's DHCP-lease fact for this VM's own
    # MAC. Never re-resolve an mDNS name here: a name answers with whichever
    # box claimed it, and every assertion would hit that other machine while
    # looking green.
    log(f"running the smoke suite (skip-boot) against http://{ip}:80")
    thread = threading.Thread(
        target=forward, args=(FORWARD_PORT, ip, 80), daemon=True
    )
    thread.start()
    # check=True so the smoke suite's exit code 1 propagates: the suite's
    # own set -euo pipefail already fails it on any non-zero return code,
    # but check=False here was masking that as a silent pass.
    proc = subprocess.run(
        ["bash", str(REPO / "scripts" / "qemu-api-smoke-test.sh")],
        cwd=str(REPO), check=True,
        env={**os.environ, "SMOKE_SKIP_BOOT": "1"},
    )
    log("the smoke suite passed (exit %s)" % proc.returncode)


def step_delete(args: argparse.Namespace) -> None:
    st = state_read()
    name = args.vmid or st.get("domain")
    if not name:
        fail("no domain to delete — create first (or pass its name)")
    host = Host()
    host.destroy_if_running(name)
    dom = host.domain(name)
    if dom is not None:
        try:
            # NVRAM: UEFI domains get a per-domain VARS file; undefining
            # without this flag refuses ("cannot undefine domain with nvram").
            dom.undefineFlags(host.libvirt.VIR_DOMAIN_UNDEFINE_NVRAM)
        except host.libvirt.libvirtError as e:
            fail(f"undefine {name} -> {e}")
    host.delete_volumes(name)
    log(f"VM {name} deleted")


# ------------------------------------------------------- mDNS function test


def step_mdns_probe(args: argparse.Namespace) -> None:
    """Prove the appliance's mDNS advertising works on the NAT segment —
    without touching this host's system avahid (which may not listen on
    virbr0 at all). A single stdlib mDNS query is multicasted onto the bridge
    and every response is dumped: name, IP, TTL. Zero new dependencies."""
    iface = args.iface or MDNS_IFACE
    want_name = args.name or INSTALLER_MDNS
    try:
        import struct

        ifaces_output = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "dev", iface],
            check=True, capture_output=True, text=True,
        ).stdout
        host_ip = ifaces_output.split("inet ")[1].split("/")[0]
    except Exception as e:
        fail(f"cannot find an IPv4 address on {iface}: {e}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host_ip, 0))
    mcast_group = socket.inet_aton("224.0.0.251") + socket.inet_aton(host_ip)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mcast_group)
    sock.settimeout(1.0)

    def qname(name: str) -> bytes:
        out = b""
        for part in name.split("."):
            out += struct.pack("B", len(part)) + part.encode()
        return out + b"\x00"

    # One-shot A query for the appliance's mDNS name (and PTR of _http._tcp
    # so service advertisement is exercised, not just hostname resolution).
    queries = [(qname(want_name), 1)]  # type IN=A
    packet = struct.pack(">HHHHHH", 0, 0x0100, 1, 0, 0, 0) + b"".join(
        qn + struct.pack(">HH", 1, 0x0001) for qn, _ in queries
    )
    ptr_packet = (
        struct.pack(">HHHHHH", 0, 0x0100, 1, 0, 0, 0)
        + qname("_http._tcp.local") + struct.pack(">HH", 12, 0x0001)
    )

    deadline = time.monotonic() + args.timeout
    sock.sendto(packet, ("224.0.0.251", 5353))
    sock.sendto(ptr_packet, ("224.0.0.251", 5353))
    log(f"mDNS probe on {iface} ({host_ip}) for '{want_name}' — dumping responses")
    responders: dict[str, list[str]] = {}

    def read_name(data: bytes, off: int) -> tuple[str, int]:
        # Minimal DNS name reader with compression support (RFC 1035 §4.1.4).
        labels: list[str] = []
        jumps = 0
        while True:
            if off >= len(data) or jumps > 8:
                return ".".join(labels), off + 1
            length = data[off]
            if length == 0:
                return ".".join(labels), off + 1
            if length & 0xC0:  # a compression pointer to an earlier name
                ptr = ((length & 0x3F) << 8) | data[off + 1]
                tail, _ = read_name(data, ptr)
                return (".".join(labels + [tail]) if tail else ".".join(labels)), off + 2
            labels.append(data[off + 1:off + 1 + length].decode(errors="replace"))
            off += 1 + length
            jumps += 1

    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        # Skip the header (12 bytes) and one question to reach the answers.
        off = 12
        try:
            qdcount = struct.unpack(">H", data[4:6])[0]
            for _ in range(qdcount):
                _, off = read_name(data, off)
                off += 4
            ancount = struct.unpack(">H", data[6:8])[0]
            names: list[str] = []
            for _ in range(ancount):
                name, off = read_name(data, off)
                rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[off:off + 10])
                off += 10
                if name:
                    names.append(name)
                if rtype == 12 and rdlen:  # PTR — the service points at a host
                    ptr_name, _ = read_name(data, off)
                    if ptr_name:
                        names.append(ptr_name)
                off += rdlen
        except Exception:
            continue
        responders.setdefault(addr[0], [])
        for n in names:
            if n not in responders[addr[0]]:
                responders[addr[0]].append(n)
    sock.close()
    if not responders:
        fail(f"no mDNS responses on {iface} within {args.timeout:.0f}s — "
             f"the appliance's avahi is not advertising on the NAT bridge")
    for ip, names in sorted(responders.items()):
        log(f"  responder {ip}: {', '.join(names[:6])}")
    # The advertised NAME is what setup chose (lemonseed.local before first
    # boot; an installed appliance claims its own mdns_name) — mDNS function
    # is proven by ANY response, not by one specific name.
    log(f"OK: the appliance's avahi advertises on {iface}")


# ------------------------------------------------------------------- main


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--artifacts-host", default=None, help="host:port serving the artifacts for the install")
        p.add_argument("--artifacts-dir", default=None, help="the release set's dir name (e.g. release-20261114.01)")
        p.add_argument("--vmid", default=None, help="the domain name to act on (start/delete; defaults to the state file)")

    common(ap)
    for name in ("status", "create", "start", "install", "smoke", "delete"):
        common(sub.add_parser(name))
    probe = sub.add_parser("mdns-probe", help="test mDNS advertising on the NAT bridge")
    probe.add_argument("--iface", default=None, help=f"the interface to multicast on (default {MDNS_IFACE})")
    probe.add_argument("--name", default=None, help=f"the mDNS name to query (default {INSTALLER_MDNS})")
    probe.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args()
    {
        "status": step_status,
        "create": step_create,
        "start": step_start,
        "install": step_install,
        "smoke": step_smoke,
        "delete": step_delete,
        "mdns-probe": step_mdns_probe,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
