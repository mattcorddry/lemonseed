"""Named settings over the CP's ``cp_meta`` KV store (M8 P3).

Deliberately minimal: ``get_setting`` / ``set_setting`` with a per-name
whitelist. The release channel is the first resident; P5's settings UI reads
and writes through these two calls rather than inventing a second store.

The updater cannot read Postgres (root jobs stay DB-free by design), so the
setting is *materialized* to ``/var/lib/lemonseed/channel`` — rewritten on
every set. Before the first ``set``, readers resolve the seeded choice the
same way the updater does (file, then the installer's kernel-command-line
stub), so nothing ever disagrees about what the box is on.
"""

from __future__ import annotations

import syslog
from pathlib import Path
from typing import Any, Optional

import db

CHANNEL_FILE = Path("/var/lib/lemonseed/channel")
_CMDLINE = Path("/proc/cmdline")

# A typo'd name must never become a row: writers are whitelisted, readers are
# not (an unknown name simply falls through to the default). The default for
# a known name is *the name's* default — the channel is stable, not None.
_VALIDATORS: dict[str, Any] = {
    "release_channel": lambda v: v in ("stable", "beta"),
}
_DEFAULTS: dict[str, str] = {
    "release_channel": "stable",
}


def _read_channel_file() -> str:
    try:
        return CHANNEL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cmdline_channel() -> str:
    """``lemonseed.channel=`` from /proc/cmdline — the installer's ESP stub
    for a fresh install, whose user-data has no @var yet (seed_esp)."""
    path = _CMDLINE
    if not path.is_file():
        return ""
    prefix = "lemonseed.channel="
    for arg in path.read_text(encoding="utf-8").split():
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return ""


def get_setting(name: str, default: Optional[str] = None) -> Optional[str]:
    """The setting's value, or *default*.

    No row yet is not an error: the release channel falls back to the seeded
    file and then the installer's command-line stub, in the same order the
    updater resolves them. A DB that is down reads the same way — a check
    never looks like a broken appliance because Postgres hiccuped.
    """
    row = db.cp_get_meta(name)  # None when down or absent
    if isinstance(row, str) and row:
        return row
    if name == "release_channel":
        raw = _read_channel_file()
        if raw in ("stable", "beta"):
            return raw
        raw = _cmdline_channel()
        if raw in ("stable", "beta"):
            return raw
    if name in _DEFAULTS:
        return _DEFAULTS[name]
    return default


def set_setting(name: str, value: str) -> bool:
    """Validate, store, and materialize. False on any failure — the caller is
    a service boundary that reports, never raises, into the HTTP answer."""
    check = _VALIDATORS.get(name)
    if check is None or not check(value):
        return False
    if not db.cp_set_meta(name, value):
        return False
    try:
        CHANNEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHANNEL_FILE.write_text(value + "\n", encoding="utf-8")
    except OSError as exc:
        syslog.syslog(
            syslog.LOG_WARNING,
            f"lemonseed-settings: could not materialize {name} to {CHANNEL_FILE}: {exc}",
        )
        return False
    return True
