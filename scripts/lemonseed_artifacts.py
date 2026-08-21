#!/usr/bin/env python3
"""Shared Lemonseed artifact acquire/verify helpers (installer + updater)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ProgressFn = Callable[..., None]

IMAGE_BASE_DEFAULT = "https://lemonseed.ai/releases/stable/"
USER_AGENT = "lemonseed/0.1"


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


def image_base_url(override: str | None = None) -> str:
    raw = (
        override
        or cmdline_val("lemonseed.image")
        or os.environ.get("LEMONSEED_IMAGE_BASE_URL")
        or IMAGE_BASE_DEFAULT
    )
    return raw.rstrip("/") + "/"


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
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            json.JSONDecodeError,
            TimeoutError,
            OSError,
        ) as exc:
            errors.append(f"{url}: {exc}")
    raise ArtifactError(
        "Could not download image manifest. Tried:\n  " + "\n  ".join(errors)
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


def acquire_artifacts(
    progress: ProgressFn | None = None,
    *,
    download_dir: Path,
    url: str | None = None,
    directory: str | None = None,
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

    base = image_base_url(url)
    _progress(
        progress,
        phase="download",
        message=f"Fetching manifest from {base}",
        progress=6,
    )
    manifest, dir_url = fetch_manifest(base)
    version = str(manifest.get("version") or "unknown")
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
