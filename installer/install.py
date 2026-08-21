#!/usr/bin/env python3
"""Lemonseed install pipeline: download → wipe/repart → write root-a → seed ESP.

Designed to run as root on the live installer. Progress is reported through a
callback so the web UI can drive a progress bar.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from disks import list_candidate_disks

ProgressFn = Callable[..., None]

IMAGE_BASE_DEFAULT = "https://lemonseed.ai/releases/stable/"
USER_AGENT = "lemonseed-installer/0.1"
DOWNLOAD_DIR = Path(os.environ.get("LEMONSEED_DOWNLOAD_DIR", "/var/lib/lemonseed/download"))
ESP_MOUNT = Path("/run/lemonseed-install-esp")
LEMONSEED_ROOT = Path(os.environ.get("LEMONSEED_ROOT", "/etc/lemonseed"))
REPART_SCRIPT = LEMONSEED_ROOT / "disk-repart.sh"
RELEASE_SCRIPT = LEMONSEED_ROOT / "disk-release.sh"
GRUB_CFG = LEMONSEED_ROOT / "grub.cfg"
AB_HELPER = LEMONSEED_ROOT / "lemonseed-ab.sh"


class InstallError(Exception):
    pass


def _progress(cb: ProgressFn | None, **kwargs: Any) -> None:
    if cb:
        cb(**kwargs)


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=True,
        text=True,
        stdout=kwargs.pop("stdout", subprocess.PIPE),
        stderr=kwargs.pop("stderr", subprocess.STDOUT),
        **kwargs,
    )


def _cmdline_val(key: str) -> str | None:
    path = Path("/proc/cmdline")
    if not path.is_file():
        return None
    prefix = key + "="
    for arg in path.read_text(encoding="utf-8").split():
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return None


def artifact_dir() -> Path | None:
    raw = os.environ.get("LEMONSEED_ARTIFACT_DIR") or _cmdline_val("lemonseed.artifacts")
    if not raw:
        return None
    p = Path(raw)
    return p if p.is_dir() else None


def image_base_url() -> str:
    raw = (
        _cmdline_val("lemonseed.image")
        or os.environ.get("LEMONSEED_IMAGE_BASE_URL")
        or IMAGE_BASE_DEFAULT
    )
    return raw.rstrip("/") + "/"


def _eligible(disk_path: str) -> dict[str, Any]:
    disks = list_candidate_disks()
    match = next((d for d in disks if d["path"] == disk_path), None)
    if match is None or not match["eligible"]:
        raise InstallError(f"Selected disk is not eligible: {disk_path}")
    return match


def parts_on_disk(disk: str) -> dict[str, str]:
    out = subprocess.check_output(["lsblk", "-nr", "-o", "PARTLABEL,PATH", disk], text=True)
    found: dict[str, str] = {}
    for line in out.splitlines():
        bits = line.split()
        if len(bits) < 2:
            continue
        found[bits[0]] = bits[1]
    return found


def wait_part(disk: str, label: str, timeout: float = 30.0) -> str:
    deadline = time.time() + timeout
    last: dict[str, str] = {}
    while time.time() < deadline:
        subprocess.run(["blockdev", "--rereadpt", disk], check=False, capture_output=True)
        subprocess.run(["udevadm", "settle"], check=False, capture_output=True)
        last = parts_on_disk(disk)
        path = last.get(label)
        if path and Path(path).exists():
            return path
        time.sleep(0.4)
    raise InstallError(f"Partition {label} did not appear on {disk} (have {sorted(last)})")


def partition_number(part: str) -> int:
    name = Path(part).name
    sysf = Path("/sys/class/block") / name / "partition"
    try:
        return int(sysf.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as exc:
        raise InstallError(f"Could not determine partition number for {part}") from exc


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _urlopen(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_manifest(base: str) -> tuple[dict[str, Any], str]:
    """Return (manifest, directory URL that contains the artifacts)."""
    candidates = [
        base + "manifest.json",
        base + "latest/manifest.json",
    ]
    errors: list[str] = []
    for url in candidates:
        try:
            with _urlopen(url) as resp:
                body = resp.read()
                manifest = json.loads(body.decode("utf-8"))
                dir_url = url.rsplit("/", 1)[0] + "/"
                return manifest, dir_url
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append(f"{url}: {exc}")
    raise InstallError(
        "Could not download image manifest from lemonseed.ai. Tried:\n  " + "\n  ".join(errors)
    )


def download_file(
    url: str,
    dest: Path,
    expected_sha: str | None,
    progress: ProgressFn | None,
    pct_lo: int,
    pct_hi: int,
    label: str,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    got = 0
    try:
        with _urlopen(url, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            with dest.open("wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    hasher.update(chunk)
                    got += len(chunk)
                    if total:
                        frac = min(got / total, 1.0)
                        pct = int(pct_lo + (pct_hi - pct_lo) * frac)
                        _progress(
                            progress,
                            phase="download",
                            message=f"Downloading {label} ({got // (1024 * 1024)} MiB)",
                            progress=pct,
                        )
                out.flush()
                os.fsync(out.fileno())
    except (urllib.error.URLError, TimeoutError) as exc:
        raise InstallError(f"Download failed for {label}: {exc}") from exc

    digest = hasher.hexdigest()
    if expected_sha and digest.lower() != expected_sha.lower():
        raise InstallError(
            f"Checksum mismatch for {label}: got {digest}, expected {expected_sha}"
        )


def copy_local(src: Path, dest: Path, expected_sha: str | None, label: str) -> None:
    if not src.is_file():
        raise InstallError(f"Local artifact missing: {src} ({label})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    if expected_sha:
        digest = sha256_file(dest)
        if digest.lower() != expected_sha.lower():
            raise InstallError(
                f"Checksum mismatch for {label}: got {digest}, expected {expected_sha}"
            )


def load_local_manifest(directory: Path) -> dict[str, Any]:
    man = directory / "manifest.json"
    if man.is_file():
        return json.loads(man.read_text(encoding="utf-8"))
    return {
        "version": "local",
        "artifacts": {
            "vmlinuz": {"path": "vmlinuz"},
            "root.squashfs": {"path": "root.squashfs"},
            "initrd.img": {"path": "initrd.img"},
        },
    }


def artifact_relpath(manifest: dict[str, Any], key: str) -> tuple[str, str | None]:
    arts = manifest.get("artifacts") or {}
    entry = arts.get(key) or {}
    path = entry.get("path") or key
    sha = entry.get("sha256")
    return path, sha


def acquire_artifacts(progress: ProgressFn | None) -> tuple[dict[str, Path], str]:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    local = artifact_dir()
    if local:
        _progress(
            progress,
            phase="download",
            message=f"Using local artifacts in {local}",
            progress=8,
        )
        manifest = load_local_manifest(local)
        files: dict[str, Path] = {}
        for key in ("vmlinuz", "root.squashfs", "initrd.img"):
            rel, sha = artifact_relpath(manifest, key)
            dest = DOWNLOAD_DIR / Path(rel).name
            copy_local(local / rel, dest, sha, key)
            files[key] = dest
        _progress(
            progress,
            phase="download",
            message=f"Artifacts ready (version {manifest.get('version', 'unknown')})",
            progress=45,
            version=manifest.get("version"),
        )
        return files, str(manifest.get("version") or "local")

    base = image_base_url()
    _progress(
        progress,
        phase="download",
        message=f"Fetching manifest from {base}",
        progress=6,
    )
    manifest, dir_url = fetch_manifest(base)
    version = manifest.get("version", "unknown")
    files = {}
    ranges = {
        "vmlinuz": (8, 14),
        "initrd.img": (14, 22),
        "root.squashfs": (22, 45),
    }
    for key, (lo, hi) in ranges.items():
        rel, sha = artifact_relpath(manifest, key)
        dest = DOWNLOAD_DIR / Path(rel).name
        download_file(dir_url + rel, dest, sha, progress, lo, hi, key)
        files[key] = dest
    _progress(
        progress,
        phase="download",
        message=f"Downloaded Lemonseed {version}",
        progress=45,
        version=version,
    )
    return files, str(version)


def write_raw(src: Path, dest: str, progress: ProgressFn | None, pct_lo: int, pct_hi: int) -> None:
    total = src.stat().st_size
    dest_size = int(
        subprocess.check_output(["blockdev", "--getsize64", dest], text=True).strip()
    )
    if dest_size < total:
        raise InstallError(
            f"{dest} is {dest_size} bytes but image is {total} bytes (too small)"
        )
    got = 0
    with src.open("rb") as inf, open(dest, "wb") as outf:
        while True:
            chunk = inf.read(4 * 1024 * 1024)
            if not chunk:
                break
            outf.write(chunk)
            got += len(chunk)
            frac = min(got / total, 1.0) if total else 1.0
            _progress(
                progress,
                phase="write",
                message=f"Writing system image ({got // (1024 * 1024)} / {total // (1024 * 1024)} MiB)",
                progress=int(pct_lo + (pct_hi - pct_lo) * frac),
            )
        outf.flush()
        os.fsync(outf.fileno())


def partition_disk(
    disk: str, progress: ProgressFn | None, preserve_user_data: bool = False
) -> None:
    if not REPART_SCRIPT.is_file():
        raise InstallError(f"Missing {REPART_SCRIPT}")
    env = os.environ.copy()
    env.setdefault("LEMONSEED_ROOT", str(LEMONSEED_ROOT))
    env.setdefault("LEMONSEED_REPART_DIR", "/var/lib/lemonseed/repart.d")
    if preserve_user_data:
        _progress(
            progress,
            phase="partition",
            message="Verifying and preserving existing Lemonseed user data",
            progress=48,
        )
        details = _eligible(disk).get("preservable_user_data")
        if not details:
            raise InstallError("Existing Lemonseed user data could not be verified; refusing preserve mode")
        required = {"ESP", "root-a", "root-b"}
        missing = required.difference(parts_on_disk(disk))
        if missing:
            raise InstallError(
                "Existing disk layout is incompatible with preservation (missing "
                + ", ".join(sorted(missing))
                + ")"
            )
        if not RELEASE_SCRIPT.is_file():
            raise InstallError(f"Missing {RELEASE_SCRIPT}")
        try:
            _run(["bash", str(RELEASE_SCRIPT), "--keep-signatures", disk], env=env)
        except subprocess.CalledProcessError as exc:
            raise InstallError(f"Could not release preserved storage:\n{exc.stdout}") from exc
        # Mark the inactive raw-SquashFS slot unbootable without touching data.
        root_b = wait_part(disk, "root-b")
        subprocess.run(["wipefs", "-a", root_b], check=False, capture_output=True)
    else:
        _progress(
            progress,
            phase="partition",
            message="Releasing leftover volumes and creating partitions",
            progress=48,
        )
        try:
            _run(["bash", str(REPART_SCRIPT), "--wipe", disk], env=env)
        except subprocess.CalledProcessError as exc:
            raise InstallError(f"Partitioning failed:\n{exc.stdout}") from exc
    wait_part(disk, "ESP")
    wait_part(disk, "root-a")
    wait_part(disk, "lemonseed-user-data")
    _progress(progress, phase="partition", message="Partitions ready", progress=55)


def seed_esp(disk: str, files: dict[str, Path], progress: ProgressFn | None) -> None:
    esp = wait_part(disk, "ESP")
    ESP_MOUNT.mkdir(parents=True, exist_ok=True)
    mounted = False
    try:
        # A reinstall owns ESP/boot artifacts; this deliberately leaves the
        # separately verified user-data partition untouched.
        _run(["mkfs.fat", "-F", "32", "-n", "ESP", esp])
        _run(["mount", "-t", "vfat", "-o", "rw,umask=0077", esp, str(ESP_MOUNT)])
        mounted = True

        _progress(progress, phase="esp", message="Installing GRUB", progress=86)
        try:
            _run(
                [
                    "grub-install",
                    "--target=x86_64-efi",
                    f"--efi-directory={ESP_MOUNT}",
                    f"--boot-directory={ESP_MOUNT}",
                    "--bootloader-id=lemonseed",
                    "--no-nvram",
                    "--recheck",
                ]
            )
        except subprocess.CalledProcessError as exc:
            raise InstallError(f"grub-install failed:\n{exc.stdout}") from exc

        grub_dir = ESP_MOUNT / "grub"
        grub_dir.mkdir(parents=True, exist_ok=True)
        if not GRUB_CFG.is_file():
            raise InstallError(f"Missing {GRUB_CFG}")
        shutil.copy2(GRUB_CFG, grub_dir / "grub.cfg")

        efi_ls = ESP_MOUNT / "EFI" / "lemonseed"
        efi_ls.mkdir(parents=True, exist_ok=True)
        shutil.copy2(files["vmlinuz"], efi_ls / "vmlinuz-a")
        shutil.copy2(files["initrd.img"], efi_ls / "initrd-a.img")
        # Slot b is filled by the updater; first install only seeds A.

        efi_bin = None
        for cand in (efi_ls / "grubx64.efi", efi_ls / "grub.efi"):
            if cand.is_file():
                efi_bin = cand
                break
        if efi_bin is None:
            raise InstallError("GRUB EFI binary missing after grub-install")

        efi_boot = ESP_MOUNT / "EFI" / "BOOT"
        efi_boot.mkdir(parents=True, exist_ok=True)
        shutil.copy2(efi_bin, efi_boot / "BOOTX64.EFI")

        if AB_HELPER.is_file():
            _run(["bash", str(AB_HELPER), "init", "--esp", str(ESP_MOUNT)])

        partn = partition_number(esp)
        loader = "\\EFI\\lemonseed\\" + efi_bin.name
        try:
            efi_root = Path("/sys/firmware/efi")
            efi_vars = efi_root / "efivars"
            if efi_root.is_dir():
                efi_vars.mkdir(parents=True, exist_ok=True)
                if not os.path.ismount(str(efi_vars)):
                    subprocess.run(
                        ["mount", "-t", "efivarfs", "efivarfs", str(efi_vars)],
                        check=False,
                        capture_output=True,
                    )
            listing = subprocess.check_output(["efibootmgr"], text=True)
            for line in listing.splitlines():
                if "Lemonseed" in line and line.startswith("Boot"):
                    bootnum = line[4:8]
                    subprocess.run(
                        ["efibootmgr", "-b", bootnum, "-B"],
                        check=False,
                        capture_output=True,
                    )
            _run(
                [
                    "efibootmgr",
                    "--create",
                    "--disk",
                    disk,
                    "--part",
                    str(partn),
                    "--loader",
                    loader,
                    "--label",
                    "Lemonseed",
                ]
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            # Removable EFI path (BOOTX64.EFI) is already in place.
            _progress(
                progress,
                phase="esp",
                message=f"EFI NVRAM entry skipped ({exc}); removable boot path installed",
                progress=96,
            )
        else:
            _progress(
                progress,
                phase="esp",
                message="Bootloader installed",
                progress=98,
            )
    finally:
        if mounted:
            subprocess.run(["umount", str(ESP_MOUNT)], check=False)


def run_install(
    disk_path: str, progress: ProgressFn | None = None, preserve_user_data: bool = False
) -> None:
    disk = _eligible(disk_path)
    _progress(
        progress,
        phase="validate",
        message=f"Installing to {disk['path']}",
        progress=4,
        disk=disk,
        error=None,
        can_reboot=False,
    )
    files, version = acquire_artifacts(progress)
    if preserve_user_data and not disk.get("preservable_user_data"):
        raise InstallError("Preserve user data was requested for an unverified disk")
    partition_disk(disk_path, progress, preserve_user_data=preserve_user_data)
    root_a = wait_part(disk_path, "root-a")
    _progress(progress, phase="write", message="Writing system image to root-a", progress=58)
    write_raw(files["root.squashfs"], root_a, progress, 58, 84)
    _progress(progress, phase="write", message="System image written", progress=85)
    seed_esp(disk_path, files, progress)
    # Flush target disk before inviting media removal — the live installer
    # root is still on that CD/USB and will vanish when it is ejected.
    _progress(
        progress,
        phase="await_usb",
        message="Flushing disks…",
        progress=99,
    )
    subprocess.run(["sync"], check=False)
    time.sleep(1)
    subprocess.run(["sync"], check=False)
    _progress(
        progress,
        phase="await_usb",
        message=(
            f"Lemonseed {version} is installed. "
            "Remove the installer CD/USB, then click Reboot "
            "(immediate restart — do not wait for a clean shutdown)."
        ),
        progress=100,
        can_reboot=True,
        version=version,
    )
