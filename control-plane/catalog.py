"""Model catalog, VRAM planner, and user model choice (M5 / M6.5)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from hardware import max_gpu_vram_gb

DEFAULT_CATALOG = Path(__file__).resolve().parent / "catalog" / "models.json"

# Durable user model choice (M6.5). Written by the control plane on user-data;
# absent = "automatic" (the planner picks the default family).
CHOICE_FILE = Path("/var/lib/lemonseed/model-choice.json")


def catalog_path() -> Path:
    override = os.environ.get("LEMONSEED_CATALOG", "").strip()
    return Path(override) if override else DEFAULT_CATALOG


def load_catalog() -> dict[str, Any]:
    path = catalog_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"catalog_version": 0, "models": [], "error": str(exc)}


def dir_key(name: str) -> str:
    """Comparison key for a ``models--<org>--<repo>`` directory name.

    The name on disk is minted by whatever downloaded the weights (lemond,
    using the *upstream* repo id), not by us — so a catalog ``checkpoint`` whose
    casing drifts from upstream is invisible to the download but fatal to the
    disk-space guard, whose protected/loaded sets are compared against real
    directory names. A miss there can evict the model we are about to download,
    or one lemond has resident. Comparing casefolded makes that class of drift
    harmless while the catalog stays the human-readable source of truth.
    """
    return str(name or "").casefold()


def model_dir_name(model_id: str, catalog: Optional[dict[str, Any]] = None) -> Optional[str]:
    """On-disk cache directory name for a catalog model id.

    lemond stores a pulled model under ``models--<org>--<repo>`` derived from the
    catalog entry's ``checkpoint`` (``<org>/<repo>:<variant>`` → strip the
    variant, case preserved, e.g. ``unsloth/gemma-4-E2B-it-GGUF:Q4_K_M`` →
    ``models--unsloth--gemma-4-E2B-it-GGUF``). The catalog id (e.g.
    ``Gemma-4-E2B-it-GGUF``) is *not* the directory name, so the disk-space
    guard must translate between the two. Returns None when the id has no
    checkpoint (caller falls back to the filesystem only).
    """
    cat = catalog if catalog is not None else load_catalog()
    for m in cat.get("models") or []:
        if isinstance(m, dict) and m.get("id") == model_id:
            checkpoint = str(m.get("checkpoint") or "").strip()
            if not checkpoint:
                return None
            base = checkpoint.split(":", 1)[0]
            parts = base.split("/", 1)
            if len(parts) == 2 and parts[0] and parts[1]:
                return f"models--{parts[0]}--{parts[1]}"
            return None
    return None


def read_model_choice() -> dict[str, Any]:
    try:
        raw = CHOICE_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {"family": "auto", "model_id": None}


def choice_exists() -> bool:
    """True once the user has made an explicit model choice (M6.5).

    An absent file means "no choice yet" (first boot) — distinct from the
    user explicitly picking "Automatic", which writes the file with
    ``family: "auto"``. The download gate in chat_ready waits on this so a
    fresh box shows the picker instead of auto-downloading the default.
    """
    try:
        return CHOICE_FILE.is_file()
    except OSError:
        return False


def write_model_choice(family: str, model_id: Optional[str] = None) -> dict[str, Any]:
    payload = {
        "family": family,
        "model_id": model_id,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        CHOICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CHOICE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o640)
        tmp.replace(CHOICE_FILE)
    except OSError as exc:
        print(f"model-choice persist: {exc}", flush=True)
    return payload


def default_family_id(catalog: Optional[dict[str, Any]] = None) -> str:
    """The family marked `default: true`, else the first family."""
    cat = catalog if catalog is not None else load_catalog()
    for family in cat.get("families") or []:
        if isinstance(family, dict) and family.get("default"):
            return str(family.get("id") or "")
    families = [f for f in (cat.get("families") or []) if isinstance(f, dict)]
    return str(families[0].get("id") or "") if families else ""


def family_label(
    family_id: str, catalog: Optional[dict[str, Any]] = None
) -> Optional[str]:
    """Friendly family label ("Google Gemma") for user-facing messages.

    Returns None when the family id is unknown so callers can fall back to a
    generic phrase rather than showing a raw catalog id.
    """
    cat = catalog if catalog is not None else load_catalog()
    for family in cat.get("families") or []:
        if isinstance(family, dict) and str(family.get("id") or "") == family_id:
            maker = str(family.get("maker") or "").strip()
            name = str(family.get("name") or "").strip()
            if maker and name:
                return f"{maker} {name}"
            return name or None
    return None


def models_for_family(family_id: str, catalog: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Catalog models of a family, best (highest rank) first."""
    cat = catalog if catalog is not None else load_catalog()
    models = [
        m
        for m in (cat.get("models") or [])
        if isinstance(m, dict) and str(m.get("family") or "") == family_id
    ]
    return sorted(models, key=lambda m: int(m.get("rank") or 0), reverse=True)


