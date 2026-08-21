#!/usr/bin/env python3
"""Eligible install-disk discovery for the Lemonseed USB installer.

Rules (DESIGN.md M4):
  - Prefer internal SATA/NVMe disks
  - Disallow USB-attached and eMMC
  - Never target the live installer medium
  - Flag existing OS / partition tables for a wipe warning
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any


SYS_BLOCK = Path("/sys/block")
LVM_PARTTYPE = "e6d6d379-f507-44c2-a33c-2081b433ad87"
STORAGE_PARTLABEL = "lemonseed-user-data"
STORAGE_TAG = "lemonseed.storage=v1"
VG_NAME = "appliance"
LV_NAME = "user-data"
STORAGE_IDENTITY = ".lemonseed-storage.json"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _lsblk_json() -> dict[str, Any]:
    out = subprocess.check_output(
        [
            "lsblk",
            "-J",
            "-b",
            "-o",
            "NAME,PATH,SIZE,TYPE,TRAN,RM,HOTPLUG,MODEL,SERIAL,VENDOR,FSTYPE,LABEL,PARTLABEL,PARTTYPE,MOUNTPOINT,PKNAME",
        ],
        text=True,
    )
    return json.loads(out)


def _is_emmc(name: str, tran: str) -> bool:
    # mmcblk* without boot partitions; TRAN may be "mmc"
    if name.startswith("mmcblk") and "boot" not in name:
        return True
    return tran.lower() == "mmc"


def _is_usb(tran: str, device: Path) -> bool:
    if tran.lower() == "usb":
        return True
    # Walk device symlink for a usb component (covers odd stacks).
    try:
        resolved = (device / "device").resolve()
        return "usb" in resolved.parts
    except OSError:
        return False


def _installer_backing_devices() -> set[str]:
    """Block devices hosting the live ISO / rootfs (must not be wiped)."""
    banned: set[str] = set()
    for mount in ("/", "/iso", "/dev/.iso"):
        try:
            st = os.stat(mount)
        except OSError:
            continue
        # Major/minor → find matching /sys/dev/block
        maj, min_ = os.major(st.st_dev), os.minor(st.st_dev)
        link = Path(f"/sys/dev/block/{maj}:{min_}")
        if not link.exists():
            continue
        try:
            target = link.resolve()
        except OSError:
            continue
        # .../block/sda/sda1 or .../block/sda
        parts = target.parts
        if "block" in parts:
            i = parts.index("block")
            if i + 1 < len(parts):
                banned.add(parts[i + 1])
    # Also findiso / overlay conventions
    for line in _read(Path("/proc/mounts")).splitlines():
        src = line.split()[0] if line else ""
        if src.startswith("/dev/"):
            base = src.rsplit("/", 1)[-1]
            # strip partition suffix loosely
            for prefix in (base, base.rstrip("0123456789"), base.split("p")[0]):
                if (SYS_BLOCK / prefix).exists():
                    banned.add(prefix)
    return banned


def _child_hints(node: dict[str, Any]) -> dict[str, Any]:
    """Summarize partitions for wipe-warning UI."""
    labels: list[str] = []
    fstypes: list[str] = []
    partlabels: list[str] = []
    has_esp = False
    has_rootish = False
    existing_os = False

    def walk(n: dict[str, Any]) -> None:
        nonlocal has_esp, has_rootish, existing_os
        for key, bucket in (
            ("label", labels),
            ("fstype", fstypes),
            ("partlabel", partlabels),
        ):
            val = (n.get(key) or "").strip()
            if val and val not in bucket:
                bucket.append(val)

        pt = (n.get("parttype") or "").lower()
        pl = (n.get("partlabel") or "").lower()
        fs = (n.get("fstype") or "").lower()
        label = (n.get("label") or "").lower()

        if pt.startswith("c12a7328") or pl in ("esp", "efi") or fs == "vfat" and "efi" in pl:
            has_esp = True
        if pl.startswith("root") or fs in ("ext4", "btrfs", "xfs", "squashfs") or label in (
            "root",
            "nixos",
            "ubuntu",
            "fedora",
            "arch",
        ):
            has_rootish = True
        if fs in ("ext4", "btrfs", "xfs", "ntfs", "lvm2_member") or has_esp:
            existing_os = True

        for ch in n.get("children") or []:
            walk(ch)

    walk(node)
    if has_esp and has_rootish:
        existing_os = True

    return {
        "labels": labels,
        "fstypes": fstypes,
        "partlabels": partlabels,
        "has_esp": has_esp,
        "existing_os": existing_os,
        "has_partition_table": bool(node.get("children")),
    }


def _walk_partitions(node: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(current: dict[str, Any]) -> None:
        if current.get("type") == "part":
            found.append(current)
        for child in current.get("children") or []:
            walk(child)

    walk(node)
    return found


def _run_text(args: list[str]) -> str:
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _preservable_user_data(node: dict[str, Any]) -> dict[str, Any] | None:
    """Return a privacy-safe summary only for positively identified M4.1+ storage.

    A bare LVM PV or generic label must never expose the preserve switch.
    """
    for part in _walk_partitions(node):
        part_path = part.get("path") or ""
        part_label = (part.get("partlabel") or "").strip()
        part_type = (part.get("parttype") or "").lower()
        if (
            not part_path
            or part_label != STORAGE_PARTLABEL
            or not part_type.startswith(LVM_PARTTYPE)
        ):
            continue

        pv = _run_text(
            ["pvs", "--noheadings", "--separator=|", "-o", "pv_name,vg_name,vg_tags", part_path]
        )
        fields = [field.strip() for field in pv.split("|")]
        if len(fields) != 3 or Path(fields[0]).resolve() != Path(part_path).resolve():
            continue
        if fields[1] != VG_NAME or STORAGE_TAG not in fields[2].split(","):
            continue

        # Activating the verified target briefly lets us validate the LV,
        # filesystem and top-level btrfs identity. Always deactivate again.
        subprocess.run(["vgchange", "-ay", VG_NAME], check=False, capture_output=True)
        try:
            lv_path = _run_text(["lvs", "--noheadings", "-o", "lv_path", f"{VG_NAME}/{LV_NAME}"])
            if not lv_path:
                continue
            label = _run_text(["blkid", "-o", "value", "-s", "LABEL", lv_path])
            if label != STORAGE_PARTLABEL:
                continue
            with tempfile.TemporaryDirectory(prefix="lemonseed-inspect-") as mountpoint:
                mounted = subprocess.run(
                    ["mount", "-t", "btrfs", "-o", "ro,subvolid=5", lv_path, mountpoint],
                    check=False,
                    capture_output=True,
                ).returncode == 0
                if not mounted:
                    continue
                try:
                    root = Path(mountpoint)
                    if not (root / "@etc").is_dir() or not (root / "@var").is_dir():
                        continue
                    identity_path = root / STORAGE_IDENTITY
                    try:
                        identity = json.loads(identity_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        continue
                finally:
                    subprocess.run(["umount", mountpoint], check=False, capture_output=True)

            if (
                identity.get("format") == "lemonseed-storage-v1"
                and identity.get("vg") == VG_NAME
                and identity.get("lv") == LV_NAME
            ):
                return {
                    "storage_uuid": identity.get("storage_uuid"),
                    "size_bytes": int(part.get("size") or 0),
                }
        finally:
            subprocess.run(["vgchange", "-an", VG_NAME], check=False, capture_output=True)
    return None


def list_candidate_disks() -> list[dict[str, Any]]:
    banned = _installer_backing_devices()
    data = _lsblk_json()
    disks: list[dict[str, Any]] = []

    for node in data.get("blockdevices") or []:
        if (node.get("type") or "") != "disk":
            continue
        name = node.get("name") or ""
        path = node.get("path") or f"/dev/{name}"
        tran = (node.get("tran") or "").strip()
        rm = bool(node.get("rm"))
        size = int(node.get("size") or 0)
        sysdev = SYS_BLOCK / name

        reasons: list[str] = []
        eligible = True

        if name in banned or any(name.startswith(b) for b in banned):
            eligible = False
            reasons.append("installer-medium")
        if _is_usb(tran, sysdev) or rm:
            eligible = False
            reasons.append("usb-or-removable")
        if _is_emmc(name, tran):
            eligible = False
            reasons.append("emmc")
        if size < 16 * 1024**3:
            # Soft floor: ESP 1G + 2×4G roots + user-data headroom
            eligible = False
            reasons.append("too-small")

        hints = _child_hints(node)
        preserved = _preservable_user_data(node) if eligible else None
        disks.append(
            {
                "name": name,
                "path": path,
                "size_bytes": size,
                "model": (node.get("model") or "").strip() or None,
                "serial": (node.get("serial") or "").strip() or None,
                "tran": tran or None,
                "eligible": eligible,
                "ineligible_reasons": reasons,
                "wipe_warning": hints["existing_os"] or hints["has_partition_table"],
                "existing_os": hints["existing_os"],
                "partition_summary": hints,
                "preservable_user_data": preserved,
            }
        )

    disks.sort(key=lambda d: (not d["eligible"], -d["size_bytes"], d["name"]))
    return disks


def pick_default(disks: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    disks = disks if disks is not None else list_candidate_disks()
    for d in disks:
        if d["eligible"]:
            return d
    return None


def main() -> None:
    disks = list_candidate_disks()
    payload = {
        "disks": disks,
        "default": pick_default(disks),
    }
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
