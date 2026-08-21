#!/usr/bin/env python3
"""Publish lemonseed.local as a temporary Avahi CNAME during install handoff.

Only runs while this appliance is not named \"lemonseed\" and still needs the
installer (or a version-review page) to find it. Exits once handoff is done
(/run/lemonseed/mdns-handoff-done) or the appliance is fully confirmed, so
multiple appliances can share a LAN without a permanent lemonseed.local claim.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

import dbus

AVAHI_NAME = "org.freedesktop.Avahi"
AVAHI_PATH = "/"
AVAHI_IFACE_SERVER = "org.freedesktop.Avahi.Server"
AVAHI_IFACE_ENTRY = "org.freedesktop.Avahi.EntryGroup"

IF_UNSPEC = -1
PROTO_UNSPEC = -1
DNS_CLASS_IN = 0x0001
DNS_TYPE_CNAME = 0x0005
TTL = 60

ALIAS = "lemonseed.local"
PRIMARY = "lemonseed"

HOSTNAME_FILE = Path("/etc/hostname")
SETUP_COMPLETE_FILE = Path("/etc/appliance-setup-complete")
SETUP_STATE_FILE = Path("/etc/appliance-setup.json")
VERSION_FILE = Path("/run/appliance/version")
HANDOFF_DONE_FILE = Path("/run/lemonseed/mdns-handoff-done")


def encode_dns_name(name: str) -> bytes:
    """Encode a DNS name as length-prefixed labels + NUL (RFC 1035)."""
    out = bytearray()
    for label in name.rstrip(".").split("."):
        if not label:
            continue
        raw = label.encode("ascii")
        if len(raw) > 63:
            raise ValueError(f"DNS label too long: {label!r}")
        out.append(len(raw))
        out.extend(raw)
    out.append(0)
    return bytes(out)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def live_hostname() -> str:
    return _read_text(HOSTNAME_FILE).splitlines()[0].strip() if HOSTNAME_FILE.is_file() else ""


def load_setup() -> dict:
    raw = _read_text(SETUP_STATE_FILE)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def handoff_needed() -> bool:
    """True while the installer may still need lemonseed.local to find us."""
    if HANDOFF_DONE_FILE.is_file():
        return False
    host = live_hostname()
    if not host or host == PRIMARY:
        return False
    if not SETUP_COMPLETE_FILE.is_file():
        return True
    setup = load_setup()
    confirmed = str(setup.get("confirmed_version") or "")
    current = _read_text(VERSION_FILE) or "unknown"
    return confirmed != current


class AliasPublisher:
    def __init__(self) -> None:
        self.bus = dbus.SystemBus()
        self.server = dbus.Interface(
            self.bus.get_object(AVAHI_NAME, AVAHI_PATH),
            AVAHI_IFACE_SERVER,
        )
        self.group = None
        self.published_target: str | None = None
        self._running = True

    def stop(self, *_args) -> None:
        self._running = False

    def _reset(self) -> None:
        if self.group is not None:
            try:
                self.group.Reset()
            except dbus.DBusException:
                pass
            self.group = None
        self.published_target = None

    def publish(self) -> None:
        fqdn = str(self.server.GetHostNameFqdn())
        if self.published_target == fqdn and self.group is not None:
            return
        self._reset()
        path = self.server.EntryGroupNew()
        group = dbus.Interface(self.bus.get_object(AVAHI_NAME, path), AVAHI_IFACE_ENTRY)
        group.AddRecord(
            IF_UNSPEC,
            PROTO_UNSPEC,
            dbus.UInt32(0),
            ALIAS,
            DNS_CLASS_IN,
            DNS_TYPE_CNAME,
            TTL,
            encode_dns_name(fqdn),
        )
        group.Commit()
        self.group = group
        self.published_target = fqdn
        print(f"lemonseed-avahi-alias: handoff {ALIAS} -> {fqdn}", flush=True)

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)

        if not handoff_needed():
            print("lemonseed-avahi-alias: no handoff needed; exiting", flush=True)
            return 0

        while self._running and handoff_needed():
            try:
                self.publish()
            except dbus.DBusException as exc:
                print(f"lemonseed-avahi-alias: dbus error: {exc}", file=sys.stderr, flush=True)
                self._reset()
            for _ in range(5):
                if not self._running or not handoff_needed():
                    break
                time.sleep(1)

        self._reset()
        if HANDOFF_DONE_FILE.is_file():
            print("lemonseed-avahi-alias: handoff complete; alias released", flush=True)
        else:
            print("lemonseed-avahi-alias: handoff window closed; alias released", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(AliasPublisher().run())