def _fits(model: dict[str, Any], vram: float, ram: float, vram_known: bool) -> bool:
    need_v = float(model.get("min_vram_gb") or 0)
    need_r = float(model.get("min_ram_gb") or 0)
    if vram_known:
        # GPU present: weights in VRAM, system RAM covers OS + KV spillover.
        return (vram + 1e-6 >= need_v) and (ram + 1e-6 >= need_r)
    # No GPU (CPU-only): weights + KV + OS all live in system RAM.
    return ram + 1e-6 >= need_v + need_r


def best_fit_model(
    family_id: str,
    hardware: dict[str, Any],
    catalog: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Largest model of a family that fits the biggest GPU + system RAM.

    Returns None when nothing in the family fits (the picker marks that
    family as unavailable for this hardware).
    """
    vram = max_gpu_vram_gb(hardware)
    ram = float(hardware.get("ram_gb") or 0)
    vram_known = vram > 0
    fitting = [
        m
        for m in models_for_family(family_id, catalog)
        if _fits(m, vram, ram, vram_known)
    ]
    if not fitting:
        return None
    # Lowest rank number = best model (rank 10 beats rank 45).
    return min(fitting, key=lambda m: int(m.get("rank") or 0))


def families_view(hardware: dict[str, Any]) -> list[dict[str, Any]]:
    """Picker payload: one entry per family with the best-fit model for this box."""
    catalog = load_catalog()
    choice = read_model_choice()
    out: list[dict[str, Any]] = []
    for family in catalog.get("families") or []:
        if not isinstance(family, dict):
            continue
        family_id = str(family.get("id") or "")
        best = best_fit_model(family_id, hardware, catalog)
        out.append(
            {
                **family,
                "models": models_for_family(family_id, catalog),
                "best_fit": best,
                "fits": best is not None,
                "selected": str(choice.get("family") or "auto") == family_id,
            }
        )
    return out


def resolve_plan_choice(
    hardware: dict[str, Any], catalog: Optional[dict[str, Any]] = None
) -> tuple[str, Optional[dict[str, Any]]]:
    """(family_id, explicit model) the planner should honor, or ("auto", None).

    An explicit model choice is honored only while it still fits; otherwise
    fall back to the automatic default family.
    """
    cat = catalog if catalog is not None else load_catalog()
    choice = read_model_choice()
    family = str(choice.get("family") or "auto")
    model_id = choice.get("model_id")
    if family and family != "auto":
        if isinstance(model_id, str) and model_id:
            for model in models_for_family(family, cat):
                if model.get("id") == model_id:
                    vram = max_gpu_vram_gb(hardware)
                    ram = float(hardware.get("ram_gb") or 0)
                    if _fits(model, vram, ram, vram > 0):
                        return family, model
        best = best_fit_model(family, hardware, cat)
        if best is not None:
            return family, best
        # Chosen family has nothing that fits — return the family id with no
        # explicit model so plan_model can report the fallback in `reason`.
        return family, None
    return "auto", None


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


def _detected_vendors(hardware: dict[str, Any]) -> set[str]:
    vendors = set()
    for gpu in hardware.get("gpus") or []:
        if isinstance(gpu, dict) and gpu.get("vendor"):
            vendors.add(str(gpu["vendor"]))
    return vendors


def select_backend(hardware: dict[str, Any]) -> str:
    """Pick the llamacpp backend to load with, from detected hardware.

    NVIDIA prefers CUDA; AMD prefers Vulkan (M6.4). The returned name is the
    lemond backend id (``cuda`` / ``vulkan`` / ``cpu``) used both for the
    planner gate and the ``lemonade load --llamacpp`` argument.
    """
    vendors = _detected_vendors(hardware)
    if "nvidia" in vendors:
        return "cuda"
    if "amd" in vendors:
        return "vulkan"
    return "cpu"


def plan_model(hardware: dict[str, Any]) -> dict[str, Any]:
    """Pick the highest-ranked catalog entry that fits the largest single GPU."""
    catalog = load_catalog()
    models = catalog.get("models") or []
    vram = max_gpu_vram_gb(hardware)
    ram = float(hardware.get("ram_gb") or 0)
    backend = select_backend(hardware)
    backend_state = _llamacpp_backend_state(hardware, backend)

    if backend_state in ("installable", "unsupported"):
        return {
            "state": "not-ready",
            "reason": f"llamacpp:{backend} backend is '{backend_state}', expected installed.",
            "selection": None,
            "catalog_version": catalog.get("catalog_version"),
            "max_vram_gb": vram,
        }

    # Best (lowest rank number) first — used for the cross-family fallback.
    ranked = sorted(
        (m for m in models if isinstance(m, dict)),
        key=lambda m: int(m.get("rank") or 0),
    )
    # When VRAM is unknown (vram == 0, e.g. an AMD Vulkan device that
    # vulkaninfo does not report VRAM for), skip the VRAM fit check and let
    # lemond decide at load time. We still rank by rank so the largest
    # reasonable model is tried first.
    vram_known = vram > 0
    family_id, explicit = resolve_plan_choice(hardware, catalog)
    if explicit is not None:
        return {
            "state": "selected",
            "reason": (
                f"Using user-selected {explicit.get('display_name') or explicit.get('id')} "
                f"(family {family_id})."
            ),
            "selection": explicit,
            "family": family_id,
            "catalog_version": catalog.get("catalog_version"),
            "max_vram_gb": vram,
        }
    # Track whether the user explicitly chose a family (vs. auto) so the
    # fallback reason can say "the chosen family has no model that fits".
    user_chose_family = family_id not in ("auto", default_family_id(catalog))
    if family_id == "auto":
        family_id = default_family_id(catalog)

    best = best_fit_model(family_id, hardware, catalog)
    if best is not None:
        if user_chose_family:
            reason = (
                f"Falling back to {best.get('display_name') or best.get('id')} "
                f"(family {family_id}) — the chosen family has no model that "
                f"fits this hardware."
            )
        else:
            if vram_known:
                reason = (
                    f"Selected {best.get('display_name') or best.get('id')} "
                    f"(family {family_id}) for {vram:g} GiB GPU VRAM "
                    f"and {ram:g} GiB system RAM."
                )
            else:
                reason = (
                    f"Selected {best.get('display_name') or best.get('id')} "
                    f"(family {family_id}) for CPU inference "
                    f"with {ram:g} GiB system RAM."
                )
        return {
            "state": "selected",
            "reason": reason,
            "selection": best,
            "family": family_id,
            "catalog_version": catalog.get("catalog_version"),
            "max_vram_gb": vram,
        }

    # Chosen (or default) family has nothing that fits — fall back to the
    # smallest catalog model overall (e.g. E2B on CPU-only / tiny GPUs).
    for model in ranked:
        if _fits(model, vram, ram, vram_known):
            return {
                "state": "selected",
                "reason": (
                    f"Falling back to {model.get('display_name') or model.get('id')} — "
                    f"no model from the {family_id} family fits this hardware."
                ),
                "selection": model,
                "family": str(model.get("family") or ""),
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
        "family": family_id,
        "catalog_version": catalog.get("catalog_version"),
        "max_vram_gb": vram,
    }
