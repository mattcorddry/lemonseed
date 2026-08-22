"""Minimal model catalog + VRAM planner for M5."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from hardware import max_gpu_vram_gb

DEFAULT_CATALOG = Path(__file__).resolve().parent / "catalog" / "models.json"


def catalog_path() -> Path:
    override = os.environ.get("LEMONSEED_CATALOG", "").strip()
    return Path(override) if override else DEFAULT_CATALOG


def load_catalog() -> dict[str, Any]:
    path = catalog_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"catalog_version": 0, "models": [], "error": str(exc)}


def _llamacpp_backend_state(hardware: dict[str, Any], backend: str) -> Optional[str]:
    """Read recipe backend state from proxied lemond /v1/system-info."""
    info = ((hardware.get("lemond") or {}).get("system_info") or {})
    backends = (
        ((info.get("recipes") or {}).get("llamacpp") or {}).get("backends") or {}
    )
    meta = backends.get(backend)
    if isinstance(meta, dict):
        state = meta.get("state")
        return str(state) if state is not None else None
    return None


def plan_model(hardware: dict[str, Any]) -> dict[str, Any]:
    """Pick the highest-ranked catalog entry that fits the largest single GPU."""
    catalog = load_catalog()
    models = catalog.get("models") or []
    vram = max_gpu_vram_gb(hardware)
    ram = float(hardware.get("ram_gb") or 0)
    cuda_state = _llamacpp_backend_state(hardware, "cuda")

    if vram <= 0:
        return {
            "state": "not-ready",
            "reason": "No NVIDIA GPU with usable VRAM was detected.",
            "selection": None,
            "catalog_version": catalog.get("catalog_version"),
        }
    if cuda_state in ("installable", "unsupported"):
        return {
            "state": "not-ready",
            "reason": f"llamacpp:cuda backend is '{cuda_state}', expected installed.",
            "selection": None,
            "catalog_version": catalog.get("catalog_version"),
            "max_vram_gb": vram,
        }

    ranked = sorted(
        (m for m in models if isinstance(m, dict)),
        key=lambda m: int(m.get("rank") or 0),
        reverse=True,
    )
    for model in ranked:
        need_v = float(model.get("min_vram_gb") or 0)
        need_r = float(model.get("min_ram_gb") or 0)
        if vram + 1e-6 >= need_v and ram + 1e-6 >= need_r:
            return {
                "state": "selected",
                "reason": (
                    f"Selected {model.get('display_name') or model.get('id')} "
                    f"(rank {model.get('rank')}) for {vram:g} GiB GPU VRAM "
                    f"and {ram:g} GiB system RAM."
                ),
                "selection": model,
                "catalog_version": catalog.get("catalog_version"),
                "max_vram_gb": vram,
            }

    return {
        "state": "not-ready",
        "reason": (
            f"No catalog model fits the largest GPU ({vram:g} GiB VRAM, "
            f"{ram:g} GiB RAM)."
        ),
        "selection": None,
        "catalog_version": catalog.get("catalog_version"),
        "max_vram_gb": vram,
    }
