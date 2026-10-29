#!/usr/bin/env python3
"""Reclaim the configured mDNS hostname after spurious avahi renames.

avahi-daemon renames itself (lemonseed1 -> lemonseed1-2) whenever it believes
the host name is taken. Docker bridge/veth churn on a single host can trigger
such a false conflict, after which <name>.local stops resolving. This watcher
polls the daemon over D-Bus; when its host name no longer matches the system
hostname it restarts avahi-daemon so Avahi re-probes and reclaims the name —
but only if the name is actually free (not owned by another host on the LAN).
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time

import dbus

AVAHI_NAME = "org.freedesktop.Avahi"
AVAHI_PATH = "/"
AVAHI_IFACE_SERVER = "org.freedesktop.Avahi.Server"

POLL_SECONDS = 60
# A genuine name conflict (another host owns the name) makes every restart
# rename again; never restart more often than this so the loop stays calm.
RESTART_MIN_SECONDS = 300


def avahi_hostname(bus: dbus.Bus) -> str:
    obj = bus.get_object(AVAHI_NAME, AVAHI_PATH)
    server = dbus.Interface(obj, AVAHI_IFACE_SERVER)
    return str(server.GetHostName())


def resolve_hostname(name: str) -> str | None:
    """Return the IPv4 address mDNS resolves for `name`, or None."""
    try:
        return socket.gethostbyname(name)
    except OSError:
        return None


def decide(avahi_name: str, want: str, want_ip: str | None, own_ip: str | None) -> str:
    """Return "ok", "restart" or "leave" (name owned by someone else)."""
    if avahi_name.removesuffix(".local") == want:
        return "ok"
    if want_ip is None:
        # Nobody answers for the configured name; avahi renamed itself over a
        # stale/ghost record, so the name is free to reclaim.
        return "restart"
    if want_ip == own_ip:
        # The claimant is our own announcement; a restart re-probes it.
        return "restart"
    return "leave"


def restart_avahi() -> bool:
    result = subprocess.run(
        ["systemctl", "restart", "avahi-daemon.service"],
        check=False,
    )
    return result.returncode == 0


def run() -> int:
    bus = dbus.SystemBus()
    last_restart = 0.0
    while True:
        try:
            avahi_name = avahi_hostname(bus)
        except dbus.DBusException as exc:
            # Daemon down or restarting: not a rename, just wait it out.
            print(f"lemonseed-avahi-watch: dbus error: {exc}", file=sys.stderr, flush=True)
            time.sleep(POLL_SECONDS)
            continue

        want = socket.gethostname()
        action = decide(
            avahi_name,
            want,
            resolve_hostname(f"{want}.local"),
            resolve_hostname(f"{avahi_name}.local"),
        )

        if action == "ok":
            pass
        elif action == "leave":
            print(
                f"lemonseed-avahi-watch: {want}.local is owned by another host;"
                " avahi stays renamed, not restarting",
                flush=True,
            )
        elif time.monotonic() - last_restart < RESTART_MIN_SECONDS:
            print("lemonseed-avahi-watch: back off before restarting again", flush=True)
        else:
            print(
                f"lemonseed-avahi-watch: avahi renamed {want} -> {avahi_name};"
                " restarting avahi-daemon to reclaim it",
                flush=True,
            )
            last_restart = time.monotonic()
            if not restart_avahi():
                print("lemonseed-avahi-watch: restart failed", file=sys.stderr, flush=True)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        raise SystemExit(0)
