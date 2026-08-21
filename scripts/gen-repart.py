#!/usr/bin/env python3
"""Generate systemd-repart drop-ins from partitions.yaml."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def parse_simple_yaml(text: str) -> dict:
    """Minimal YAML subset parser for our partitions.yaml (no external deps)."""
    # Prefer PyYAML when available.
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        pass

    # Fallback: enough for the current partitions.yaml shape.
    data: dict = {"partitions": []}
    current: dict | None = None
    in_partitions = False
    mount: dict | None = None

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        s = line.strip()

        if indent == 0 and ":" in s and not s.startswith("-"):
            key, _, val = s.partition(":")
            key = key.strip()
            val = val.strip()
            if key == "partitions":
                in_partitions = True
                current = None
                continue
            data[key] = val
            continue

        if not in_partitions:
            continue

        if s.startswith("- "):
            current = {}
            data["partitions"].append(current)
            mount = None
            rest = s[2:].strip()
            if ":" in rest:
                k, _, v = rest.partition(":")
                current[k.strip()] = _scalar(v.strip())
            continue

        if current is None:
            continue

        if s.startswith("mount:"):
            mount = {}
            current["mount"] = mount
            continue

        if ":" in s:
            k, _, v = s.partition(":")
            k, v = k.strip(), _scalar(v.strip())
            if mount is not None and indent >= 6:
                mount[k] = v
            else:
                current[k] = v
                mount = None

    return data


def _scalar(v: str):
    if v == "":
        return None
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def size_to_bytes(size: str | None) -> str | None:
    if size is None or size == "grow":
        return None
    size = str(size).strip().upper()
    units = {"B": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    if size[-1] in units:
        return str(int(float(size[:-1]) * units[size[-1]]))
    return size


# GPT type UUID for Linux LVM. systemd-repart has no "linux-lvm" identifier
# (only named aliases like esp / linux-generic), so emit the UUID.
LVM_TYPE_UUID = "E6D6D379-F507-44C2-A33C-2081B433AD87"


def role_type(part: dict) -> str:
    if "type" in part and part["type"]:
        t = str(part["type"]).lower()
        # Named aliases where systemd-repart documents them; otherwise UUID.
        mapping = {
            "ef00": "esp",
            "8e00": LVM_TYPE_UUID,
            "8300": "linux-generic",
        }
        return mapping.get(t, t)
    role = part.get("role", "")
    if role == "esp":
        return "esp"
    if role in ("lvm-pv", "user-data"):
        return LVM_TYPE_UUID
    if role == "squashfs-root":
        return "linux-generic"
    return "linux-generic"


def emit_repart(part: dict, index: int) -> str:
    name = part["name"]
    label = part.get("label", name)
    lines = [
        f"# Generated from partitions.yaml — do not edit by hand",
        f"[Partition]",
        f"Type={role_type(part)}",
        f"Label={label}",
    ]

    size = part.get("size")
    if size == "grow":
        lines.append("SizeMinBytes=64M")
        # Weight so grow partition takes remaining space.
        lines.append("Weight=1000")
    else:
        b = size_to_bytes(size)
        if b:
            lines.append(f"SizeMinBytes={b}")
            lines.append(f"SizeMaxBytes={b}")
        lines.append("Weight=0")

    fs = part.get("filesystem")
    if fs == "vfat":
        lines.append("Format=vfat")
    # squashfs-root and lvm-pv: no Format= (raw / pvcreate later)

    if part.get("role") == "esp":
        pass  # formatted vfat above; installer seeds GRUB files later (M4)

    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="partitions.yaml path",
    )
    ap.add_argument(
        "-o",
        "--out-dir",
        type=Path,
        required=True,
        help="output directory for *.conf",
    )
    args = ap.parse_args()

    root = Path(__file__).resolve().parent.parent
    cfg_path = args.config or (root / "partitions.yaml")
    data = parse_simple_yaml(cfg_path.read_text(encoding="utf-8"))
    parts = data.get("partitions") or []
    if not parts:
        print("no partitions found in", cfg_path, file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    # Remove old generated confs
    for old in args.out_dir.glob("*.conf"):
        old.unlink()

    for i, part in enumerate(parts):
        fname = f"{i * 10:02d}-{part['name']}.conf"
        path = args.out_dir / fname
        path.write_text(emit_repart(part, i), encoding="utf-8")
        print(f"wrote {path}")

    meta = args.out_dir / "README.md"
    meta.write_text(
        "# Generated systemd-repart definitions\n\n"
        f"Source: `{cfg_path.name}`\n\n"
        "Apply with: `make disk-repart DISK=/dev/sdX` (destructive).\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
