#!/usr/bin/env python3
"""Lemonseed A/B image updater.

Writes verified artifacts into the inactive root slot + ESP kernels, then sets
GRUB boot_once so the next reboot tries the new slot. Lock-in promotes it only
after a healthy boot. User-data (/etc, /var) is never touched.

Usage:
  lemonseed-update status
  lemonseed-update [--url URL | --dir DIR] [--reboot] [--force]
  lemonseed-update apply [--url URL | --dir DIR] [--reboot] [--force]
  lemonseed-update cancel
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from lemonseed_artifacts import (
    ArtifactError,
    acquire_artifacts,
    sha256_device,
    sha256_file,
    write_raw,
)

DOWNLOAD_DIR = Path(os.environ.get("LEMONSEED_DOWNLOAD_DIR", "/var/lib/lemonseed/download"))
ESP_MOUNT = Path("/run/lemonseed-esp")
AB_HELPER = os.environ.get("LEMONSEED_AB", "lemonseed-ab")


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
    force: bool,
) -> None:
    refuse_dirty_state(force=force)
    active = current_slot()
    if target_slot == active:
        raise UpdateError(f"Refusing to overwrite active slot {active}")

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
    for label in ("root-a", "root-b", "ESP"):
        path = Path(f"/dev/disk/by-partlabel/{label}")
        print(f"part_{label}={'present' if path.exists() else 'missing'}")
    return 0


def cmd_apply(url: str | None, directory: str | None, *, reboot: bool, force: bool) -> int:
    require_root()
    if url and directory:
        raise UpdateError("use either --url or --dir, not both")
    active = current_slot()
    target = inactive_slot(active)
    log(f"active slot {active}; writing inactive slot {target}")
    files, version, _manifest = acquire_artifacts(
        progress=progress,
        download_dir=DOWNLOAD_DIR,
        url=url,
        directory=directory,
    )
    log(f"artifacts ready (version {version})")
    write_inactive_slot(files, target, force=force)
    log(f"pending boot_once={target}; reboot to try Lemonseed {version}")
    if reboot:
        log("rebooting now")
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
    apply_p.add_argument("--reboot", action="store_true", help="Reboot after arming boot_once")
    apply_p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite inactive slot even if a pending update exists",
    )

    sub.add_parser("cancel", help="Clear pending boot_once / boot_attempted")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare flags without a subcommand imply "apply".
    if argv and argv[0] not in ("status", "apply", "cancel", "-h", "--help"):
        argv = ["apply", *argv]

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "status" or args.command is None:
            return cmd_status()
        if args.command == "cancel":
            return cmd_cancel()
        if args.command == "apply":
            return cmd_apply(
                args.url,
                args.directory,
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
