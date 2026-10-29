#!/usr/bin/env python3
"""Lemonseed A/B image updater.

Writes verified artifacts into the inactive root slot + ESP kernels, then sets
GRUB boot_once so the next reboot tries the new slot. Lock-in promotes it only
after a healthy boot. User-data (/etc, /var) is never touched.

Usage:
  lemonseed-update status
  lemonseed-update [--url URL | --dir DIR] [--reboot] [--force]
  lemonseed-update apply [--url URL | --dir DIR] [--reboot] [--force]
  lemonseed-update check [--url URL] [--record]
  lemonseed-update cancel
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from lemonseed_artifacts import (
    CHANNELS,
    ArtifactError,
    acquire_artifacts,
    cleanup_payload,
    fetch_manifest,
    image_base_url,
    resolve_channel,
    resolve_release_base,
    rollback_floor,
    sha256_device,
    sha256_file,
    write_raw,
)

DOWNLOAD_DIR = Path(os.environ.get("LEMONSEED_DOWNLOAD_DIR", "/var/lib/lemonseed/download"))
ESP_MOUNT = Path("/run/lemonseed-esp")
AB_HELPER = os.environ.get("LEMONSEED_AB", "lemonseed-ab")
GRUB_CFG = Path(os.environ.get("LEMONSEED_GRUB_CFG", "/etc/lemonseed/grub.cfg"))
GRUB_CFG_FALLBACK = Path("/usr/share/lemonseed/grub.cfg")
ATTEMPT_FILE = Path(os.environ.get("LEMONSEED_ATTEMPT_FILE", "/var/lib/lemonseed/update-attempt.json"))
LATEST_FILE = Path(os.environ.get("LEMONSEED_LATEST_FILE", "/var/lib/lemonseed/latest-update.json"))
SLOT_VERSIONS_FILE = Path(os.environ.get(
    "LEMONSEED_SLOT_VERSIONS_FILE", "/var/lib/lemonseed/slot-versions.json"
))
VERSION_FILE = Path("/run/appliance/version")
# The panel polls the job every 2s; rebooting the instant the word is printed
# can outrun that poll, so the dialog freezes at the last write percent and
# the user never sees the finish (bare-metal review, BM-4).
REBOOT_GRACE_S = 3.0


class UpdateError(Exception):
    pass


def log(message: str) -> None:
    print(f"lemonseed-update: {message}", flush=True)


def progress(**kwargs: Any) -> None:
    msg = kwargs.get("message")
    pct = kwargs.get("progress")
    if msg is None:
        return
    if pct is None:
        log(str(msg))
    else:
        log(f"[{pct:3d}%] {msg}")


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def require_root() -> None:
    if os.geteuid() != 0:
        raise UpdateError("must run as root")


def cmdline_slot() -> str | None:
    path = Path("/proc/cmdline")
    if not path.is_file():
        return None
    for arg in path.read_text(encoding="utf-8").split():
        if arg.startswith("lemonseed.slot="):
            slot = arg.split("=", 1)[1]
            if slot in ("a", "b"):
                return slot
    return None


def ab_status() -> dict[str, str]:
    try:
        out = run([AB_HELPER, "status"]).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise UpdateError(f"Could not read A/B status: {exc}") from exc
    values: dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def current_slot() -> str:
    slot = cmdline_slot()
    if slot:
        return slot
    status = ab_status()
    default = status.get("boot_default", "")
    if default in ("a", "b"):
        return default
    return "a"


def inactive_slot(active: str) -> str:
    if active not in ("a", "b"):
        raise UpdateError(f"Invalid active slot: {active}")
    return "b" if active == "a" else "a"


def part_path(label: str) -> str:
    path = Path(f"/dev/disk/by-partlabel/{label}")
    if not path.exists():
        raise UpdateError(f"Missing partition PARTLABEL={label}")
    return str(path.resolve())


def esp_is_mounted() -> bool:
    return (
        subprocess.run(
            ["findmnt", "-n", str(ESP_MOUNT)],
            check=False,
            capture_output=True,
        ).returncode
        == 0
    )


def ensure_esp_mounted() -> Path:
    ESP_MOUNT.mkdir(parents=True, exist_ok=True)
    if not esp_is_mounted():
        run(["mount", "-t", "vfat", "-o", "rw,umask=0077", part_path("ESP"), str(ESP_MOUNT)])
    return ESP_MOUNT


def umount_esp() -> None:
    subprocess.run(["umount", str(ESP_MOUNT)], check=False, capture_output=True)


def refuse_dirty_state(*, force: bool) -> None:
    status = ab_status()
    pending = status.get("boot_once") or status.get("boot_attempted")
    if pending and not force:
        raise UpdateError(
            f"Update already pending (boot_once={status.get('boot_once', '')}, "
            f"boot_attempted={status.get('boot_attempted', '')}); "
            "use --force to overwrite the inactive slot anyway"
        )


def preflight(*, force: bool) -> None:
    """Refuse to start unless the box's slot pointer is trustworthy and clean.

    'Write the inactive slot' is only safe when the appliance *knows* which
    slot it is running from and nothing is already pending: a guess (the
    fallback default) could overwrite the running root, and a leftover
    arm-time record means a previous attempt never landed. The panel sends no
    --force, so every refusal is a clear message; an operator who really wants
    to overwrite a dirty state passes --force on the CLI.
    """
    if cmdline_slot() is None:
        default = ab_status().get("boot_default", "")
        if default not in ("a", "b"):
            raise UpdateError(
                "could not tell which slot is active (no lemonseed.slot on the "
                "kernel command line and no boot_default); refusing to guess — "
                "run 'lemonseed-ab status' and set a default first"
            )
    refuse_dirty_state(force=force)
    if ATTEMPT_FILE.exists():
        # The record is only dirt when the attempt did not land: booting the
        # target (or running the intended version) is a landed update whose
        # record simply outlived the panel's first read. Clearing it here lets
        # the next CLI apply proceed without a manual cancel; a failed-or-stuck
        # attempt keeps the refusal, because the failed-update banner's one-shot
        # semantics depend on the record surviving until it is seen.
        attempt = _read_attempt()
        target = str(attempt.get("target_slot") or "")
        intended = str(attempt.get("intended_version") or "")
        landed = (target and current_slot() == target) or (
            intended and running_version() == intended
        )
        if landed:
            clear_attempt()
            log("cleared the arm-time record of the update that landed")
        elif not force:
            raise UpdateError(
                f"a previous attempt is still recorded ({ATTEMPT_FILE}); "
                "run 'lemonseed-update cancel' first"
            )


def atomic_copy(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".new")
    shutil.copy2(src, tmp)
    with tmp.open("rb") as handle:
        os.fsync(handle.fileno())
    tmp.replace(dest)
    try:
        dirfd = os.open(str(dest.parent), os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except OSError:
        pass


def grubenv_path(esp: Path) -> Path:
    for candidate in (
        esp / "grub" / "grubenv",
        esp / "EFI" / "lemonseed" / "grubenv",
        esp / "boot" / "grub" / "grubenv",
    ):
        if candidate.is_file():
            return candidate
    raise UpdateError("grubenv not found on ESP")


def write_inactive_slot(
    files: dict[str, Path],
    target_slot: str,
    *,
    version: str,
    force: bool,
) -> None:
    """Write the release into the inactive slot and arm the one-shot boot.

    ``version`` rides along because the ESP boot menu names the version on
    each slot (M8 P6, D2): the grubenv vars are the menu's data source, and
    the write is the one moment both are known — the slot just written, and
    the running one self-healed for boxes whose last update predates the
    registry.
    """
    refuse_dirty_state(force=force)
    active = current_slot()
    if target_slot == active:
        raise UpdateError(f"Refusing to overwrite active slot {active}")

    # Resolve the ESP grub.cfg template BEFORE anything is written. The refresh
    # is not cosmetic — new kernel-param logic (e.g. the APU GPU memory split)
    # rides in it — and the old behavior skipped it silently when neither
    # candidate existed, so every installed box kept a stale ESP config until a
    # USB reinstall (bare-metal review; see PLAN.md P5). Failing here leaves
    # the inactive slot unwritten and the error names the paths that were
    # checked.
    grub_src = GRUB_CFG if GRUB_CFG.is_file() else GRUB_CFG_FALLBACK
    if not grub_src.is_file():
        raise UpdateError(
            "no grub.cfg template found (checked "
            f"{GRUB_CFG} and {GRUB_CFG_FALLBACK}); set LEMONSEED_GRUB_CFG — "
            "the ESP grub.cfg would go stale, so the slot was not written"
        )

    root_dev = part_path(f"root-{target_slot}")
    squash = files["root.squashfs"]
    expected = sha256_file(squash)
    log(f"writing SquashFS to {root_dev} (slot {target_slot})")
    write_raw(squash, root_dev, progress=progress, pct_lo=50, pct_hi=82)
    written = sha256_device(root_dev, squash.stat().st_size)
    if written.lower() != expected.lower():
        raise UpdateError(
            f"Post-write checksum mismatch on {root_dev}: got {written}, expected {expected}"
        )
    log(f"verified SquashFS hash on {root_dev}")

    esp = ensure_esp_mounted()
    try:
        efi_dir = esp / "EFI" / "lemonseed"
        efi_dir.mkdir(parents=True, exist_ok=True)
        atomic_copy(files["vmlinuz"], efi_dir / f"vmlinuz-{target_slot}")
        atomic_copy(files["initrd.img"], efi_dir / f"initrd-{target_slot}.img")
        log(f"updated ESP kernels for slot {target_slot}")
        # Refresh grub.cfg so new kernel-param logic (e.g. extra_params for
        # APU GPU memory) is picked up on the next boot. Newer images ship
        # the template at /usr/share/lemonseed/grub.cfg; older images
        # (pre-M6.4) only had /etc/lemonseed/grub.cfg. Resolution happened
        # before anything was written, so this copy cannot be skipped.
        grub_dir = esp / "grub"
        grub_dir.mkdir(parents=True, exist_ok=True)
        atomic_copy(grub_src, grub_dir / "grub.cfg")
        log(f"updated ESP grub.cfg (from {grub_src})")
        # The boot menu names the version on each slot (M8 P6, D2): the labels
        # are grubenv vars the template expands, so the write is two env sets —
        # the slot just written, and the running one. Non-fatal when the env
        # block is missing: the menu falls back to the bare slot name.
        env = grubenv_path(esp)
        run(["grub-editenv", str(env), "set", f"slot_{target_slot}_version={version}"], check=False)
        running = running_version()
        if running != "unknown":
            run(["grub-editenv", str(env), "set", f"slot_{active}_version={running}"], check=False)
        run([AB_HELPER, "set-once", target_slot, "--esp", str(esp)])
    finally:
        umount_esp()


def cmd_status() -> int:
    active = current_slot()
    status = ab_status()
    print(f"current_slot={active}")
    print(f"inactive_slot={inactive_slot(active)}")
    print(f"boot_default={status.get('boot_default', '')}")
    print(f"boot_once={status.get('boot_once', '')}")
    print(f"boot_attempted={status.get('boot_attempted', '')}")
    # Slot versions from the registry (M8 P6): the honest answer to "what am I
    # running and what would rollback boot?". A slot written before the
    # registry existed reads as unknown.
    registry = _read_slot_versions()
    for slot in ("a", "b"):
        version = str((registry["slots"].get(slot) or {}).get("version") or "unknown")
        mark = " (running)" if slot == active else ""
        print(f"slot_{slot}_version={version}{mark}")
    for label in ("root-a", "root-b", "ESP"):
        path = Path(f"/dev/disk/by-partlabel/{label}")
        print(f"part_{label}={'present' if path.exists() else 'missing'}")
    return 0


def cmd_apply(
    url: str | None,
    directory: str | None,
    channel: str | None = None,
    *,
    reboot: bool,
    force: bool,
) -> int:
    require_root()
    if url and directory:
        raise UpdateError("use either --url or --dir, not both")
    if url and channel:
        raise UpdateError("use either --url or --channel, not both")
    # Preflight before the download: nothing below touches a disk until the
    # slot pointer is trustworthy and no update is pending.
    preflight(force=force)
    active = current_slot()
    target = inactive_slot(active)
    log(f"active slot {active}; writing inactive slot {target}")
    files, version, manifest = acquire_artifacts(
        progress=progress,
        download_dir=DOWNLOAD_DIR,
        url=url,
        directory=directory,
        channel=channel,
    )
    running = running_version()
    if version == running and not force:
        raise UpdateError(
            f"the channel offers {version}, which is already running; "
            "nothing to update (use --force to reinstall it anyway)"
        )
    if version < running and running != "unknown" and not force:
        # D-C2: switching channels must never walk a box backwards. The beta
        # box moved to stable simply waits for stable to catch up. A box whose
        # running version cannot be read ("unknown") has nothing to compare —
        # dev sets and fresh probes — so the guard stays out of the way.
        raise UpdateError(
            f"the channel offers {version}, which is older than the running "
            f"{running}; refusing the downgrade (use --force to write it anyway)"
        )
    # The rollback marker suppresses *offers* (check, panel); an explicit
    # apply with --force is the operator overriding it deliberately.
    rolled_back = str(_read_slot_versions().get("rolled_back_version") or "")
    if rolled_back and version <= rolled_back and not force:
        raise UpdateError(
            f"the channel offers {version}, which was rolled back from; "
            "use --force to apply it anyway"
        )
    log(f"artifacts ready (version {version})")
    write_inactive_slot(files, target, version=version, force=force)
    # boot_once is armed; the detector's input exists only from here on.
    record_attempt(version, target)
    # Slot-version bookkeeping (M8 P6): the applied release is recorded for
    # the target slot — with the rollback floor its manifest declares — and
    # the running slot's record self-heals here for boxes whose last update
    # predates this registry. A successful apply also clears the rollback
    # marker: the operator just made a newer choice.
    rollback_block = manifest.get("rollback") if isinstance(manifest, dict) else {}
    if not isinstance(rollback_block, dict):
        rollback_block = {}
    floor = str(rollback_block.get("min_rollback_version") or "") or None
    record_slot_version(target, version, min_rollback_version=floor)
    if running != "unknown":
        record_slot_version(active, running)
    clear_rolled_back()
    # The payload is on its way to being the running system: the assembled
    # staging and the verified part cache are no longer needed (M8 P2).
    cleanup_payload(DOWNLOAD_DIR)
    log(f"pending boot_once={target}; reboot to try Lemonseed {version}")
    if reboot:
        # The write phase tops out at 82% and the remaining steps print no
        # percentages, so say 100 in so many words before the box goes down.
        progress(phase="writing", message="Update finished; the appliance is about to reboot", progress=100)
        log("rebooting now")
        time.sleep(REBOOT_GRACE_S)   # let the panel's poll see the finish
        run(["systemctl", "reboot"], check=False)
    return 0


def cmd_cancel() -> int:
    require_root()
    esp = ensure_esp_mounted()
    try:
        env = grubenv_path(esp)
        run(["grub-editenv", str(env), "unset", "boot_once"], check=False)
        run(["grub-editenv", str(env), "unset", "boot_attempted"], check=False)
        log("cleared pending boot_once / boot_attempted")
    finally:
        umount_esp()
    clear_attempt()
    return 0


def cmd_rollback(*, reboot: bool, force: bool) -> int:
    """Boot the previous slot (M8 P6, D2): a boot-pointer flip, not a write.

    The inactive slot still holds the release the last update replaced, so
    rolling back means making it the default — no download, no writes to
    either root, through the same ``lemonseed-ab lock-in`` primitive apply
    arms. Deliberately N-1: older releases come back through an explicit
    ``apply --force`` at an older set.

    The registry gates what the flip may target. The inactive slot must hold
    exactly the prior release — a newer re-apply over it means there is
    nothing to roll back to — and the running release's declared rollback
    floor (its manifest's ``rollback.min_rollback_version``) must include the
    prior version, so one-way-door releases are not walked back through.
    """
    require_root()
    # The same honesty rules as apply: a knowable slot, nothing pending, and
    # a landed attempt record cleared rather than stacked under the flip.
    preflight(force=force)
    active = current_slot()
    target = inactive_slot(active)
    running = running_version()
    registry = _read_slot_versions()
    prior_entry = registry["slots"].get(target) or {}
    prior = str(prior_entry.get("version") or "")
    if not prior:
        raise UpdateError(
            f"no version is recorded for slot {target} — nothing to roll back to "
            "(use the boot menu, or write that slot with an update first)"
        )
    if prior == running and not force:
        raise UpdateError(
            f"slot {target} already holds {prior}, the version you are running; "
            "nothing to roll back to"
        )
    running_entry = registry["slots"].get(active) or {}
    floor = str(running_entry.get("min_rollback_version") or "") or rollback_floor(running)
    if floor and prior < floor and not force:
        raise UpdateError(
            f"rollback to {prior} is below the floor {floor} declared by the "
            f"running release; use --force to roll back anyway"
        )
    esp = ensure_esp_mounted()
    try:
        run([AB_HELPER, "lock-in", target, "--esp", str(esp)])
    finally:
        umount_esp()
    clear_attempt()
    if running != "unknown":
        set_rolled_back(running)
    log(f"rolled back to {prior} (slot {target}); boot_default={target}")
    log(f"update checks will not offer {running} again")
    if reboot:
        progress(phase="writing", message="Rollback finished; the appliance is about to reboot", progress=100)
        log("rebooting now")
        time.sleep(REBOOT_GRACE_S)
        run(["systemctl", "reboot"], check=False)
    return 0


def _read_attempt() -> dict[str, Any]:
    """The arm-time record, else {} — an unreadable one reads as no record."""
    try:
        attempt = json.loads(ATTEMPT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return attempt if isinstance(attempt, dict) else {}


def record_attempt(version: str, target_slot: str) -> None:
    """Arm-time record of the update-failure detector's one unread input.

    Until now the appliance never wrote down which version it *tried* to boot —
    after the reboot it knew only what it was running, so a failed or stuck
    attempt was undetectable. Arm time is the only moment the target is known.
    The control plane compares this against the booted slot (see updates.py).
    """
    payload = {
        "intended_version": version,
        "target_slot": target_slot,
        "attempted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ATTEMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
    ATTEMPT_FILE.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def clear_attempt() -> None:
    ATTEMPT_FILE.unlink(missing_ok=True)


def _read_slot_versions() -> dict[str, Any]:
    """The slot-version registry, else a fresh empty shape.

    Unreadable or malformed JSON reads as no records: the registry is
    bookkeeping, never a reason the box cannot boot. The rollback verb
    re-derives what it can and refuses what it cannot.
    """
    try:
        registry = json.loads(SLOT_VERSIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"slots": {}, "rolled_back_version": None}
    if not isinstance(registry, dict) or not isinstance(registry.get("slots"), dict):
        return {"slots": {}, "rolled_back_version": None}
    registry.setdefault("rolled_back_version", None)
    return registry


def _write_slot_versions(registry: dict[str, Any]) -> None:
    SLOT_VERSIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = SLOT_VERSIONS_FILE.with_name(SLOT_VERSIONS_FILE.name + ".new")
    tmp.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    tmp.replace(SLOT_VERSIONS_FILE)


def record_slot_version(
    slot: str,
    version: str,
    *,
    min_rollback_version: str | None = None,
    written_at: str | None = None,
) -> None:
    """Slot-version bookkeeping for the rollback verb and the boot menu (M8 P6).

    One JSON beside the attempt record answers every rollback question — what
    each slot holds, when it was written, and the rollback floor the running
    release declares. Written by every apply (both slots: the target with the
    applied release, the running one self-healed from /run), by the installer
    for slot a, and read by the rollback verb and the control plane's panel.
    """
    registry = _read_slot_versions()
    entry: dict[str, Any] = {
        "version": version,
        "written_at": written_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if min_rollback_version:
        entry["min_rollback_version"] = min_rollback_version
    registry["slots"][slot] = entry
    _write_slot_versions(registry)


def set_rolled_back(version: str) -> None:
    """Record the version the operator fled to (M8 P6, D30).

    Offers at or below it are suppressed (check, panel) until a strictly
    newer release is published; an explicit ``apply --force`` overrides.
    """
    registry = _read_slot_versions()
    registry["rolled_back_version"] = version
    _write_slot_versions(registry)


def clear_rolled_back() -> None:
    """A successful apply supersedes any rollback: the operator made a choice."""
    registry = _read_slot_versions()
    if registry.get("rolled_back_version"):
        registry["rolled_back_version"] = None
        _write_slot_versions(registry)


def running_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def cmd_check(url: str | None, channel: str | None, *, record: bool) -> int:
    """Running vs latest available; the one definition of "an update exists".

    Both the weekly timer and the panel's *Check now* run this, and the panel
    never fetches the channel itself. ``--record`` persists the finding for the
    control plane's snapshot (the timer passes it; a plain check just prints).
    """
    picked = resolve_channel(channel)
    if url:
        manifest, _dir_url = fetch_manifest(image_base_url(url))
        latest = str(manifest.get("version") or "unknown")
    else:
        # Discovery picks the release this channel resolves to. No candidate
        # (e.g. only prereleases published, stable selected) is *up to date*,
        # not an error (D-C2/Q5) — the same answer as a candidate no newer
        # than the running release.
        resolved = resolve_release_base(picked)
        if resolved is None:
            latest = running_version()
            print(f"no release on channel '{picked}'; treating it as up to date")
        else:
            manifest, _dir_url = fetch_manifest(resolved[0])
            latest = str(manifest.get("version") or "unknown")
    running = running_version()
    # Same rule as the panel: a stale record must not offer a downgrade —
    # which is also the whole channel-switch story (D-C2). A rollback marker
    # suppresses offers at or below the version the operator fled to, until a
    # strictly newer release is published (M8 P6).
    rolled_back = str(_read_slot_versions().get("rolled_back_version") or "")
    suppressed = bool(rolled_back) and latest <= rolled_back
    available = "true" if (latest != running and latest > running and not suppressed) else "false"
    print(f"running={running}")
    print(f"latest={latest}")
    print(f"channel={picked}")
    print(f"rolled_back={rolled_back}")
    print(f"update_available={available}")
    if record:
        payload = {
            "latest": latest,
            "running": running,
            "channel": picked,
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        LATEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        LATEST_FILE.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lemonseed-update",
        description="Lemonseed A/B image updater",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("status", help="Show current and pending A/B slots")

    apply_p = sub.add_parser("apply", help="Write inactive slot and set boot_once")
    apply_p.add_argument("--url", help="HTTP directory with manifest.json + artifacts")
    apply_p.add_argument("--dir", dest="directory", help="Local artifact directory")
    apply_p.add_argument(
        "--channel",
        choices=CHANNELS,
        help="Release channel to resolve (default: the configured one, else stable)",
    )
    apply_p.add_argument("--reboot", action="store_true", help="Reboot after arming boot_once")
    apply_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite inactive slot even if a pending update exists",
    )

    sub.add_parser("cancel", help="Clear pending boot_once / boot_attempted")

    rollback_p = sub.add_parser(
        "rollback",
        help="Boot the previous slot (N-1): flip the default, clear the attempt",
    )
    rollback_p.add_argument("--reboot", action="store_true", help="Reboot into the rolled-back slot")
    rollback_p.add_argument(
        "--force",
        action="store_true",
        help="Override the eligibility checks (dirty state, declared floor)",
    )

    check_p = sub.add_parser("check", help="Report running vs latest available version")
    check_p.add_argument("--url", help="HTTP directory with manifest.json")
    check_p.add_argument(
        "--channel",
        choices=CHANNELS,
        help="Release channel to resolve (default: the configured one, else stable)",
    )
    check_p.add_argument(
        "--record",
        action="store_true",
        help="Persist the result for the control plane (weekly timer, Check now)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare flags without a subcommand imply "apply".
    if argv and argv[0] not in ("status", "apply", "cancel", "check", "rollback", "-h", "--help"):
        argv = ["apply", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "status" or args.command is None:
            return cmd_status()
        if args.command == "cancel":
            return cmd_cancel()
        if args.command == "rollback":
            return cmd_rollback(reboot=bool(args.reboot), force=bool(args.force))
        if args.command == "check":
            return cmd_check(args.url, getattr(args, "channel", None), record=args.record)
        if args.command == "apply":
            return cmd_apply(
                args.url,
                args.directory,
                channel=getattr(args, "channel", None),
                reboot=bool(args.reboot),
                force=bool(args.force),
            )
        parser.print_help()
        return 2
    except (UpdateError, ArtifactError) as exc:
        print(f"lemonseed-update: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
