#!/usr/bin/env python3
"""Merge ``modules/*/module.yaml`` into the shipped modules-catalog.json (M9 P2).

A thin CLI over ``control-plane/modules_catalog.build_catalog``: the strict
validation and the merge invariants live in one place, so the build step, the
unit tests and the runtime loader cannot disagree about what a valid module
is. Fails closed on the first invalid module — a catalog that half-works is
worse than none, because the installer would offer a module the box cannot
actually run.

    python3 scripts/build-modules-catalog.py --out out/modules-catalog.json

Inside the Nix build the control-plane source and this script live in
different store paths; set LEMONSEED_CP_DIR to the control-plane source and
``--modules`` to the copied modules tree.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
sys.path.insert(0, os.environ.get("LEMONSEED_CP_DIR", str(ROOT / "control-plane")))

import modules_catalog  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--modules",
        type=Path,
        default=ROOT / "modules",
        help="the in-tree modules directory (default: %(default)s)",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        catalog = modules_catalog.build_catalog(args.modules)
    except modules_catalog.CatalogError as exc:
        print(f"build-modules-catalog: FAIL: {exc}", file=sys.stderr)
        return 2

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(args.out.suffix + ".tmp")
    tmp.write_text(
        json.dumps(catalog, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    tmp.replace(args.out)
    module_ids = sorted(catalog["modules"])
    print(f"build-modules-catalog: {len(module_ids)} module(s): {', '.join(module_ids)}")
    print(f"  wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
