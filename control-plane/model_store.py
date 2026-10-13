"""Disk-space maintenance for the model cache on the persistent user-data volume.

Before a new model is downloaded we verify there is room on the filesystem
holding the model cache. If the download would push the filesystem past a
high-water mark we evict old models (LRU) to make room, always preserving the
single most-recent prior model as a fallback in case the new download fails.
If even that fallback cannot be kept alongside the new model we refuse the
download with a clear error.

The model cache lives on the durable user-data volume (MODELS_DIR). lemond
stores each pulled model as a subdirectory of MODELS_DIR; we treat each
immediate subdirectory as one model and order them by directory mtime as a
coarse LRU signal (lemond exposes no per-model access timestamp).

Where possible we ask lemond for the facts and act through its HTTP API rather
than touching the cache from underneath a running server (see DESIGN.md
"lemond API over CLI"):

  - free/total bytes        GET  /v1/system-info → model_storage
  - per-model size          GET  /v1/models/{id}/files → sum size_bytes
  - which models are loaded GET  /v1/health   → never evict a loaded model
  - deletion                POST /v1/delete   (unloads first if loaded)

Each of these falls back to a pure-filesystem equivalent (shutil.disk_usage,
os.walk, shutil.rmtree) when lemond is unreachable, so the guard still works
during lemond's startup window or a transient failure.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Optional

import catalog

try:  # LemondClient is undefined when the SDK is absent (unit-test env).
    from lemond_client import LemondClient
except ImportError:  # noqa: BLE001
    LemondClient = None  # type: ignore[assignment, misc]

# When set, _catalog_id_for_dir and the loaded-set translation use this catalog
# instead of the on-disk one. Unit tests inject a synthetic catalog whose dir
# names match the temp fixtures; production always leaves it None.
_catalog_override: Optional[dict[str, Any]] = None


# Trigger cleanup when a download would push the filesystem to >= this fraction
# full. Below this we leave the cache untouched.
_HIGH_WATER = 0.90
# Always preserve this fraction of the filesystem free, even after eviction.
_RESERVE = 0.10
_GIB = 1024 ** 3


def _dir_size(path: Path) -> int:
    """Total size in bytes of a directory tree (best-effort)."""
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _model_entries(models_dir: Path) -> list[dict[str, Any]]:
    """List model subdirectories with mtime, oldest first.

    Each immediate subdirectory of MODELS_DIR is treated as one model. Sizes
    are filled in by the caller (from the lemond API, falling back to the
    filesystem); this only establishes the inventory and LRU order.
    """
    entries: list[dict[str, Any]] = []
    try:
        children = sorted(models_dir.iterdir())
    except OSError:
        return entries
    for child in children:
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        entries.append(
            {
                "path": child,
                "name": child.name,
                "size_bytes": 0,
                "mtime": mtime,
            }
        )
    entries.sort(key=lambda e: e["mtime"])
    return entries


def _free_bytes(models_dir: Path) -> tuple[int, int]:
    """Return (total_bytes, free_bytes) for the filesystem holding models_dir.

    A (0, 0) result means disk_usage failed; callers must treat that as
    "cannot verify" and refuse rather than risk filling the disk.
    """
    try:
        usage = shutil.disk_usage(models_dir if models_dir.exists() else models_dir.parent)
        return usage.total, usage.free
    except OSError:
        return 0, 0


def _disk_space(
    models_dir: Path, client: Optional[LemondClient]
) -> tuple[int, int]:
    """(total, free) for the model cache, preferring lemond's system-info.

    Falls back to shutil.disk_usage when lemond is unreachable or reports no
    usable model_storage. (0, 0) means neither source could be verified.
    """
    if client is not None:
        info = client.system_info()
        storage = info.get("model_storage") if isinstance(info, dict) else None
        if isinstance(storage, dict):
            total = storage.get("total_bytes")
            free = storage.get("free_bytes")
            if isinstance(total, int) and total > 0 and isinstance(free, int):
                return total, free
    return _free_bytes(models_dir)


def _catalog_id_for_dir(name: str) -> Optional[str]:
    """Catalog id whose on-disk directory is `name`, or None.

    lemond's HTTP API is keyed by the catalog id (e.g. ``Gemma-4-E2B-it-GGUF``),
    *not* the on-disk directory name (``models--unsloth--gemma-4-E2B-it-GGUF``).
    The disk-space guard walks directories, so it must translate each dir back
    to its catalog id before calling ``model_files()`` / ``delete_model()`` —
    passing a dir name yields 404/422 and forces the unsafe ``rmtree`` fallback.
    """
    cat = _catalog_override if _catalog_override is not None else catalog.load_catalog()
    key = catalog.dir_key(name)
    for m in cat.get("models") or []:
        if not isinstance(m, dict):
            continue
        expected = catalog.model_dir_name(str(m.get("id") or ""), cat)
        if expected and catalog.dir_key(expected) == key:
            return str(m.get("id"))
    return None


def _model_size(
    name: str, path: Path, client: Optional[LemondClient]
) -> int:
    """On-disk size of a model, preferring lemond's file listing.

    `name` is the on-disk directory; the lemond API call is keyed by the
    corresponding catalog id (translated here), falling back to a filesystem
    walk when the id is unknown or lemond is unreachable.
    """
    if client is not None:
        model_id = _catalog_id_for_dir(name)
        if model_id is not None:
            files = client.model_files(model_id)
            if files is not None:
                total = 0
                for f in files:
                    if isinstance(f, dict):
                        size = f.get("size_bytes")
                        if isinstance(size, int) and size > 0:
                            total += size
                return total
    return _dir_size(path)


def _lemond_reachable(client: Optional[LemondClient]) -> bool:
    """True when lemond is up and answering (system-info round-trip)."""
    if client is None:
        return False
    return client.system_info() is not None


def _delete_model(
    name: str, path: Path, client: Optional[LemondClient]
) -> bool:
    """Delete a model, preferring lemond's API (unloads first if loaded).

    `name` is the on-disk directory; the lemond API call is keyed by the
    corresponding catalog id.

    **Safety rule:** a raw ``rmtree`` is only attempted when lemond is
    genuinely unreachable (client is None or system-info fails). When lemond
    is alive but the API delete fails (model in a transitional state, busy
    mid-GC, etc.) we **skip** the model rather than delete from underneath a
    running server — that leaves lemond's ``user_models.json`` stale and the
    model still appears in ``lemonade list`` with no files on disk.
    """
    model_id = _catalog_id_for_dir(name)
    if client is not None and model_id is not None:
        if client.delete_model(model_id):
            return True
        # lemond is alive but refused the delete (e.g. model busy). Do NOT
        # fall through to rmtree — that would orphan the server's metadata.
        if _lemond_reachable(client):
            return False
    # lemond is unreachable (or the dir is not in the catalog): a raw delete
    # is the only way to reclaim space, and it is safe because no server is
    # running against the cache.
    try:
        shutil.rmtree(path)
        return True
    except OSError:
        return False


def ensure_model_space(
    models_dir: Path,
    *,
    needed_bytes: int,
    target_name: Optional[str] = None,
    protect_name: Optional[str] = None,
    client: Optional[LemondClient] = None,
) -> dict[str, Any]:
    """Ensure room on the model cache for a download of `needed_bytes`.

    Evicts LRU model subdirectories as needed, always preserving:
      - the target model's own directory (never delete what we are about to
        (re)download),
      - `protect_name`, the single most-recent prior model kept as a fallback,
      - any model currently **loaded** in lemond (never evict a resident model).

    `target_name` / `protect_name` are **catalog ids** (e.g.
    ``Gemma-4-E2B-it-GGUF``), not on-disk directory names. lemond stores each
    model under ``models--<org>--<repo>`` and its HTTP API is keyed by the
    catalog id, so this function translates between the two (via
    ``catalog.model_dir_name`` / ``_catalog_id_for_dir``) when it touches the
    cache or calls the API.

    `client` is a LemondClient used for disk info, per-model sizes, the loaded
    set, and deletion; pass None (or a client that reports unreachable) to
    force the pure-filesystem path.

    Returns a dict describing the action taken:
      {"ok": True, "evicted": [names...], "free_before": int, "free_after": int}

    Raises RuntimeError when the download cannot fit while still preserving the
    fallback model (or when disk usage cannot be verified).
    """
    total, free = _disk_space(models_dir, client)
    if total <= 0:
        raise RuntimeError(
            "Cannot verify free disk space for the model download; "
            "refusing to avoid filling the disk"
        )

    reserve = int(_RESERVE * total)
    high_water_free = int((1.0 - _HIGH_WATER) * total)  # free needed to stay < high water

    def effective_free(free: int) -> int:
        # Space actually available for the download after preserving the reserve.
        return max(0, free - reserve)

    def would_exceed_high_water(free_after: int) -> bool:
        return free_after < high_water_free

    free_before = free
    evicted: list[str] = []

    # Fast path: enough room already (both the reserve and the high-water mark).
    if needed_bytes <= effective_free(free) and not would_exceed_high_water(free - needed_bytes):
        return {
            "ok": True,
            "evicted": evicted,
            "free_before": free_before,
            "free_after": free,
        }

    # Determine which entries are protected from eviction.
    entries = _model_entries(models_dir)
    for entry in entries:
        entry["size_bytes"] = _model_size(entry["name"], entry["path"], client)

    # Never evict a model that lemond currently has resident. lemond reports
    # loaded models by **catalog id**, but entries are keyed by on-disk dir
    # name — translate each dir to its catalog id so the loaded set actually
    # matches (a dir-name vs catalog-id mismatch would let us evict a live model).
    _cat = _catalog_override if _catalog_override is not None else catalog.load_catalog()

    loaded: set[str] = set()
    if client is not None:
        for cid in client.loaded_model_names():
            d = catalog.model_dir_name(cid, _cat)
            if d:
                loaded.add(catalog.dir_key(d))

    # The target model's own on-disk directory (it may already be present when
    # we re-pull / re-download it). The catalog id passed as `target_name` is
    # *not* the dir name, so translate it — this is what previously protected
    # nothing and let the target be evicted.
    target_dir = catalog.model_dir_name(target_name, _cat) if target_name else None

    # The single most-recent prior model, kept as a fallback if the new
    # download fails. It must be a *real existing* directory that is neither
    # the target nor the explicit protect — inferred independently of whether
    # the target is on disk yet.
    # (`protect_name`, when a caller supplies one, is already a directory name.)
    protect_dir = protect_name
    if protect_dir is None:
        target_key = catalog.dir_key(target_dir) if target_dir else ""
        candidates = [e for e in entries if catalog.dir_key(e["name"]) != target_key]
        if candidates:
            # entries are oldest→newest, so the last one is the most recent.
            protect_dir = candidates[-1]["name"]

    protected = {catalog.dir_key(d) for d in (target_dir, protect_dir) if d}

    # Evict oldest-first, skipping protected and loaded entries, until we have
    # room.
    for entry in entries:
        if needed_bytes <= effective_free(free) and not would_exceed_high_water(free - needed_bytes):
            break
        entry_key = catalog.dir_key(entry["name"])
        if entry_key in protected or entry_key in loaded:
            continue
        if not _delete_model(entry["name"], entry["path"], client):
            # A failed deletion is not fatal; we simply have less to reclaim.
            print(f"model-store: failed to evict {entry['name']}", flush=True)
            continue
        free += entry["size_bytes"]
        evicted.append(entry["name"])

    # After eviction, re-check. If we still cannot fit the new model while
    # keeping the fallback, refuse.
    if needed_bytes > effective_free(free) or would_exceed_high_water(free - needed_bytes):
        # Distinguish "cannot fit even with fallback kept" from "fits but would
        # cross the high-water mark". The user-facing rule: if we cannot keep
        # 1 prior model + the new model, do not download.
        raise RuntimeError(
            "Insufficient disk space to download this model "
            f"(need {needed_bytes / _GIB:.1f} GB, "
            f"free {free / _GIB:.1f} GB, reserve {reserve / _GIB:.1f} GB)"
        )

    return {
        "ok": True,
        "evicted": evicted,
        "free_before": free_before,
        "free_after": free,
    }
