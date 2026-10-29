#!/usr/bin/env python3
"""Release payload packing and verification (M8 P2).

A release is a flat asset set: manifest.json (+ .sig) outside the payload,
fixed-size `payload-NNN.part` byte slices of one canonical tar, and the USB
installer ISO as a direct asset. The manifest's `payload` block is the assembly
plan (proposals/M8/PACKAGING.md §2.2); the signature over the manifest is the
root of trust, so packing strictly precedes signing.

The consumer side (download parts → verify → assemble → re-verify) lives in
`lemonseed_artifacts.assemble_payload` — one implementation, shared by the
updater and the installer. This module owns the producer side (`pack`), the
release-dir `verify` used by `make release` (and, later, the P4 publisher's
self-check), and the GitHub size gates (D16).

The tarball is never stored on either side: `pack` streams tar bytes straight
into the chunk splitter, and the consumer streams the verified parts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Any

import lemonseed_artifacts as artifacts

# GitHub release limits (BACKLOG.md, GitHub-releases entry): each asset under
# 2 GiB, at most 1000 assets per release. Defaults here; tests may shrink them.
MAX_ASSET_BYTES = 2 * 1024**3
MAX_CHUNKS = 1000
PART_BYTES_DEFAULT = 1024**3  # 1 GiB; the final part is a remainder

# The payload members, in the fixed order the canonical tar is written in.
PAYLOAD_KEYS = ("vmlinuz", "root.squashfs", "initrd.img")


class ReleaseError(Exception):
    """A release set could not be packed, or does not verify. Legible, fatal."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class _ChunkSplitter:
    """A tarfile-compatible writer that hashes the tar and cuts fixed parts.

    Written to as ``tarfile`` copies member bytes through it; rolls over to the
    next `payload-NNN.part` whenever the current part reaches `part_bytes`, and
    tracks the whole-tar length and digest at the same time.
    """

    def __init__(self, out_dir: Path, part_bytes: int):
        self.out_dir = out_dir
        self.part_bytes = part_bytes
        self.tar_digest = hashlib.sha256()
        self.tar_length = 0
        self.parts: list[dict[str, Any]] = []
        self._index = 0
        self._part_digest = hashlib.sha256()
        self._part_length = 0
        self._handle = None

    def _roll(self) -> None:
        self._close_part()
        name = f"payload-{self._index:03d}.part"
        self._index += 1
        self._handle = (self.out_dir / name).open("wb")
        self._part_digest = hashlib.sha256()
        self._part_length = 0
        self.parts.append({"name": name, "length": 0, "sha256": None, "path": self._handle.name})

    def _close_part(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self.parts[-1]["length"] = self._part_length
            self.parts[-1]["sha256"] = self._part_digest.hexdigest()
            self._handle = None

    def write(self, data: bytes) -> int:
        self.tar_digest.update(data)
        self.tar_length += len(data)
        view = memoryview(data)
        while view:
            if self._handle is None:
                self._roll()
            take = min(len(view), self.part_bytes - self._part_length)
            piece = view[:take]
            self._handle.write(piece)
            self._part_digest.update(piece)
            self._part_length += take
            view = view[take:]
            if self._part_length >= self.part_bytes:
                self._close_part()
        return len(data)

    def tell(self) -> int:
        # tarfile tracks its own offsets through this (write mode).
        return self.tar_length

    def flush(self) -> None:
        pass

    def finish(self) -> tuple[int, str]:
        """Close the final part (a remainder is expected) and return (length, sha)."""
        self._close_part()
        return self.tar_length, self.tar_digest.hexdigest()


def cmd_pack(args: argparse.Namespace) -> int:
    artifacts_dir = Path(args.artifacts_dir)
    out_dir = Path(args.out_dir)
    installer = Path(args.installer) if args.installer else None
    manifest_path = artifacts_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ReleaseError(f"no manifest to pack: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("artifacts") or {}
    for key in PAYLOAD_KEYS:
        if key not in entries:
            raise ReleaseError(f"manifest is missing the {key!r} artifact")
    if "payload" in manifest:
        raise ReleaseError("manifest already carries a payload block — pack is not idempotent")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    splitter = _ChunkSplitter(out_dir, args.part_bytes)
    with tarfile.open(fileobj=splitter, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for key in PAYLOAD_KEYS:
            src = artifacts_dir / key
            if not src.is_file():
                raise ReleaseError(f"payload artifact missing: {src}")
            info = tarfile.TarInfo(key)
            info.size = src.stat().st_size
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            with src.open("rb") as handle:
                tar.addfile(info, handle)
    tar_length, tar_sha = splitter.finish()

    for part in splitter.parts:
        if part["length"] > args.max_asset_bytes:
            raise ReleaseError(
                f"size gate: {part['name']} is {part['length']} bytes, over the "
                f"{args.max_asset_bytes}-byte release-asset cap"
            )
        if not part["name"].isascii():
            raise ReleaseError(f"size gate: part name {part['name']!r} is not ASCII")
    if len(splitter.parts) > args.max_chunks:
        raise ReleaseError(
            f"size gate: {len(splitter.parts)} chunks exceed the {args.max_chunks}-chunk cap"
        )

    manifest["payload"] = {
        "tar": {"length": tar_length, "sha256": tar_sha},
        "chunks": [
            {"name": p["name"], "length": p["length"], "sha256": p["sha256"]}
            for p in splitter.parts
        ],
    }

    if installer is not None:
        if not installer.is_file():
            raise ReleaseError(f"installer image missing: {installer}")
        if not installer.name.isascii():
            raise ReleaseError(f"size gate: installer name {installer.name!r} is not ASCII")
        length = installer.stat().st_size
        if length > args.max_asset_bytes:
            raise ReleaseError(
                f"size gate: {installer.name} is {length} bytes, over the "
                f"{args.max_asset_bytes}-byte release-asset cap"
            )
        shutil.copyfile(installer, out_dir / installer.name)
        manifest["installer"] = {
            "name": installer.name,
            "length": length,
            "sha256": sha256_file(out_dir / installer.name),
        }

    # The rollback floor rides inside the signed bytes (M8 P6, D30): the
    # earliest version a rollback may target, defaulting to the release's own
    # date minus the horizon. A one-way-door change pins it explicitly via
    # MIN_ROLLBACK_VERSION (release.sh), disabling rollbacks past that line.
    floor = os.environ.get("MIN_ROLLBACK_VERSION") or artifacts.rollback_floor(
        str(manifest.get("version") or "")
    )
    if floor:
        manifest["rollback"] = {"min_rollback_version": floor}

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o644)

    print(f"==> packed {len(splitter.parts)} parts ({tar_length} bytes) into {out_dir}")
    for part in splitter.parts:
        print(f"    {part['name']}  {part['length']}")
    if installer is not None:
        print(f"    {installer.name}  {length}")
    return 0


def _verify_release_dir(release_dir: Path, trust_dir: str | None) -> None:
    """The publisher's self-check: the appliance's own verifier, run on the set."""
    import lemonseed_verify

    manifest_path = release_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ReleaseError(f"no manifest in {release_dir}")
    key = lemonseed_verify.verify_manifest_files(manifest_path, directory=Path(trust_dir) if trust_dir else None)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # The metadata cross-check must consult the SAME trust set the signature
    # just verified against — a --trust-dir override that never reaches it
    # would compare the named key against a default directory the build
    # machine does not even have (lemonbuild has no /usr/share/lemonseed).
    lemonseed_verify.check_signing_metadata(
        manifest, directory=Path(trust_dir) if trust_dir else None
    )
    print(f"OK: manifest signature verifies (key {key.name})")

    payload = manifest.get("payload") or {}
    chunks = payload.get("chunks") or []
    part_paths: list[Path] = []
    for chunk in chunks:
        part = release_dir / chunk["name"]
        if not part.is_file():
            raise ReleaseError(f"missing part: {part}")
        if part.stat().st_size != chunk["length"]:
            raise ReleaseError(
                f"part length mismatch for {chunk['name']}: "
                f"got {part.stat().st_size}, expected {chunk['length']}"
            )
        digest = sha256_file(part)
        if digest != chunk["sha256"]:
            raise ReleaseError(f"part checksum mismatch for {chunk['name']}")
        part_paths.append(part)
    print(f"OK: {len(part_paths)} parts verified")

    tar_block = payload.get("tar") or {}
    digest = hashlib.sha256()
    length = 0
    for path in part_paths:
        with path.open("rb") as handle:
            for piece in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(piece)
                length += len(piece)
    if length != tar_block.get("length") or digest.hexdigest() != tar_block.get("sha256"):
        raise ReleaseError("assembled tar length/hash mismatch")

    installer = manifest.get("installer")
    if installer:
        path = release_dir / installer["name"]
        if not path.is_file():
            raise ReleaseError(f"missing installer asset: {path}")
        if path.stat().st_size != installer["length"] or sha256_file(path) != installer["sha256"]:
            raise ReleaseError(f"installer asset checksum mismatch: {path}")
        print(f"OK: installer asset verified ({installer['name']})")
    print("OK: release set verifies")


def cmd_verify(args: argparse.Namespace) -> int:
    _verify_release_dir(Path(args.release_dir), args.trust_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="release payload pack/verify")
    sub = parser.add_subparsers(dest="command", required=True)

    pack = sub.add_parser("pack", help="build the canonical tar, cut parts, update the manifest")
    pack.add_argument("--artifacts-dir", required=True, help="dir with manifest.json + payload artifacts")
    pack.add_argument("--out-dir", required=True, help="release asset staging dir")
    pack.add_argument("--installer", default=None, help="USB installer ISO (direct asset)")
    pack.add_argument("--part-bytes", type=int, default=PART_BYTES_DEFAULT)
    pack.add_argument("--max-asset-bytes", type=int, default=MAX_ASSET_BYTES)
    pack.add_argument("--max-chunks", type=int, default=MAX_CHUNKS)
    pack.set_defaults(func=cmd_pack)

    verify = sub.add_parser("verify", help="verify a staged release directory")
    verify.add_argument("--release-dir", required=True)
    verify.add_argument("--trust-dir", default=None, help="trust set dir (default: baked)")
    verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ReleaseError as exc:
        print(f"release_payload: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
