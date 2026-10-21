#!/usr/bin/env python3
"""Shared Lemonseed artifact acquire/verify helpers (installer + updater)."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import lemonseed_verify
from lemonseed_verify import VerificationError

ProgressFn = Callable[..., None]

# The release channel is GitHub releases (M8 P3, D8/D23): `lemonseed.ai` sits
# behind Cloudflare, which forbids serving large objects through its proxy, so
# a ~3 GB root.squashfs cannot be distributed from there. One REST listing per
# check; the assets themselves ride the CDN (D26).
RELEASES_API_DEFAULT = (
    "https://api.github.com/repos/mattcorddry/lemonseed/releases?per_page=10"
)
# The channel is *only* GitHub's prerelease flag (D-C1): the signed manifest
# describes the payload, never the audience, so a beta→stable flip is a GitHub
# metadata edit — no rebuild, no re-sign.
CHANNELS = ("stable", "beta")
# The settings table's materialization (control-plane/settings.py writes it;
# DB-free root jobs — the systemd timers — read it through the wrapper).
CHANNEL_FILE = Path("/var/lib/lemonseed/channel")
# Distinctive, as GitHub asks; never a token (D26).
USER_AGENT = "lemonseed-updater/0.1 (+https://github.com/mattcorddry/lemonseed)"


class ArtifactError(Exception):
    pass


def _progress(cb: ProgressFn | None, **kwargs: Any) -> None:
    if cb:
        cb(**kwargs)


def cmdline_val(key: str) -> str | None:
    path = Path("/proc/cmdline")
    if not path.is_file():
        return None
    prefix = key + "="
    for arg in path.read_text(encoding="utf-8").split():
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return None


def artifact_dir(override: str | None = None) -> Path | None:
    raw = override or os.environ.get("LEMONSEED_ARTIFACT_DIR") or cmdline_val("lemonseed.artifacts")
    if not raw:
        return None
    path = Path(raw)
    return path if path.is_dir() else None


def image_base_url(override: str | None = None) -> str | None:
    """A --url-style source override, or None (the caller discovers)."""
    raw = override or cmdline_val("lemonseed.image") or os.environ.get("LEMONSEED_IMAGE_BASE_URL")
    return raw.rstrip("/") + "/" if raw else None


def resolve_channel(override: str | None = None) -> str:
    """--channel flag > LEMONSEED_CHANNEL > the settings-materialized file
    > the installer's ESP stub (kernel command line) > stable.

    An unreadable or unknown file value falls back rather than bricking
    updates: the writer (control-plane settings.set_setting) is the whitelist.
    """
    raw = override or os.environ.get("LEMONSEED_CHANNEL")
    if raw is None:
        try:
            raw = CHANNEL_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            raw = ""
    if raw not in CHANNELS:
        raw = cmdline_val("lemonseed.channel") or ""
    return raw if raw in CHANNELS else "stable"


def releases_api_url() -> str:
    return os.environ.get("LEMONSEED_RELEASES_API") or RELEASES_API_DEFAULT


def resolve_release_base(channel: str) -> tuple[str, str] | None:
    """GitHub discovery (D5): the release *this channel* resolves to.

    Returns the asset base — everything under
    ``releases/download/<tag>/`` — plus the tag, or ``None`` when no release
    qualifies. ``None`` means "up to date", not an error (D-C2: the channel
    filters candidates; the version comparison decides). Drafts are never
    candidates; prereleases qualify for beta only.
    """
    try:
        with _urlopen(releases_api_url()) as resp:
            releases = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            retry = exc.headers.get("Retry-After") or "shortly"
            raise ArtifactError(
                "GitHub API rate limit reached (unauthenticated requests are "
                "60/hour, shared by every appliance behind your IP). "
                f"Try again {retry}."
            ) from exc
        raise ArtifactError(f"GitHub release API error: {exc}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"Could not reach the GitHub release API: {exc}") from exc
    if not isinstance(releases, list):
        raise ArtifactError("GitHub release API returned an unexpected answer (not a list)")

    candidates = [r for r in releases if isinstance(r, dict) and r.get("draft") is False]
    if channel == "stable":
        candidates = [r for r in candidates if r.get("prerelease") is False]
    if not candidates:
        return None
    # The API lists newest-first; sort anyway so the answer never depends on
    # the order GitHub happens to hand us.
    candidates.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    skipped: list[str] = []
    for release in candidates:
        tag = str(release.get("tag_name") or "")
        asset = next(
            (
                a
                for a in release.get("assets") or []
                if isinstance(a, dict) and a.get("name") == "manifest.json"
            ),
            None,
        )
        if not tag or not asset or not asset.get("browser_download_url"):
            skipped.append(tag or "(untagged release)")
            continue
        url = str(asset["browser_download_url"])
        return url[: -len("manifest.json")], tag
    raise ArtifactError(
        "No release on channel '"
        + channel
        + "' carries a manifest.json asset (skipped: "
        + ", ".join(skipped)
        + ")"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_device(path: str, expected_bytes: int) -> str:
    digest = hashlib.sha256()
    remaining = expected_bytes
    with open(path, "rb", buffering=0) as handle:
        while remaining > 0:
            chunk = handle.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
    if remaining != 0:
        raise ArtifactError(
            f"Could not read {expected_bytes} bytes from {path} (short by {remaining})"
        )
    return digest.hexdigest()


def _urlopen(url: str, timeout: int = 60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_manifest(base: str) -> tuple[dict[str, Any], str]:
    """Fetch manifest.json + its detached signature, verifying before parsing.

    The signature is checked against the baked trust set *before* the manifest
    is parsed, so its SHA-256 values become trustworthy only after the root of
    trust holds (M8 P1). A missing, malformed or wrong-key signature refuses
    the release with the verifier's legible reason.
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
            # Wrap: the verifier's legible reason becomes the boundary error
            # every caller already knows how to surface.
            errors.append(f"{url}: {exc}")
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
            TimeoutError,
            OSError,
        ) as exc:
            errors.append(f"{url}: {exc}")
    raise ArtifactError(
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
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ArtifactError(f"Download failed for {label}: {exc}") from exc

    digest = hasher.hexdigest()
    if expected_sha and digest.lower() != expected_sha.lower():
        raise ArtifactError(
            f"Checksum mismatch for {label}: got {digest}, expected {expected_sha}"
        )


def copy_local(src: Path, dest: Path, expected_sha: str | None, label: str) -> None:
    if not src.is_file():
        raise ArtifactError(f"Local artifact missing: {src} ({label})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    if expected_sha:
        digest = sha256_file(dest)
        if digest.lower() != expected_sha.lower():
            raise ArtifactError(
                f"Checksum mismatch for {label}: got {digest}, expected {expected_sha}"
            )


def load_local_manifest(directory: Path) -> dict[str, Any]:
    """Load a local artifact-set manifest, verifying its signature first.

    A directory that carries no manifest.json at all is an unsigned raw dev
    set (P3's future ``--dev`` seed); the moment a manifest exists it must
    carry a valid signature — there is no hash-only path (D10).
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
        raise ArtifactError(str(exc)) from exc
    return manifest


def artifact_relpath(manifest: dict[str, Any], key: str) -> tuple[str, str | None]:
    arts = manifest.get("artifacts") or {}
    entry = arts.get(key) or {}
    path = entry.get("path") or key
    sha = entry.get("sha256")
    return path, sha


def acquire_artifacts(
    progress: ProgressFn | None = None,
    *,
    download_dir: Path,
    url: str | None = None,
    directory: str | None = None,
    channel: str | None = None,
) -> tuple[dict[str, Path], str, dict[str, Any]]:
    download_dir.mkdir(parents=True, exist_ok=True)
    local = artifact_dir(directory)
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
            files = assemble_payload(
                manifest,
                source_dir=local,
                base_url=None,
                download_dir=download_dir,
                progress=progress,
            )
            version = str(manifest.get("version") or "local")
            _progress(
                progress,
                phase="download",
                message=f"Payload assembled (version {version})",
                progress=45,
                version=version,
            )
            return files, version, manifest
        files: dict[str, Path] = {}
        for key in ("vmlinuz", "root.squashfs", "initrd.img"):
            rel, sha = artifact_relpath(manifest, key)
            dest = download_dir / Path(rel).name
            copy_local(local / rel, dest, sha, key)
            files[key] = dest
        version = str(manifest.get("version") or "local")
        _progress(
            progress,
            phase="download",
            message=f"Artifacts ready (version {version})",
            progress=45,
            version=version,
        )
        return files, version, manifest

    if url:
        # --url is a source override: production-strict, unchanged (D8).
        base = image_base_url(url)
        _progress(
            progress,
            phase="download",
            message=f"Fetching manifest from {base}",
            progress=6,
        )
    else:
        # GitHub discovery (M8 P3): the channel resolves to a release; the
        # assets then ride the CDN exactly like any --url set.
        picked = resolve_channel(channel)
        resolved = resolve_release_base(picked)
        if resolved is None:
            raise ArtifactError(
                f"No release on channel '{picked}' to install — the channel is up to date"
            )
        base, tag = resolved
        _progress(
            progress,
            phase="download",
            message=f"Channel {picked}: release {tag}",
            progress=4,
        )
    manifest, dir_url = fetch_manifest(base)
    version = str(manifest.get("version") or "unknown")
    if manifest.get("payload"):
        # Sharded release set over HTTP (or file://) — the P3 channel shape.
        files = assemble_payload(
            manifest,
            source_dir=None,
            base_url=dir_url,
            download_dir=download_dir,
            progress=progress,
        )
        _progress(
            progress,
            phase="download",
            message=f"Payload assembled (version {version})",
            progress=45,
            version=version,
        )
        return files, version, manifest
    files = {}
    ranges = {
        "vmlinuz": (8, 14),
        "initrd.img": (14, 22),
        "root.squashfs": (22, 45),
    }
    for key, (lo, hi) in ranges.items():
        rel, sha = artifact_relpath(manifest, key)
        dest = download_dir / Path(rel).name
        download_file(dir_url + rel, dest, sha, progress, lo, hi, key)
        files[key] = dest
    _progress(
        progress,
        phase="download",
        message=f"Downloaded Lemonseed {version}",
        progress=45,
        version=version,
    )
    return files, version, manifest


def write_raw(
    src: Path,
    dest: str,
    progress: ProgressFn | None = None,
    pct_lo: int = 50,
    pct_hi: int = 85,
) -> None:
    total = src.stat().st_size
    dest_size = int(
        subprocess.check_output(["blockdev", "--getsize64", dest], text=True).strip()
    )
    if dest_size < total:
        raise ArtifactError(
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


# --- Release payload assembly (M8 P2) -------------------------------------------------
# A sharded release ships payload-NNN.part byte slices of one canonical tar;
# the signed manifest carries both the chunk list and the reassembled artifacts'
# hashes. See proposals/M8/PACKAGING.md. The tarball is never stored: the parts
# are streamed once to hash, once to extract, so peak disk use is 1x the
# extracted payload, under `download_dir` on /var (SSD-backed), never tmpfs.

PAYLOAD_STAGING = "payload"
PAYLOAD_PARTS = "parts"
PAYLOAD_MARGIN_BYTES = 64 * 1024 * 1024


class _ConcatReader:
    """Sequential reader over the verified part files, in order.

    Blocks until `size` bytes are available or global EOF, so tarfile's stream
    reader never sees a short read at a part boundary.
    """

    def __init__(self, paths: list[Path]):
        self._paths = paths
        self._index = 0
        self._handle: Any = None

    def read(self, size: int = 1024 * 1024) -> bytes:
        pieces: list[bytes] = []
        got = 0
        while size < 0 or got < size:
            if self._handle is None:
                if self._index >= len(self._paths):
                    break
                self._handle = self._paths[self._index].open("rb")
                self._index += 1
            piece = self._handle.read((size if size > 0 else 1024 * 1024) - got)
            if not piece:
                self._handle.close()
                self._handle = None
                continue
            pieces.append(piece)
            got += len(piece)
        return b"".join(pieces)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def _check_free_space(download_dir: Path, required: int) -> None:
    """Refuse before downloading/assembling when /var cannot hold the payload."""
    free = shutil.disk_usage(download_dir).free
    need = required + PAYLOAD_MARGIN_BYTES
    if free < need:
        raise ArtifactError(
            f"not enough space for payload assembly in {download_dir}: need "
            f"{need} bytes (extracted payload + margin), {free} free — free up "
            "space on /var and retry"
        )


def _copy_member(member: tarfile.TarInfo, src: Any, dest: Path, expected_sha: str) -> str:
    """Stream one payload member to disk, hashing as it goes. Patchable in tests
    to simulate a full staging filesystem (ENOSPC)."""
    digest = hashlib.sha256()
    count = 0
    with dest.open("wb") as out:
        for piece in iter(lambda: src.read(1024 * 1024), b""):
            out.write(piece)
            digest.update(piece)
            count += len(piece)
    if count != member.size:
        raise ArtifactError(
            f"payload member {member.name}: wrote {count} bytes, expected {member.size}"
        )
    if digest.hexdigest() != expected_sha:
        raise ArtifactError(f"payload member {member.name} checksum mismatch after extraction")
    return digest.hexdigest()


def _extract_tar_members(
    reader: _ConcatReader,
    expected: dict[str, tuple[int, str]],
    staging: Path,
) -> dict[str, Path]:
    """Stream the verified tar into staging, re-verifying every member against
    the signed manifest's hashes (the second verification pass)."""
    files: dict[str, Path] = {}
    with tarfile.open(fileobj=reader, mode="r|") as tar:
        for member in tar:
            name = member.name[2:] if member.name.startswith("./") else member.name
            if name not in expected:
                raise ArtifactError(f"unexpected payload member: {member.name!r}")
            if not member.isfile():
                raise ArtifactError(f"unexpected non-file payload member: {member.name!r}")
            length, sha = expected[name]
            if member.size != length:
                raise ArtifactError(
                    f"payload member {name}: size {member.size}, expected {length}"
                )
            src = tar.extractfile(member)
            if src is None:
                raise ArtifactError(f"payload member {name} could not be extracted")
            _copy_member(member, src, staging / name, sha)
            files[name] = staging / name
    missing = sorted(set(expected) - set(files))
    if missing:
        raise ArtifactError(f"payload tar is missing members: {missing}")
    return files


def cleanup_payload(download_dir: Path) -> None:
    """Remove assembled staging + the verified part cache (after a successful
    apply, or when the caller tears down). Best-effort: a failed cleanup must
    not fail the update."""
    for name in (PAYLOAD_STAGING, PAYLOAD_PARTS):
        path = Path(download_dir) / name
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)


def assemble_payload(
    manifest: dict[str, Any],
    *,
    source_dir: Path | None,
    base_url: str | None,
    download_dir: Path,
    progress: ProgressFn | None = None,
) -> dict[str, Path]:
    """Download/verify the payload chunks, assemble the tar, extract and
    re-verify every artifact. Returns {artifact key: staging path}.

    Order is fail-closed (PACKAGING.md §4): each part is verified before it is
    used, the assembled tar's length + sha256 are verified before anything is
    extracted, and every extracted file is re-verified against the manifest.
    The tarball itself is never written: two passes over the verified parts —
    one to hash, one to extract — keep peak disk use at 1x the extracted
    payload, under `download_dir` on /var.
    """
    payload = manifest.get("payload") or {}
    chunks = payload.get("chunks") or []
    if not chunks:
        raise ArtifactError("payload manifest carries no chunks")
    artifacts = manifest.get("artifacts") or {}

    tar_block = payload.get("tar") or {}
    tar_length = int(tar_block.get("length") or 0)
    tar_sha = str(tar_block.get("sha256") or "")

    # Free-space preflight: the manifest states everything needed — the
    # extracted payload sizes — plus a fixed margin.
    required = 0
    for key, entry in artifacts.items():
        length = int(entry.get("length") or 0)
        if length <= 0:
            raise ArtifactError(f"payload manifest artifact {key!r} has no usable length")
        required += length
    download_dir.mkdir(parents=True, exist_ok=True)
    _check_free_space(download_dir, required)

    staging = download_dir / PAYLOAD_STAGING
    parts_dir = download_dir / PAYLOAD_PARTS
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)

    try:
        # 1. Obtain + verify every part, in order. A verified part is a
        # checkpoint: it stays in the parts cache and is never re-fetched.
        part_paths: list[Path] = []
        total_chunk_bytes = sum(int(c["length"]) for c in chunks)
        done = 0
        for index, chunk in enumerate(chunks):
            name = str(chunk.get("name") or "")
            if not name or name != os.path.basename(name) or not name.isascii():
                raise ArtifactError(f"payload chunk has a non-bare/non-ASCII name: {name!r}")
            length = int(chunk.get("length") or 0)
            sha = str(chunk.get("sha256") or "")
            lo = 8 + int((45 - 8) * (done / total_chunk_bytes)) if total_chunk_bytes else 8
            hi = (
                8 + int((45 - 8) * ((done + length) / total_chunk_bytes))
                if total_chunk_bytes
                else 45
            )

            if source_dir is not None:
                part = source_dir / name
                if not part.is_file():
                    raise ArtifactError(f"payload part missing: {part}")
                if part.stat().st_size != length:
                    raise ArtifactError(
                        f"payload part {name}: got {part.stat().st_size} bytes, expected {length}"
                    )
                if sha256_file(part) != sha:
                    raise ArtifactError(f"Checksum mismatch for {name}")
                part_paths.append(part)
            else:
                dest = parts_dir / name
                # A verified part already in the cache is a checkpoint: skip it.
                if not (dest.is_file() and dest.stat().st_size == length and sha256_file(dest) == sha):
                    download_file(base_url + name, dest, sha, progress, lo, hi, name)
                if dest.stat().st_size != length:
                    raise ArtifactError(
                        f"payload part {name}: got {dest.stat().st_size} bytes, expected {length}"
                    )
                part_paths.append(dest)
            done += length

        # 2. Pass 1: hash the assembled tar without writing it.
        _progress(
            progress,
            phase="assemble",
            message="Verifying assembled payload",
            progress=45,
        )
        reader = _ConcatReader(part_paths)
        digest = hashlib.sha256()
        length = 0
        for piece in iter(lambda: reader.read(4 * 1024 * 1024), b""):
            digest.update(piece)
            length += len(piece)
        if length != tar_length or digest.hexdigest() != tar_sha:
            raise ArtifactError(
                f"assembled payload tar mismatch: got {length} bytes "
                f"({digest.hexdigest()}), expected {tar_length} ({tar_sha})"
            )

        # 3. Pass 2: stream the same bytes into staging, re-verifying every
        # member against the manifest's hashes. Nothing touches a partition
        # before this returns.
        expected = {
            key: (int(entry.get("length") or 0), str(entry.get("sha256") or ""))
            for key, entry in artifacts.items()
        }
        reader = _ConcatReader(part_paths)
        try:
            files = _extract_tar_members(reader, expected, staging)
        finally:
            reader.close()
    except ArtifactError:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if exc.errno == errno.ENOSPC:
            raise ArtifactError(
                f"staging filesystem is full while extracting the payload: {exc} — "
                "free up space on /var and retry"
            ) from exc
        raise ArtifactError(f"payload extraction failed: {exc}") from exc

    _progress(
        progress,
        phase="assemble",
        message="Payload verified and extracted",
        progress=45,
    )
    return files
