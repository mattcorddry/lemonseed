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
import syslog
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# The shared verifier ships next to the vendored pipeline on the ISO: web.nix
# puts both the installer dir and the scripts dir on PYTHONPATH, so the
# installer and the in-place updater never carry two verifiers (SIGNING.md §5).
import lemonseed_artifacts
import lemonseed_verify
from disks import list_candidate_disks
from lemonseed_verify import VerificationError
from preflight import require_host_ready

ProgressFn = Callable[..., None]

# Internal-network channel note is gone: the release channel is GitHub
# releases, resolved by the shared module (lemonseed_artifacts.resolve_channel /
# resolve_release_base). The installer carries no channel constant of its own —
# one definition, shared with the updater (M8 P3).
USER_AGENT = "lemonseed-installer/0.1"
DOWNLOAD_DIR = Path(os.environ.get("LEMONSEED_DOWNLOAD_DIR", "/var/lib/lemonseed/download"))
INSTALL_LOG = Path("/var/lib/lemonseed/installer.log")


def _log(level: int, msg: str) -> None:
    """Log to syslog (journal) AND append to a persistent file.

    The installer's journal otherwise only shows HTTP access logs — the
    install thread runs in-process and its output went nowhere persistent.
    The file survives on the live rootfs and is copied to the target's
    user-data at the end of the install (see run_install), so the install
    can be inspected post-reboot.
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"{stamp} {msg}"
    try:
        syslog.syslog(level, line)
    except OSError:
        pass
    try:
        INSTALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with INSTALL_LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
ESP_MOUNT = Path("/run/lemonseed-install-esp")
USER_DATA_MOUNT = Path("/run/lemonseed-install-user-data")
# The user-data partition is only an LVM PV: the btrfs volume holding the
# @etc/@var subvolumes is the appliance/user-data LV
# (scripts/appliance-storage-provision.sh). "user-data" is kept as a fallback
# key for volumes probed before the label was settled.
USER_DATA_PARTLABELS = ("lemonseed-user-data", "user-data")
USER_DATA_VG = "appliance"
USER_DATA_LV = "user-data"
# Paths inside a mounted user-data volume, i.e. the appliance's
# /var/lib/lemonseed/reset-admin and /var/lib/lemonseed/admins.json. Their
# readers/writers on the box are auth.RESET_MARKER_FILE and auth.ADMINS_FILE.
RESET_MARKER_REL = Path("@var/lib/lemonseed/reset-admin")
ADMIN_ROSTER_REL = Path("@var/lib/lemonseed/admins.json")
# Same volume, same rule (M8 P3): the updater wrapper and the settings
# materialization read /var/lib/lemonseed/channel.
CHANNEL_FILE_REL = Path("@var/lib/lemonseed/channel")
LEMONSEED_ROOT = Path(os.environ.get("LEMONSEED_ROOT", "/etc/lemonseed"))
REPART_SCRIPT = LEMONSEED_ROOT / "disk-repart.sh"
RELEASE_SCRIPT = LEMONSEED_ROOT / "disk-release.sh"
GRUB_CFG = LEMONSEED_ROOT / "grub.cfg"
AB_HELPER = LEMONSEED_ROOT / "lemonseed-ab.sh"
AMDGPU_KERNEL_PARAMS = LEMONSEED_ROOT / "amdgpu-kernel-params.sh"


class InstallError(Exception):
    pass


class ReleaseSourceError(InstallError):
    """The release set could not be acquired, before anything touched the disk.

    ``str()`` is the product sentence the installer UI shows every user; the
    technical reason (DNS, HTTP status, signature refusal, …) rides in
    ``detail`` for the UI's debug disclosure and the install log. Retry and
    reboot are always safe: acquire runs entirely before partitioning.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            "Unable to find the release files for Lemonseed AI. "
            "No changes were made to your system."
        )
        self.detail = detail


def _progress(cb: ProgressFn | None, **kwargs: Any) -> None:
    if cb:
        cb(**kwargs)


