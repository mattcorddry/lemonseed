#!/usr/bin/env python3
"""Generate icon-sized Lemonseed brand assets from assets/brand/sources/*.png

Requires Pillow.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Pillow required: pip install pillow", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets" / "brand"
SOURCE = BRAND / "source"
DESTS = (
    ROOT / "control-plane" / "static" / "brand",
    ROOT / "installer" / "static" / "brand",
)
SIZES = (16, 32, 48, 64, 128, 256, 512)


def main() -> None:
    src = SOURCE / "lemon-full.png"
    if not src.is_file():
        print(f"missing source image {SOURCE}/lemon-full.png", file=sys.stderr)
        sys.exit(1)

    img = Image.open(src).convert("RGBA")
    w, h = img.size
    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    img = img.crop((left, top, left + side, top + side))

    BRAND.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(BRAND / "lemon.jpg", quality=92, optimize=True)
    img.save(BRAND / "lemon.png", optimize=True)

    for size in SIZES:
        img.resize((size, size), Image.Resampling.LANCZOS).save(
            BRAND / f"lemon-{size}.png", optimize=True
        )
    img.resize((180, 180), Image.Resampling.LANCZOS).save(
        BRAND / "apple-touch-icon.png", optimize=True
    )
    ico = img.resize((32, 32), Image.Resampling.LANCZOS)
    ico.save(BRAND / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)])

    # Packaged UIs need small/medium icons only (skip multi-MB master + 512).
    serve = [
        "favicon.ico",
        "apple-touch-icon.png",
        "lemon.jpg",
        *(f"lemon-{s}.png" for s in SIZES if s <= 256),
    ]
    for dest in DESTS:
        dest.mkdir(parents=True, exist_ok=True)
        for name in serve:
            shutil.copy2(BRAND / name, dest / name)
        print(f"updated {dest}")


if __name__ == "__main__":
    main()
