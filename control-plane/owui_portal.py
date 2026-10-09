"""Ownership view of the Open WebUI config tree for the admin portal (M6.3b 4A).

Pure join, no I/O: manifest intent + live ``configs/export`` + the engine's last
drift report -> the structure the portal renders. Keeping it pure means the
classification math is unit-testable without a running Open WebUI, and the HTTP
handler stays a fetch-and-serialize shim.

The point of the view is *visible ownership*: an admin browsing Open WebUI's
settings can see which keys Lemonseed manages, which it seeded once and will not
touch again, and which are entirely the admin's — plus how well enforcement is
going on the managed ones. Values are masked, because this payload is a status
surface, not a credentials viewer: ``configs/export`` is unredacted, so masking
here is the only thing between an admin's API keys and the response body.
"""

from __future__ import annotations

from typing import Any, Optional

from owui_policy import MASK, is_secret_key, mask_setting

# Classifications (badge ids; the portal CSS/JS and the smoke test key off these).
CLASS_MANAGED = "managed"          # Lemonseed re-asserts this every boot
CLASS_SEED = "initial_setup"       # seeded once, never touched again
CLASS_YOURS = "yours"              # not in the manifest: the admin's own

ROLLUP_OURS = "ours"
ROLLUP_MIXED = "mixed"
ROLLUP_YOURS = "yours"

_CLASS_LABELS = {
    CLASS_MANAGED: "Lemonseed manages",
    CLASS_SEED: "Seeded once",
    CLASS_YOURS: "Yours",
}

_STATE_LABELS = {
    "ok": "matches intent",
    "fixed": "corrected this run",
    "pending": "drift seen, not corrected",
    "to_fix": "will be corrected",
    "drift": "outside our intent",
    "error": "could not be applied",
    None: "not observed yet",
}

# Keys the view always shows first inside a section, so the interesting ones
# surface before the long permission leaf list.
_PINNED = (
    "openai.api_base_urls",
    "openai.api_configs",
    "openai.api_keys",
    "openai.enable",
    "ollama.enable",
    "direct.enable",
    "web.search.enable",
    "image_generation.enable",
    "images.edit.enable",
    "auth.enable_api_keys",
    "ui.enable_signup",
    "ui.enable_community_sharing",
)

# Namespace (first dotted segment) -> human title. Keys here are the segments
# Open WebUI 0.11.0 actually uses; unknown segments fall back to their own name
# so a new upstream namespace still renders.
_SEGMENT_TITLES = {
    "audio": "Audio",
    "auth": "Authentication & API keys",
    "automations": "Automations",
    "channels": "Channels",
    "code_execution": "Code execution",
    "direct": "Direct connections",
    "docs": "Documents",
    "image_generation": "Image generation",
    "images": "Images",
    "memories": "Memories",
    "model": "Model defaults",
    "models": "Models",
    "note": "Notes",
    "notes": "Notes",
    "ollama": "Ollama",
    "openai": "OpenAI-compatible connections",
    "rag": "Retrieval (RAG)",
    "rate_limiting": "Rate limiting",
    "s3": "Object storage",
    "service": "External services",
    "task": "Background tasks",
    "tool_server": "Tool servers",
    "tts": "Text-to-speech",
    "ui": "Interface & sharing",
    "user": "Member permissions",
    "vector_db": "Vector database",
    "web": "Web access",
    "workplace": "Workplace",
}


def namespace_of(key: str) -> str:
    """Ownership granularity: Open WebUI's own top-level config namespace.

    ``web.search.enable`` and ``web.ui_enabled`` both land in ``web``. Two
    segments would be finer, but it shreds the small namespaces
    (``ollama.enable``, ``automations.enable``, …) into one-row groups, which is
    the opposite of browsable — and Open WebUI's admin UI itself groups by this
    same first segment.
    """
    return str(key or "").split(".", 1)[0]


def _classification(mode: Optional[str]) -> str:
    if mode == "managed":
        return CLASS_MANAGED
    if mode == "initial_setup":
        return CLASS_SEED
    return CLASS_YOURS


def _rollup(counts: dict[str, int]) -> str:
    ours = counts.get(CLASS_MANAGED, 0) + counts.get(CLASS_SEED, 0)
    if ours == 0:
        return ROLLUP_YOURS
    if counts.get(CLASS_YOURS, 0) == 0:
        return ROLLUP_OURS
    return ROLLUP_MIXED