def _run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output and mirroring it to the install log.

    Subprocess output (repart, grub-install, amdgpu-kernel-params, …) is
    what we need to diagnose install failures; without this it was only
    visible in the web progress message (last line) or lost entirely.
    """
    result = subprocess.run(
        cmd,
        check=True,
        text=True,
        stdout=kwargs.pop("stdout", subprocess.PIPE),
        stderr=kwargs.pop("stderr", subprocess.STDOUT),
        **kwargs,
    )
    for line in (result.stdout or "").splitlines():
        _log(syslog.LOG_INFO, f"$ {' '.join(cmd)}: {line}")
    return result


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


def image_base_url() -> str | None:
    raw = _cmdline_val("lemonseed.image") or os.environ.get("LEMONSEED_IMAGE_BASE_URL")
    return raw.rstrip("/") + "/" if raw else None


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


def _first_line(text: str | None, fallback: str) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return fallback


def user_data_device(disk: str) -> str | None:
    """Return the mountable node holding ``disk``'s Lemonseed user-data.

    The partition alone is not mountable on a provisioned box: it is an LVM PV
    and the btrfs volume lives on the ``appliance/user-data`` LV, so the VG is
    activated and the LV preferred. A volume that was never provisioned has no
    LV, and the bare partition is returned so the caller's mount fails with a
    real message instead of a mystery.
    """
    parts = parts_on_disk(disk)
    part = None
    for label in USER_DATA_PARTLABELS:
        if parts.get(label):
            part = parts[label]
            break
    if not part:
        return None
    subprocess.run(["vgchange", "-ay", USER_DATA_VG], check=False, capture_output=True)
    for lv in (f"/dev/{USER_DATA_VG}/{USER_DATA_LV}", f"/dev/mapper/{USER_DATA_VG}-{USER_DATA_LV}"):
        if Path(lv).exists():
            return lv
    return part


@contextmanager
def mount_user_data(disk: str, read_only: bool = False) -> Iterator[Path]:
    """Mount ``disk``'s user-data volume at USER_DATA_MOUNT and yield its root.

    An existing mount on that path is reused rather than stacked, matching the
    install-log copy in run_install. Read-only probes hand the volume back when
    they finish so nothing is left busy before partitioning runs.
    """
    device = user_data_device(disk)
    if not device:
        raise InstallError(f"No Lemonseed user-data partition found on {disk}")
    USER_DATA_MOUNT.mkdir(parents=True, exist_ok=True)
    ours = not os.path.ismount(str(USER_DATA_MOUNT))
    mode = "ro" if read_only else "rw"
    if ours:
        proc = subprocess.run(
            ["mount", "-t", "btrfs", "-o", f"{mode},subvolid=5", device, str(USER_DATA_MOUNT)],
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise InstallError(
                f"Could not mount {device} on {USER_DATA_MOUNT}: "
                f"{_first_line(proc.stderr or proc.stdout, 'mount failed')}"
            )
    try:
        yield USER_DATA_MOUNT
    finally:
        if ours:
            subprocess.run(["umount", str(USER_DATA_MOUNT)], check=False)
            if read_only:
                subprocess.run(
                    ["vgchange", "-an", USER_DATA_VG], check=False, capture_output=True
                )


def apply_reset_marker(disk: str, requested: bool) -> None:
    """State the admin-reset intent on the target's user-data (D-27).

    ``/var/lib/lemonseed/reset-admin`` is the file the control plane reads on
    boot (``auth.RESET_MARKER_FILE``): while it exists the portal reopens
    ``/setup``, and the next admin enrollment revokes admin from the accounts it
    names. Creating it is deliberately **not** best-effort — a write that
    quietly failed would leave a household believing a lost password had been
    reset when it had not. Removing a stale marker stays best-effort, because
    nothing was promised to the user in that direction.
    """
    if not requested:
        try:
            with mount_user_data(disk) as root:
                marker = root / RESET_MARKER_REL
                if marker.is_file():
                    try:
                        marker.unlink()
                        _log(syslog.LOG_INFO, f"cleared stale admin reset marker on {disk}")
                    except OSError as exc:
                        _log(
                            syslog.LOG_WARNING,
                            f"could not clear stale admin reset marker on {disk}: {exc}",
                        )
        except Exception as exc:  # noqa: BLE001 — no reset asked for, never block an install
            _log(syslog.LOG_WARNING, f"could not inspect admin reset marker on {disk}: {exc}")
        return

    try:
        with mount_user_data(disk) as root:
            marker = root / RESET_MARKER_REL
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("1\n", encoding="utf-8")
    except (InstallError, OSError) as exc:
        raise InstallError(
            f"Lemonseed was installed, but the admin password was not reset: {exc}"
        ) from exc
    _log(syslog.LOG_INFO, f"admin reset marker written to {marker}")


def write_channel_stub(grub_dir: Path, channel: str) -> None:
    """The chosen release channel rides the kernel command line (M8 P3):
    a fresh install has no @var yet (the first-boot provisioner creates it),
    so the ESP stub is the only durable seed. Stable writes nothing — absence
    means stable — and a beta stub survives A/B updates because the ESP is
    never touched by the updater. The control plane adopts the choice into its
    settings table at first boot; from then on the materialized channel file
    outranks the command line. boot/grub.cfg sources this file into every
    linux line, next to the amdgpu kernel-params stub it mirrors.
    """
    stub = grub_dir / "channel-params"
    if channel == "beta":
        stub.write_text("set channel_params='lemonseed.channel=beta'\n", encoding="utf-8")
    elif stub.is_file():
        stub.unlink()


SLOT_VERSIONS_REL = Path("@var/lib/lemonseed/slot-versions.json")


def write_slot_registry(disk: str, version: str) -> None:
    """Seed the slot-version registry on the target's user-data (M8 P6, D30).

    The rollback verb and the boot-menu labels read what each slot holds, so
    the installed slot is recorded here. Preserved user-data keeps the other
    slot's record — that root is untouched by the reinstall — while the
    installed slot's record is replaced and any rollback marker is dropped:
    the reinstall supersedes it. Best-effort: a fresh install's user-data is
    provisioned but write failures are logged, and the next update re-records
    both slots anyway (the updater self-heals the running one).
    """
    try:
        with mount_user_data(disk) as root:
            target = root / SLOT_VERSIONS_REL
            try:
                registry = json.loads(target.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                registry = {}
            if not isinstance(registry, dict):
                registry = {}
            slots = registry.get("slots") if isinstance(registry.get("slots"), dict) else {}
            slots["a"] = {
                "version": version,
                "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            registry["slots"] = slots
            registry["rolled_back_version"] = None
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    except (InstallError, OSError) as exc:
        _log(syslog.LOG_WARNING, f"could not record the slot-version registry on {disk}: {exc}")


def apply_channel_file(disk: str, channel: str) -> None:
    """Record the chosen release channel on the target's user-data (M8 P3, D-C4).

    ``@var/lib/lemonseed/channel`` is the file the packaged updater wrapper
    reads (the settings table's materialization), so the box checks the channel
    the installer was asked for. Only non-stable channels are written: absence
    means stable. The control plane imports the value into ``cp_meta`` at boot
    and owns the file from then on. Like the reset marker, a failed write
    fails the install — a silently applied channel contradicts an explicit
    choice. Only meaningful for preserved user-data: a fresh install has no
    btrfs yet and carries the choice on the ESP instead (seed_esp).
    """
    try:
        with mount_user_data(disk) as root:
            target = root / CHANNEL_FILE_REL
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(channel + "\n", encoding="utf-8")
    except (InstallError, OSError) as exc:
        raise InstallError(f"Lemonseed was installed, but the channel choice was not recorded: {exc}") from exc
    _log(syslog.LOG_INFO, f"release channel '{channel}' written to {target}")


def parse_admin_roster(raw: str) -> list[str] | None:
    """Admin usernames from a CP ``admins.json`` payload, or None if unreadable.

    The control plane writes ``{"admins": ["alice", "bob"]}``
    (``auth.publish_admin_roster``). Anything we cannot understand means "we do
    not know", never an error.
    """
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    admins = payload.get("admins")
    if not isinstance(admins, list):
        return None
    return [str(name).strip() for name in admins if str(name).strip()]


def read_admin_roster(disk: str) -> list[str] | None:
    """Admins recorded on ``disk``'s user-data, or None when unknown.

    None covers "no such file" (a box predating the roster), "cannot mount" and
    "unparsable"; the UI then falls back to wording that names nobody instead of
    claiming a count it did not actually read (D-28).
    """
    try:
        with mount_user_data(disk, read_only=True) as root:
            raw = (root / ADMIN_ROSTER_REL).read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — affordance read, must never fail disk reporting
        _log(syslog.LOG_INFO, f"no admin roster on {disk}: {exc}")
        return None
    return parse_admin_roster(raw)


def attach_admin_rosters(disks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add ``admin_usernames`` to disk details for the installer UI (D-28).

    Only disks already verified as having preservable user data are probed: this
    is the same payload that reports ``preservable_user_data``, and probing any
    other disk would mount a volume we may be about to wipe.
    """
    for disk in disks:
        probeable = (
            bool(disk.get("eligible"))
            and bool(disk.get("preservable_user_data"))
            and bool(disk.get("path"))
        )
        disk["admin_usernames"] = read_admin_roster(disk["path"]) if probeable else None
    return disks


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
    """Return (manifest, directory URL that contains the artifacts).

    The detached signature is verified against the baked trust set before the
    manifest is parsed, so its SHA-256 values are only ever trusted after the
    root of trust holds (M8 P1). Fail-closed: no signature, no manifest.
    """
    candidates = [
        base + "manifest.json",
        base + "latest/manifest.json",
    ]
    errors: list[str] = []
    for url in candidates:
        try:
            with _urlopen(url) as resp:
                manifest_bytes = resp.read()
            with _urlopen(url + ".sig") as resp:
                sig_bytes = resp.read()
            lemonseed_verify.verify_manifest(manifest_bytes, sig_bytes)
            manifest = json.loads(manifest_bytes.decode("utf-8"))
            lemonseed_verify.check_signing_metadata(manifest)
            dir_url = url.rsplit("/", 1)[0] + "/"
            return manifest, dir_url
        except VerificationError as exc:
            # Wrap: the verifier's legible reason becomes the InstallError the
            # installer's job/HTTP layers already surface.
            errors.append(f"{url}: {exc}")
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, TimeoutError) as exc:
            errors.append(f"{url}: {exc}")
    raise InstallError(
        "Could not download a signed image manifest. Tried:\n  " + "\n  ".join(errors)
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
        raise ReleaseSourceError(f"Local artifact missing: {src} ({label})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    if expected_sha:
        digest = sha256_file(dest)
        if digest.lower() != expected_sha.lower():
            raise ReleaseSourceError(
                f"Checksum mismatch for {label}: got {digest}, expected {expected_sha}"
            )


def load_local_manifest(directory: Path) -> dict[str, Any]:
    """Load a local artifact-set manifest, verifying its signature first.

    A directory with no manifest.json at all is an unsigned raw dev set (the
    seed of P3's ``--dev``); the moment a manifest exists it must carry a
    valid signature (D10).
    """
    man = directory / "manifest.json"
    if not man.is_file():
        return {
            "version": "local",
            "artifacts": {
                "vmlinuz": {"path": "vmlinuz"},
                "root.squashfs": {"path": "root.squashfs"},
                "initrd.img": {"path": "initrd.img"},
            },
        }
    try:
        lemonseed_verify.verify_manifest_files(man)
        manifest = json.loads(man.read_text(encoding="utf-8"))
        lemonseed_verify.check_signing_metadata(manifest)
    except VerificationError as exc:
        raise ReleaseSourceError(str(exc)) from exc
    return manifest


def artifact_relpath(manifest: dict[str, Any], key: str) -> tuple[str, str | None]:
    arts = manifest.get("artifacts") or {}
    entry = arts.get(key) or {}
    path = entry.get("path") or key
    sha = entry.get("sha256")
    return path, sha


def acquire_artifacts(
    progress: ProgressFn | None, channel: str | None = None
) -> tuple[dict[str, Path], str]:
    """Acquire the release set, strictly before anything touches the disk.

    Every failure here is a *source* failure (network, discovery, signature,
    checksum): the product sentence goes to the UI, the technical reason in
    ``detail`` — and retry/reboot are safe because nothing was written.
    """
    try:
        return _acquire_artifacts(progress, channel=channel)
    except lemonseed_artifacts.ArtifactError as exc:
        raise ReleaseSourceError(str(exc)) from exc


def _acquire_artifacts(
    progress: ProgressFn | None, channel: str | None = None
) -> tuple[dict[str, Path], str]:
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
        if manifest.get("payload"):
            # Sharded release set: verify parts, assemble the tar, re-verify.
            try:
                files = lemonseed_artifacts.assemble_payload(
                    manifest,
                    source_dir=local,
                    base_url=None,
                    download_dir=DOWNLOAD_DIR,
                    progress=progress,
                )
            except lemonseed_artifacts.ArtifactError as exc:
                raise ReleaseSourceError(str(exc)) from exc
            _progress(
                progress,
                phase="download",
                message=f"Payload assembled (version {manifest.get('version', 'unknown')})",
                progress=45,
                version=manifest.get("version"),
            )
            return files, str(manifest.get("version") or "local")
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

    override = image_base_url()
    if override:
        # A lemonseed.image= / LEMONSEED_IMAGE_BASE_URL source override:
        # production-strict, unchanged (D8).
        base = override
        _progress(
            progress,
            phase="download",
            message=f"Fetching manifest from {base}",
            progress=6,
        )
    else:
        # GitHub discovery (M8 P3): the channel resolves to a release; the
        # assets then ride the CDN exactly like any override set.
        picked = lemonseed_artifacts.resolve_channel(channel)
        resolved = lemonseed_artifacts.resolve_release_base(picked)
        if resolved is None:
            # An unpublished set (e.g. a local test build): the channel is the
            # only source the USB carries. The user sentence stays generic;
            # the detail names the channel and the API that answered.
            raise ReleaseSourceError(
                f"No releases found on the {picked} channel at "
                f"{lemonseed_artifacts.releases_api_url()}"
            )
        base, tag = resolved
        _progress(
            progress,
            phase="download",
            message=f"Channel {picked}: release {tag}",
            progress=4,
        )
    manifest, dir_url = fetch_manifest(base)
    version = manifest.get("version", "unknown")
    if manifest.get("payload"):
        # Sharded release set over HTTP (or file://) — the P3 channel shape.
        try:
            files = lemonseed_artifacts.assemble_payload(
                manifest,
                source_dir=None,
                base_url=dir_url,
                download_dir=DOWNLOAD_DIR,
                progress=progress,
            )
        except lemonseed_artifacts.ArtifactError as exc:
            raise ReleaseSourceError(str(exc)) from exc
        _progress(
            progress,
            phase="download",
            message=f"Payload assembled (version {version})",
            progress=45,
            version=version,
        )
        return files, str(version)
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


def seed_esp(
    disk: str,
    files: dict[str, Path],
    progress: ProgressFn | None,
    channel: str = "stable",
    version: str = "unknown",
) -> None:
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
            # The boot menu names the version on each slot (M8 P6, D2): the
            # labels are grubenv vars the template expands. Slot b is the
            # updater's to label.
            _run(
                [
                    "grub-editenv",
                    str(ESP_MOUNT / "grub" / "grubenv"),
                    "set",
                    f"slot_a_version={version}",
                ]
            )

        write_channel_stub(grub_dir, channel)

        # APU iGPU shared memory: write the ttm GRUB stub to the target
        # ESP so the first appliance boot already has the correct GPU/CPU
        # split (no post-install reboot). The script detects the ESP is
        # already mounted (mountpoint check) and skips its own mount. Uses
        # lspci for iGPU detection (no Vulkan ICD in the installer). Failure
        # is non-fatal — the appliance's lemonseed-amdgpu-kernel-params
        # service will retry on first boot.
        if AMDGPU_KERNEL_PARAMS.is_file():
            try:
                result = _run(["bash", str(AMDGPU_KERNEL_PARAMS)])
                last = (result.stdout.strip().splitlines() or ["ok"])[-1]
                _progress(progress, phase="esp", message=last, progress=87)
            except subprocess.CalledProcessError as exc:
                detail = ""
                if exc.stdout and exc.stdout.strip():
                    detail = exc.stdout.strip().splitlines()[-1]
                _progress(
                    progress,
                    phase="esp",
                    message=f"GPU memory setup deferred to first boot: {detail}" if detail
                    else "GPU memory setup deferred to first boot",
                    progress=87,
                )

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
    disk_path: str,
    progress: ProgressFn | None = None,
    preserve_user_data: bool = False,
    reset_admin_credentials: bool = False,
    channel: str = "stable",
) -> None:
    _progress(
        progress,
        phase="validate",
        message="Checking hardware requirements…",
        progress=2,
        error=None,
        can_reboot=False,
    )
    require_host_ready()
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
    files, version = acquire_artifacts(progress, channel=channel)
    if preserve_user_data and not disk.get("preservable_user_data"):
        raise InstallError("Preserve user data was requested for an unverified disk")
    partition_disk(disk_path, progress, preserve_user_data=preserve_user_data)
    root_a = wait_part(disk_path, "root-a")
    _progress(progress, phase="write", message="Writing system image to root-a", progress=58)
    write_raw(files["root.squashfs"], root_a, progress, 58, 84)
    _progress(progress, phase="write", message="System image written", progress=85)
    seed_esp(disk_path, files, progress, channel=channel, version=version)
    # The slot-version registry seeds the rollback verb and the boot-menu
    # labels (M8 P6, D30): slot a is the release just installed. Best-effort —
    # a failed write is logged, and the first update re-records both slots.
    write_slot_registry(disk_path, version)
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
    # The payload staging and part cache served their purpose; the ISO's
    # persistent layer is tiny, so do not leave ~3 GB behind on it.
    lemonseed_artifacts.cleanup_payload(DOWNLOAD_DIR)
    # Preserve the install log on the target's user-data (mounted by
    # partition_disk for preserve, or freshly mounted here) so it can be
    # inspected after the first appliance boot.
    try:
        if INSTALL_LOG.is_file():
            ud_part = parts_on_disk(disk_path).get("user-data")
            if ud_part and Path(ud_part).exists():
                user_data = Path("/run/lemonseed-install-user-data")
                user_data.mkdir(parents=True, exist_ok=True)
                mounted_ud = os.path.ismount(str(user_data))
                if not mounted_ud:
                    subprocess.run(
                        ["mount", ud_part, str(user_data)], check=True, capture_output=True
                    )
                    mounted_ud = True
                try:
                    dst = user_data / "lemonseed" / "installer.log"
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(INSTALL_LOG, dst)
                    _log(syslog.LOG_INFO, f"install log copied to {dst}")
                finally:
                    if mounted_ud:
                        subprocess.run(["umount", str(user_data)], check=False)
    except Exception as exc:  # noqa: BLE001 — best-effort, never fail the install
        _log(syslog.LOG_WARNING, f"could not copy install log to user-data: {exc}")
    # D-27: the marker is the only way back into a box whose admin password was
    # lost, so spell the intent out on the volume the control plane reads. A
    # wiped install destroys the partition table, so only preserved user data
    # can carry it. Unlike the log copy above, a failed write fails the install
    # — the alternative is a household that believes a reset happened.
    if reset_admin_credentials and not preserve_user_data:
        _log(syslog.LOG_WARNING, "admin reset requested without preserved user data; skipped")
    if preserve_user_data:
        apply_reset_marker(disk_path, reset_admin_credentials)
        # The chosen channel is recorded on preserved user-data (D-C4). A
        # fresh install carries it on the ESP instead (see seed_esp) — @var
        # does not exist until the first-boot provisioner runs. A failed write
        # fails the install: a silently flipped channel contradicts an
        # explicit choice, the same standard the reset marker is held to.
        if channel != "stable":
            apply_channel_file(disk_path, channel)
    _progress(
        progress,
        phase="await_usb",
        message=(
            f"Lemonseed {version} is installed. "
            "Remove the installer USB, then click Reboot."
        ),
        progress=100,
        can_reboot=True,
        version=version,
    )
