#!/usr/bin/env python3
"""Rebase container image pins onto the digest CI validated (M6.6 S5.2).

``control-plane/container-images.json`` is the only place an image reference is
written, and its ``digest`` field is what an appliance actually runs. Release
tags move — the Postgres and Valkey pins both changed digest on 2026-10-12
while this repo was being written — so a pin has to be rebased *deliberately*,
by whoever just ran ``make test`` / QEMU against the candidate, rather than
silently by every ``docker pull`` on every deployed box.

    ./scripts/refresh-image-pins.py --check        # report drift, no writes
    ./scripts/refresh-image-pins.py                # rebase every stale pin
    ./scripts/refresh-image-pins.py postgres       # just one pin
    ./scripts/refresh-image-pins.py --set-tag open_webui=v0.12.0

Asking the registry (``docker buildx imagetools inspect``) rather than pulling
means this works for a 1.7 GB image without downloading it. The digest returned
is the manifest *index* digest, which is the same digest space Nix's
``pullImage.imageDigest`` and Docker's ``RepoDigests`` use, so one value feeds
all three consumers.

Refuses to write anything when docker or the registry is unavailable: a pin we
could not resolve is worse than a pin we have not refreshed yet.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
PINS_PATH = ROOT / "control-plane" / "container-images.json"

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# Only the column-0 `Digest:` line; per-platform digests are indented.
_TOP_DIGEST_RE = re.compile(r"^Digest:\s+(sha256:[0-9a-f]{64})\s*$", re.MULTILINE)

# Where a digest change also needs a fixed-output hash rewritten.
HASH_FILES = {
    "postgres": "nixos/modules/qemu-docker-cache.nix",
    "valkey": "nixos/modules/qemu-docker-cache.nix",
    "open_webui": "flake.nix",
}

Resolver = Callable[[str], str]


class RefreshError(RuntimeError):
    """The registry could not be asked, or answered something unusable."""


def load_pins(path: Path = PINS_PATH) -> Dict[str, Dict[str, str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RefreshError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise RefreshError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise RefreshError(f"{path} is empty or not an object")
    return data


def parse_digest(output: str) -> str:
    """Extract the top-level manifest-index digest from imagetools output."""
    match = _TOP_DIGEST_RE.search(output)
    if not match:
        raise RefreshError(f"no top-level Digest: in registry output: {output[:200]!r}")
    return match.group(1)


def registry_digest(name_tag: str) -> str:
    """Ask the registry for the digest a tag currently points at (no pull)."""
    if shutil.which("docker") is None:
        raise RefreshError("docker not found on PATH; cannot resolve image digests")
    cmd = ["docker", "buildx", "imagetools", "inspect", name_tag]
    try:
        done = subprocess.run(cmd, text=True, capture_output=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RefreshError(f"registry query failed for {name_tag}: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        raise RefreshError(f"registry query failed for {name_tag}: {detail[:300]}")
    return parse_digest(done.stdout)


def plan(
    pins: Dict[str, Dict[str, str]],
    keys: List[str],
    resolver: Resolver,
    set_tags: Optional[Dict[str, str]] = None,
) -> List[Dict[str, str]]:
    """Resolve each selected pin; returns rows with ``status`` of same/moved/new-tag."""
    set_tags = set_tags or {}
    unknown = set(set_tags) - set(pins)
    if unknown:
        raise RefreshError(f"unknown pin key(s) in --set-tag: {sorted(unknown)}")
    rows: List[Dict[str, str]] = []
    for key in keys:
        if key not in pins:
            raise RefreshError(f"no image pin named {key!r} in {PINS_PATH.name}")
        pin = pins[key]
        old_tag = str(pin.get("tag") or "")
        tag = set_tags.get(key, old_tag)
        old_digest = str(pin.get("digest") or "")
        name_tag = f"{pin['name']}:{tag}"
        digest = resolver(name_tag)
        if not _DIGEST_RE.match(digest):
            raise RefreshError(f"resolved digest for {name_tag} is malformed: {digest!r}")
        if set_tags.get(key):
            status = "new-tag"
        elif digest != old_digest:
            status = "moved"
        else:
            status = "same"
        rows.append(
            {
                "key": key,
                "name": str(pin["name"]),
                "tag": tag,
                "old_digest": old_digest,
                "digest": digest,
                "status": status,
            }
        )
    return rows


def render(rows: List[Dict[str, str]]) -> str:
    """A fixed-width report; digests shortened because nobody reads 64 hex."""
    if not rows:
        return "no pins selected"
    width = max(len(r["key"]) for r in rows)
    lines = []
    for row in rows:
        lines.append(
            f"  {row['key']:<{width}}  {row['name']}:{row['tag']}  "
            f"{row['digest'][7:19]}…  {row['status']}"
        )
    return "\n".join(lines)


def apply_rows(
    pins: Dict[str, Dict[str, str]], rows: List[Dict[str, str]]
) -> Dict[str, Dict[str, str]]:
    """Return pins with resolved tag/digest written back (order preserved)."""
    out = json.loads(json.dumps(pins))
    for row in rows:
        out[row["key"]] = {**out[row["key"]], "tag": row["tag"], "digest": row["digest"]}
    return out


def write_pins(pins: Dict[str, Dict[str, str]], path: Path = PINS_PATH) -> None:
    text = json.dumps(pins, indent=2, sort_keys=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def main(argv: Optional[List[str]] = None, resolver: Optional[Resolver] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("keys", nargs="*", help="pin keys (default: all)")
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report drift; exit 1 if any pin moved (no writes)",
    )
    parser.add_argument(
        "--set-tag",
        action="append",
        default=[],
        metavar="KEY=TAG",
        help="rebase a pin to a new tag before resolving (repeatable)",
    )
    parser.add_argument("--pins", type=Path, default=PINS_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    set_tags: Dict[str, str] = {}
    for item in args.set_tag:
        if "=" not in item:
            parser.error(f"--set-tag wants KEY=TAG, got {item!r}")
        key, tag = item.split("=", 1)
        set_tags[key.strip()] = tag.strip()

    resolve = resolver or registry_digest
    try:
        pins = load_pins(args.pins)
        keys = args.keys or sorted(pins)
        rows = plan(pins, keys, resolve, set_tags)
    except RefreshError as exc:
        print(f"refresh-image-pins: {exc}", file=sys.stderr)
        return 2

    print(render(rows))
    changed = [r for r in rows if r["status"] != "same"]

    if args.check:
        if changed:
            print(
                f"\n{len(changed)} pin(s) are out of date. "
                "Run ./scripts/refresh-image-pins.py after validating the candidate.",
            )
            return 1
        print("\nall pins current")
        return 0

    if not changed:
        print("\nnothing to write")
        return 0

    try:
        write_pins(apply_rows(pins, rows), args.pins)
    except OSError as exc:
        print(f"refresh-image-pins: could not write {args.pins}: {exc}", file=sys.stderr)
        return 2

    try:
        shown = args.pins.relative_to(ROOT)
    except ValueError:
        shown = args.pins
    print(f"\nwrote {shown}")
    for hash_file in sorted({HASH_FILES.get(r["key"], "") for r in changed} - {""}):
        print(
            f"  next: {hash_file} — update the pullImage `hash` for the new digest "
            "(nix will report the expected value on the first build failure)"
        )
    print("  next: make test && make build-qemu && make test-qemu before shipping")
    return 0


if __name__ == "__main__":
    sys.exit(main())