def _entry_class(manifest: Any, key: str) -> tuple[str, dict[str, Any]]:
    """Classification + display metadata for one key, from the manifest."""
    entry = getattr(manifest, "settings", {}).get(key) if manifest else None
    if entry is None:
        return CLASS_YOURS, {}
    spec = entry.spec
    return _classification(entry.mode), {
        "mode": entry.mode,
        "cadence": entry.cadence,
        "optional": entry.optional,
        "reason": entry.reason,
        "spec": spec,
        "secret": is_secret_key(entry.key, spec),
    }


def _policy_summary(status: dict[str, Any]) -> dict[str, Any]:
    """Engine state for the view's status strip (never empty-shaped)."""
    status = status or {}
    drift = status.get("drift") or {}
    return {
        "ok": bool(status.get("ok")),
        "state": status.get("state"),
        "manifest_version": status.get("manifest_version"),
        "policy_hash": status.get("policy_hash"),
        "owui_version": status.get("owui_version"),
        "last_reconciled": status.get("last_reconciled"),
        "last_trigger": status.get("last_trigger"),
        "last_error": status.get("last_error"),
        "manifest_error": status.get("manifest_error"),
        "key_ready": status.get("key_ready"),
        "lkg_at": status.get("lkg_at"),
        "drift_counts": {
            k: drift.get(k, 0) for k in ("ok", "fixed", "pending", "error", "drift")
        },
    }


def _config_entries(manifest: Any, export: Optional[dict], drift_index: dict[str, dict]
                    ) -> list[dict[str, Any]]:
    """One row per config key: manifest keys plus every live Open WebUI key.

    A manifest key missing from the export is still shown (``present=False``):
    "we intend to set this and Open WebUI has no value for it" is information,
    not a hole in the tree.
    """
    live = export if isinstance(export, dict) else {}
    wanted = set(getattr(manifest, "settings", {}) or {}) if manifest else set()
    rows: list[dict[str, Any]] = []
    for key in wanted | set(live):
        classification, meta = _entry_class(manifest, key)
        report = drift_index.get(key) or {}
        present = key in live
        spec = meta.get("spec")
        raw = live.get(key) if present else None
        found = mask_setting(key, raw, spec) if present else None
        want = report.get("want")
        if want is not None:
            want = mask_setting(key, want, spec)
        rows.append({
            "type": "config",
            "key": key,
            "namespace": namespace_of(key),
            "classification": classification,
            "label": _CLASS_LABELS[classification],
            "present": present,
            "found": found,
            "want": want,
            "state": report.get("state"),
            "state_label": _STATE_LABELS.get(report.get("state"), report.get("state")),
            "note": report.get("note") or "",
            # "anything hidden" — either the whole value or a leaf inside a
            # structured one (openai.api_configs keeps its shape, loses its key).
            "masked": bool(meta.get("secret")) or (present and found != raw),
            "reason": meta.get("reason", ""),
            "mode": meta.get("mode"),
            "cadence": meta.get("cadence"),
            "optional": meta.get("optional", False),
        })
    rows.sort(key=lambda r: (_PINNED.index(r["key"]) if r["key"] in _PINNED else len(_PINNED),
                             r["key"]))
    return rows


def _entities(manifest: Any, drift_index: dict[str, dict]) -> list[dict[str, Any]]:
    """Entity cards (models/users) with counts from ``kind:id`` drift entries."""
    specs = getattr(manifest, "entities", {}) or {} if manifest else {}
    cards: list[dict[str, Any]] = []
    for kind, spec in sorted(specs.items()):
        counts = {"ok": 0, "fixed": 0, "pending": 0, "to_fix": 0, "drift": 0, "error": 0}
        samples: list[dict[str, Any]] = []
        for key, report in sorted(drift_index.items()):
            if not key.startswith(f"{kind}:"):
                continue
            state = str(report.get("state") or "ok")
            counts[state] = counts.get(state, 0) + 1
            if len(samples) < 12:
                samples.append({
                    "id": key.split(":", 1)[1],
                    "state": state,
                    "state_label": _STATE_LABELS.get(state, state),
                    "note": report.get("note") or "",
                })
        config = getattr(spec, "config", {}) or {}
        cards.append({
            "type": "entity",
            "kind": kind,
            "mode": getattr(spec, "mode", None),
            "classification": CLASS_MANAGED,
            "reason": getattr(spec, "reason", ""),
            "own_ids": config.get("own_ids") or [],
            "reserved_name_regex": config.get("reserved_name_regex"),
            "scope": config.get("scope") or [],
            "counts": counts,
            "samples": samples,
        })
    return cards


