"""Container image pins — one source of truth for every image reference.

M6.6 step 5 (S5.1). Before this module, the same three images were named in
four places: floating tags in ``nixos/modules/data-services.nix``, duplicated
constants here in ``data_services.py`` (with a comment begging them to stay in
sync), a tag-only default in ``modules.py``, and hand-copied digests in
``nixos/modules/qemu-docker-cache.nix`` + ``flake.nix``. Nothing checked that
they agreed, and the Postgres and Valkey pins floated inside a major version —
so upstream could move an appliance's database underneath it.

``container-images.json`` is now the single input, read three ways:

* Nix at **eval** time (``builtins.fromJSON``) for the boot start scripts, the
  QEMU image tarballs, and the flake's ``qemuOwuiImage``;
* this module at **runtime** for the control-plane supervisors;
* ``scripts/refresh-image-pins.py`` at **packaging** time to rebase a pin.

Digest and tag are deliberately separate fields. ``digest`` is what we run
(immutability: production cannot drift ahead of what CI validated); ``tag`` is
what we *show* — a sha256 in the settings page is data no one can act on. A
``docker pull name@digest`` does **not** create ``name:tag`` locally, so the two
are not interchangeable and callers must pick deliberately: use
:func:`digest_ref` to pull/run and :func:`tag_ref` to display.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

PINS_FILE = Path(__file__).resolve().with_name("container-images.json")

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

_PINS: Optional[Dict[str, Any]] = None


class ImagePinError(RuntimeError):
    """A pin is missing or malformed. Never guess an image reference."""


def load_pins(path: Optional[Path] = None) -> Dict[str, Any]:
    """Return the parsed pins file, cached for the life of the process."""
    global _PINS
    if _PINS is not None and path is None:
        return _PINS
    source = path or PINS_FILE
    try:
        raw = source.read_text(encoding="utf-8")
    except OSError as exc:
        raise ImagePinError(f"container image pins unreadable at {source}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ImagePinError(f"container image pins are not valid JSON ({source}): {exc}") from exc
    if not isinstance(data, dict) or not data:
        raise ImagePinError(f"container image pins are empty or malformed: {source}")
    if path is None:
        _PINS = data
    return data


def reset_cache() -> None:
    """Drop the cached pins (tests, or a packaging-time rewrite)."""
    global _PINS
    _PINS = None


def entry(key: str, path: Optional[Path] = None) -> Dict[str, str]:
    """Return one validated pin entry: ``{'name', 'tag', 'digest'}``."""
    pins = load_pins(path)
    item = pins.get(key)
    if not isinstance(item, dict):
        raise ImagePinError(f"no container image pin named {key!r} in {PINS_FILE.name}")
    name = str(item.get("name") or "").strip()
    tag = str(item.get("tag") or "").strip()
    digest = str(item.get("digest") or "").strip()
    if not name or not tag:
        raise ImagePinError(f"image pin {key!r} is missing name or tag")
    if not _DIGEST_RE.match(digest):
        raise ImagePinError(f"image pin {key!r} has no valid sha256 digest: {digest!r}")
    if tag in ("latest", "main") or ":" in tag or "@" in tag:
        raise ImagePinError(f"image pin {key!r} has a floating or malformed tag: {tag!r}")
    return {"name": name, "tag": tag, "digest": digest}


def tag_ref(key: str, path: Optional[Path] = None) -> str:
    """``name:tag`` — the human-readable reference. Display only, never pulled."""
    pin = entry(key, path)
    return f"{pin['name']}:{pin['tag']}"


def digest_ref(key: str, path: Optional[Path] = None) -> str:
    """``name@sha256:…`` — the immutable reference used for pull and run."""
    pin = entry(key, path)
    return f"{pin['name']}@{pin['digest']}"


def version_label(key: str, path: Optional[Path] = None) -> str:
    """``v0.11.0`` — the pinned tag on its own, for the settings readout."""
    return entry(key, path)["tag"]