def _guards(manifest: Any, guard_state: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Guard cards.

    ``enforced`` answers "will the gateway actually refuse this?". With
    ``guard_state`` (the CP's rendered truth) it is sourced from the file nginx
    serves, so a guard the CP could not render — typically
    ``%CP_OWUI_ID%`` before the service account exists — reports
    ``state: "pending_service_identity"`` instead of claiming protection it does
    not provide. Without it (unit tests, CP mid-restart) we fall back to the
    manifest's intent, which is the best available answer and is labelled
    honestly by ``enforced_source``.
    """
    rendered = set((guard_state or {}).get("rendered") or ())
    serving = bool((guard_state or {}).get("in_sync"))
    out: list[dict[str, Any]] = []
    for guard in (getattr(manifest, "guards", ()) or ()):
        key = f"{guard.method} {guard.path}"
        declared = guard.mode == "enforce"
        if guard_state is None:
            enforced = declared
            state = "enforced" if enforced else "advertised"
        else:
            # Both conditions, deliberately: owui_guard_state() already blanks
            # ``rendered`` when the hashes disagree, and re-checking here means
            # the view cannot claim protection even if that gating changes.
            enforced = serving and key in rendered
            if not declared:
                state = "advertised"
            elif enforced:
                state = "enforced"
            else:
                state = "pending_service_identity"
        out.append({
            "type": "guard",
            "key": key,
            "method": guard.method,
            "path": guard.path,
            "mode": guard.mode,
            "enforced": enforced,
            "guard_state": state,
            "reason": guard.reason,
        })
    out.sort(key=lambda g: g["key"])
    return out


def _guards_summary(manifest: Any, guard_state: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Declared-vs-served summary for the guard card's hash line (T4-B)."""
    total = len(getattr(manifest, "guards", ()) or ())
    if guard_state is None:
        declared = sum(1 for g in (getattr(manifest, "guards", ()) or ())
                       if g.mode == "enforce")
        return {
            "source": "manifest",
            "declared": declared,
            "enforced": declared,
            "total": total,
            "in_sync": True,
            "declared_sha": None,
            "rendered_sha": None,
            "nginx_sha": None,
            "pending": [],
            "pending_reason": None,
        }
    return {
        "source": "rendered",
        "declared": guard_state.get("declared", 0),
        "enforced": guard_state.get("enforced_count", 0),
        "total": total,
        "in_sync": bool(guard_state.get("in_sync")),
        "declared_sha": guard_state.get("declared_sha"),
        "rendered_sha": guard_state.get("rendered_sha"),
        "nginx_sha": guard_state.get("nginx_sha"),
        "pending": list(guard_state.get("pending") or ()),
        "pending_reason": guard_state.get("pending_reason"),
    }


def build_view(manifest: Any, status: Optional[dict[str, Any]],
               export: Optional[dict[str, Any]],
               guard_state: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Render the ownership view.

    ``manifest`` may be ``None`` (unreadable manifest: the view degrades to the
    live export, everything classified ``yours``, with ``policy.manifest_error``
    set). ``export`` may be ``None`` (Open WebUI unreachable): the tree still
    renders from intent, with ``live_available=False`` so the copy can say so.
    ``guard_state`` (``modules.owui_guard_state()``) is what makes "enforced"
    mean *served*; see ``_guards``.
    """
    status = status or {}
    drift = status.get("drift") or {}
    drift_index = {
        str(e.get("key")): e for e in (drift.get("entries") or []) if isinstance(e, dict)
    }
    live_available = isinstance(export, dict)

    rows = _config_entries(manifest, export if live_available else None, drift_index)

    sections: dict[str, dict[str, Any]] = {}
    for row in rows:
        namespace = row["namespace"]
        section = sections.setdefault(namespace, {
            "section": namespace,
            "title": _SEGMENT_TITLES.get(namespace, namespace),
            "counts": {CLASS_MANAGED: 0, CLASS_SEED: 0, CLASS_YOURS: 0},
            "total": 0,
            "entries": [],
        })
        section["counts"][row["classification"]] = (
            section["counts"].get(row["classification"], 0) + 1
        )
        section["total"] += 1
        section["entries"].append(row)
    section_list = [sections[name] for name in sorted(sections)]
    for section in section_list:
        section["rollup"] = _rollup(section["counts"])

    totals = {CLASS_MANAGED: 0, CLASS_SEED: 0, CLASS_YOURS: 0}
    for row in rows:
        totals[row["classification"]] = totals.get(row["classification"], 0) + 1

    return {
        "ok": True,
        "live_available": live_available,
        "policy": _policy_summary(status),
        "counts": {"config": len(rows), **totals},
        "sections": section_list,
        "entities": _entities(manifest, drift_index),
        "guards": _guards(manifest, guard_state),
        "guard_summary": _guards_summary(manifest, guard_state),
    }
