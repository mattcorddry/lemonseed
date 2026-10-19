#!/usr/bin/env python3
"""Unit tests for the portal ownership view (M6.3b Phase 4A).

``owui_portal.build_view`` is pure: manifest intent + engine status + live
``configs/export`` -> the payload the admin portal renders. Two families of
assertions matter most here:

* **Leakage** — ``configs/export`` is unredacted, so a plaintext credential in
  the serialized view is a real exposure (the payload is built from it).
* **Trustworthy badges** — an admin decides what they can safely change in the
  Open WebUI admin UI based on these classifications and rollups, so the
  classification and grouping math must be exactly right.

No HTTP, no Open WebUI, no Postgres (AGENTS.md Tier A).
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Optional

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import owui_policy  # noqa: E402
import owui_portal  # noqa: E402

REAL_MANIFEST = CP / "owui-policy.json"

# A stand-in for Open WebUI 0.11.0's ``configs/export``: flat dotted keys, raw
# values, both managed and unmanaged namespaces present.
EXPORT: dict[str, Any] = {
    "openai.enable": True,
    "openai.api_base_urls": ["http://host.docker.internal/_lemonseed/lemond/v1"],
    "openai.api_keys": ["sk-PLAINTEXT"],
    "openai.api_configs": {
        # v0.11.0 shape: base url -> {"valves": {}}. Credentials live only in
        # openai.api_keys, so this value is expected to be shown unmasked.
        "http://host.docker.internal/_lemonseed/lemond/v1": {"valves": {}},
    },
    "ollama.enable": False,
    "web.search.enable": True,          # drifted: the manifest wants False
    "auth.enable_api_keys": True,
    "ui.default_models": ["lemonseed-ai"],
    # Keys Lemonseed does not manage (admin/seed territory) and a namespace we
    # have no opinion about at all.
    "web.ui_enabled": True,
    "tool_server.connections": [
        {"name": "Docs", "url": "https://tools.invalid", "bearer_token": "tok-PLAINTEXT"}
    ],
    "vector_db.chunks_per_collection": {"docs": 10},
    "audio.stt.engine": "whisper",
}


def status_with(entries: list[dict[str, Any]], counts: Optional[dict[str, int]] = None,
                **fields: Any) -> dict[str, Any]:
    """Engine status shaped like ``owui_policy.state_summary()``.

    ``counts`` are the run-level drift totals; the real engine derives them from
    the same entries, so a test that cares about the status strip should pass
    them rather than leaving them zero.
    """
    status: dict[str, Any] = {
        "ok": False,
        "state": "degraded",
        "manifest_version": 1,
        "policy_hash": "sha256:deadbeef",
        "owui_version": "0.11.0",
        "last_reconciled": "2026-10-06T04:00:00+00:00",
        "last_trigger": ["at_boot"],
        "last_error": None,
        "manifest_error": None,
        "key_ready": True,
        "lkg_at": "2026-10-06T04:00:00+00:00",
        "drift": {
            "ok": 0, "fixed": 0, "pending": 0, "error": 0, "drift": 0,
            **(counts or {}),
            "entries": entries,
        },
    }
    status.update(fields)
    return status


def real_manifest() -> owui_policy.Manifest:
    return owui_policy.load_manifest(REAL_MANIFEST)


def synthetic_manifest(
    settings: Optional[dict[str, owui_policy.SettingEntry]] = None,
    entities: Optional[dict[str, owui_policy.EntitySpec]] = None,
    guards: tuple[owui_policy.GuardSpec, ...] = (),
) -> owui_policy.Manifest:
    return owui_policy.Manifest(
        version=1,
        min_version="0.11.0",
        settings=settings or {},
        entities=entities or {},
        guards=guards,
        api={"config.export": "GET /api/v1/configs/export"},
        policy_hash="sha256:test",
    )


def setting_entry(
    key: str,
    mode: str = "managed",
    cadence: Optional[str] = "at_boot",
    spec: Optional[owui_policy.ValueSpec] = None,
) -> owui_policy.SettingEntry:
    return owui_policy.SettingEntry(
        key=key,
        spec=spec or owui_policy.ValueSpec("const", value=False),
        mode=mode,
        cadence=cadence,
        optional=False,
        reason="test",
    )


def guard(method: str, path: str, mode: str = "advertise") -> owui_policy.GuardSpec:
    return owui_policy.GuardSpec(
        method=method, path=path, regex=re.compile(re.escape(path)),
        mode=mode, reason="test",
    )


def rows_by_key(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["key"]: row for section in view["sections"] for row in section["entries"]}


def section_named(view: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in view["sections"] if s["section"] == name)


def entity_named(view: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(e for e in view["entities"] if e["kind"] == kind)


class ClassificationTests(unittest.TestCase):
    """The three modes must be visible exactly as the manifest declares them."""

    def setUp(self) -> None:
        self.view = owui_portal.build_view(real_manifest(), status_with([]), EXPORT)
        self.rows = rows_by_key(self.view)

    def test_managed_seed_and_unmanaged_keys_are_distinguished(self) -> None:
        self.assertEqual(self.rows["openai.api_base_urls"]["classification"], "managed")
        self.assertEqual(self.rows["openai.api_base_urls"]["label"], "Lemonseed manages")
        self.assertEqual(self.rows["ui.default_models"]["classification"], "initial_setup")
        self.assertEqual(self.rows["ui.default_models"]["label"], "Seeded once")
        self.assertEqual(self.rows["vector_db.chunks_per_collection"]["classification"], "yours")
        self.assertEqual(self.rows["vector_db.chunks_per_collection"]["label"], "Yours")

    def test_mode_and_cadence_are_carried_through(self) -> None:
        self.assertEqual(self.rows["web.search.enable"]["cadence"], "at_boot")
        # initial_setup entries have no cadence by contract (D3).
        self.assertIsNone(self.rows["ui.default_models"]["cadence"])
        self.assertIsNone(self.rows["vector_db.chunks_per_collection"]["mode"])

    def test_unmanaged_keys_carry_no_manifest_reason(self) -> None:
        self.assertEqual(self.rows["audio.stt.engine"]["reason"], "")
        self.assertTrue(self.rows["openai.enable"]["reason"])

    def test_totals_match_the_rendered_rows(self) -> None:
        counts = self.view["counts"]
        self.assertEqual(counts["config"], len(self.rows))
        self.assertEqual(
            counts["managed"] + counts["initial_setup"] + counts["yours"],
            counts["config"],
        )
        self.assertEqual(counts["config"], len(EXPORT) + len(
            set(real_manifest().settings) - set(EXPORT)))

    def test_manifest_key_missing_from_export_is_listed_as_unset(self) -> None:
        export = {k: v for k, v in EXPORT.items() if k != "web.search.enable"}
        view = owui_portal.build_view(real_manifest(), status_with([]), export)
        row = rows_by_key(view)["web.search.enable"]
        self.assertTrue(row["present"] is False)
        self.assertEqual(row["classification"], "managed")
        self.assertIsNone(row["found"])


class GroupingTests(unittest.TestCase):
    """Namespace grouping is what makes the tree browsable at all."""

    def setUp(self) -> None:
        self.view = owui_portal.build_view(real_manifest(), status_with([]), EXPORT)

    def test_namespace_is_the_top_level_segment(self) -> None:
        self.assertEqual(owui_portal.namespace_of("web.search.enable"), "web")
        self.assertEqual(owui_portal.namespace_of("ollama.enable"), "ollama")
        self.assertEqual(owui_portal.namespace_of("user.permissions"), "user")
        self.assertEqual(owui_portal.namespace_of("auth"), "auth")

    def test_related_keys_share_one_section(self) -> None:
        # web.search.enable (managed) + web.ui_enabled (the admin's) => one
        # "web" section, not one per key.
        web = section_named(self.view, "web")
        self.assertEqual(web["total"], 2)
        self.assertEqual({e["key"] for e in web["entries"]},
                         {"web.search.enable", "web.ui_enabled"})
        self.assertEqual(section_named(self.view, "openai")["total"], 4)

    def test_rollup_reflects_ownership_share(self) -> None:
        self.assertEqual(section_named(self.view, "web")["rollup"], "mixed")
        self.assertEqual(section_named(self.view, "openai")["rollup"], "ours")
        self.assertEqual(section_named(self.view, "vector_db")["rollup"], "yours")
        self.assertEqual(section_named(self.view, "audio")["rollup"], "yours")

    def test_titles_come_from_the_segment_table(self) -> None:
        self.assertEqual(section_named(self.view, "openai")["title"],
                         "OpenAI-compatible connections")
        self.assertEqual(section_named(self.view, "user")["title"], "Member permissions")

    def test_unknown_namespace_falls_back_to_its_own_name(self) -> None:
        view = owui_portal.build_view(
            synthetic_manifest({"zzz_widget.enable": setting_entry("zzz_widget.enable")}),
            status_with([]), {},
        )
        self.assertEqual(section_named(view, "zzz_widget")["title"], "zzz_widget")

    def test_pinned_keys_sort_first_within_their_section(self) -> None:
        order = [e["key"] for e in section_named(self.view, "openai")["entries"]]
        self.assertEqual(order[0], "openai.api_base_urls")
        self.assertIn("openai.enable", order[:4])
        # Sections are sorted by name so the tree order is stable across runs.
        self.assertEqual([s["section"] for s in self.view["sections"]],
                         sorted(s["section"] for s in self.view["sections"]))


class MaskingTests(unittest.TestCase):
    """``configs/export`` is raw; the view is the redaction point."""

    def setUp(self) -> None:
        self.view = owui_portal.build_view(real_manifest(), status_with([]), EXPORT)
        self.rows = rows_by_key(self.view)

    def test_no_plaintext_credential_survives_serialization(self) -> None:
        blob = json.dumps(self.view)
        self.assertNotIn("sk-PLAINTEXT", blob)
        self.assertNotIn("tok-PLAINTEXT", blob)

    def test_secret_setting_is_replaced_by_the_mask(self) -> None:
        row = self.rows["openai.api_keys"]
        self.assertEqual(row["found"], owui_portal.MASK)
        self.assertTrue(row["masked"])

    def test_structured_value_keeps_its_shape_but_loses_nested_secrets(self) -> None:
        row = self.rows["tool_server.connections"]
        found = row["found"]
        self.assertEqual(len(found), 1, "structure must stay readable")
        self.assertEqual(found[0]["name"], "Docs")
        self.assertEqual(found[0]["url"], "https://tools.invalid")
        self.assertEqual(found[0]["bearer_token"], owui_portal.MASK)
        self.assertTrue(row["masked"], "a masked leaf must flip the row to masked")

    def test_managed_connection_urls_are_visible_only_credentials_hidden(self) -> None:
        # api_configs is base-url -> valves: nothing secret in it, so an admin
        # can read the connection list in the portal without a mask getting in
        # the way.
        row = self.rows["openai.api_configs"]
        self.assertEqual(list(row["found"]),
                         ["http://host.docker.internal/_lemonseed/lemond/v1"])
        self.assertFalse(row["masked"])

    def test_unmanaged_secret_looking_keys_are_masked_too(self) -> None:
        # The heuristic covers keys we do not manage: an admin's own provider key
        # must not ride out in a portal payload.
        export = dict(EXPORT)
        export["service.external_api.token"] = "tok-OTHER"
        view = owui_portal.build_view(real_manifest(), status_with([]), export)
        self.assertEqual(rows_by_key(view)["service.external_api.token"]["found"],
                         owui_portal.MASK)
        self.assertNotIn("tok-OTHER", json.dumps(view))

    def test_flag_keys_named_like_secrets_pass_through(self) -> None:
        row = self.rows["auth.enable_api_keys"]
        self.assertIs(row["found"], True)
        self.assertFalse(row["masked"])


class DriftOverlayTests(unittest.TestCase):
    """The last drift report overlays onto rows (settings) and cards (entities)."""

    def setUp(self) -> None:
        entries = [
            {"key": "web.search.enable", "mode": "managed", "cadence": "at_boot",
             "state": "pending", "found": True, "want": False,
             "note": "drift seen on the audit tick"},
            {"key": "openai.api_keys", "mode": "managed", "cadence": "at_boot",
             "state": "error", "found": ["sk-PLAINTEXT"], "want": ["sk-PLAINTEXT"],
             "note": "import rejected"},
            {"key": "models:lemonseed-ai", "mode": "selective_management", "cadence": None,
             "state": "to_fix", "found": None, "want": None, "note": "owned model missing"},
            {"key": "users:ana@lemonseed.local", "mode": "full_management", "cadence": None,
             "state": "drift", "found": None, "want": None, "note": "not in Lemonseed"},
        ]
        self.view = owui_portal.build_view(
            real_manifest(),
            status_with(entries, counts={"pending": 1, "error": 1}),
            EXPORT,
        )
        self.rows = rows_by_key(self.view)

    def test_setting_drift_lands_on_its_row(self) -> None:
        row = self.rows["web.search.enable"]
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["state_label"], "drift seen, not corrected")
        self.assertEqual(row["note"], "drift seen on the audit tick")
        self.assertIs(row["want"], False)

    def test_drift_want_is_masked_for_secret_keys(self) -> None:
        row = self.rows["openai.api_keys"]
        self.assertEqual(row["want"], owui_portal.MASK)
        self.assertEqual(row["state"], "error")
        self.assertNotIn("sk-PLAINTEXT", json.dumps(self.view))

    def test_unobserved_rows_are_not_claimed_as_ok(self) -> None:
        row = self.rows["ollama.enable"]
        self.assertIsNone(row["state"])
        self.assertEqual(row["state_label"], "not observed yet")

    def test_entity_drift_routes_to_cards_not_config_rows(self) -> None:
        models = entity_named(self.view, "models")
        users = entity_named(self.view, "users")
        self.assertEqual(models["counts"]["to_fix"], 1)
        self.assertEqual([s["id"] for s in models["samples"]], ["lemonseed-ai"])
        self.assertEqual(users["counts"]["drift"], 1)
        self.assertEqual([s["id"] for s in users["samples"]], ["ana@lemonseed.local"])
        # ":"-keyed entity entries must never leak into the config tree.
        self.assertNotIn("models:lemonseed-ai", self.rows)

    def test_entity_card_exposes_ownership_metadata(self) -> None:
        models = entity_named(self.view, "models")
        self.assertEqual(models["mode"], "selective_management")
        self.assertEqual(models["own_ids"], ["lemonseed-ai", "lemonseed-ai-filtered"])
        self.assertEqual(models["reserved_name_regex"], "^Lemonseed.*$")
        self.assertEqual(entity_named(self.view, "users")["scope"],
                         # D-18: a CP-side rename now converges, so name is a
                         # managed attribute as well as membership and role.
                         ["membership", "is_admin", "name"])

    def test_policy_summary_is_carried_and_zero_filled(self) -> None:
        policy = self.view["policy"]
        self.assertEqual(policy["manifest_version"], 1)
        self.assertEqual(policy["owui_version"], "0.11.0")
        self.assertEqual(policy["drift_counts"]["pending"], 1)
        self.assertEqual(policy["drift_counts"]["ok"], 0)


class DegradationTests(unittest.TestCase):
    """The panel has to render during a restart and after a rejected manifest."""

    def test_missing_export_renders_intent(self) -> None:
        view = owui_portal.build_view(real_manifest(), status_with([]), None)
        self.assertFalse(view["live_available"])
        self.assertEqual(view["counts"]["config"], len(real_manifest().settings))
        rows = rows_by_key(view)
        self.assertTrue(all(not row["present"] for row in rows.values()))
        self.assertTrue(all(row["classification"] != "yours" for row in rows.values()))

    def test_empty_status_is_shaped_not_missing(self) -> None:
        view = owui_portal.build_view(real_manifest(), {}, EXPORT)
        self.assertFalse(view["policy"]["ok"])
        self.assertIsNone(view["policy"]["state"])
        self.assertEqual(view["policy"]["drift_counts"],
                         {"ok": 0, "fixed": 0, "pending": 0, "error": 0, "drift": 0})

    def test_null_status_is_tolerated(self) -> None:
        view = owui_portal.build_view(real_manifest(), None, EXPORT)
        self.assertTrue(view["ok"])

    def test_rejected_manifest_classifies_everything_as_yours(self) -> None:
        status = status_with([], manifest_error="manifest is not valid JSON")
        view = owui_portal.build_view(None, status, EXPORT)
        self.assertTrue(view["ok"])
        self.assertEqual(view["counts"]["managed"], 0)
        self.assertEqual(view["counts"]["initial_setup"], 0)
        self.assertEqual(view["counts"]["yours"], len(EXPORT))
        self.assertEqual(view["policy"]["manifest_error"], "manifest is not valid JSON")
        self.assertEqual(view["entities"], [])
        self.assertEqual(view["guards"], [])
        self.assertTrue(all(s["rollup"] == "yours" for s in view["sections"]))

    def test_no_manifest_and_no_export_is_empty_not_broken(self) -> None:
        view = owui_portal.build_view(None, None, None)
        self.assertFalse(view["live_available"])
        self.assertEqual(view["counts"]["config"], 0)
        self.assertEqual(view["sections"], [])


class GuardViewTests(unittest.TestCase):
    """``enforced`` must mean "the gateway will refuse this", not "we meant to".

    4B sources it from the CP's rendered state, so these tests pin the three
    states an admin can see (blocked / advisory / not-active-yet) and the
    fallback that keeps 4A's behaviour when no rendered state exists.
    """

    def test_in_tree_day_one_states(self) -> None:
        view = owui_portal.build_view(real_manifest(), status_with([]), EXPORT)
        self.assertTrue(view["guards"])
        enforced = {g["key"] for g in view["guards"] if g["enforced"]}
        # D-17b is complete as of M7 P3: create, edit and delete are all refused,
        # so the Open WebUI user panel is read-only and no guard is advisory.
        self.assertEqual(len(enforced), 7)
        self.assertEqual({g["key"] for g in view["guards"] if not g["enforced"]}, set())
        # guard_state=None means "manifest intent is the best answer we have".
        self.assertEqual(view["guard_summary"]["source"], "manifest")
        self.assertEqual(view["guard_summary"]["enforced"], 7)
        self.assertEqual(view["guard_summary"]["total"], 7)

    def test_enforce_mode_shows_as_blocked(self) -> None:
        manifest = synthetic_manifest(
            guards=(guard("POST", "/api/v1/auths/add", "enforce"),
                    guard("DELETE", "/api/v1/users/x")),
        )
        view = owui_portal.build_view(manifest, status_with([]), {})
        states = {g["key"]: g["enforced"] for g in view["guards"]}
        self.assertEqual(states, {"POST /api/v1/auths/add": True,
                                  "DELETE /api/v1/users/x": False})
        states = {g["key"]: g["guard_state"] for g in view["guards"]}
        self.assertEqual(states, {"POST /api/v1/auths/add": "enforced",
                                  "DELETE /api/v1/users/x": "advertised"})

    def test_rendered_state_is_the_source_of_truth(self) -> None:
        manifest = synthetic_manifest(
            guards=(guard("POST", "/a", "enforce"), guard("POST", "/b", "enforce"),
                    guard("POST", "/c", "advertise")),
        )
        guard_state = {
            "declared": 2,
            "declared_sha": "sha256:aaa",
            "rendered_sha": "sha256:aaa",
            "nginx_sha": "sha256:aaa",
            "in_sync": True,
            # Only /a is actually served; /b is declared but not in the file.
            "rendered": ["POST /a"],
            "pending": ["POST /b"],
            "enforced_count": 1,
            "pending_reason": "POST /b: placeholder %CP_OWUI_ID% is unresolved",
        }
        view = owui_portal.build_view(manifest, status_with([]), {},
                                      guard_state=guard_state)
        states = {g["key"]: g["guard_state"] for g in view["guards"]}
        self.assertEqual(states, {"POST /a": "enforced",
                                  "POST /b": "pending_service_identity",
                                  "POST /c": "advertised"})
        # Declared-enforce but not served => enforced must read False.
        self.assertEqual(
            {g["key"] for g in view["guards"]
             if g["mode"] == "enforce" and not g["enforced"]},
            {"POST /b"},
        )
        summary = view["guard_summary"]
        self.assertEqual(summary["source"], "rendered")
        self.assertEqual(summary["enforced"], 1)
        self.assertEqual(summary["declared"], 2)
        self.assertTrue(summary["in_sync"])
        self.assertEqual(summary["pending"], ["POST /b"])
        self.assertIn("unresolved", summary["pending_reason"])

    def test_out_of_sync_render_claims_nothing(self) -> None:
        """A file nginx never accepted must not be shown as protection."""
        manifest = synthetic_manifest(guards=(guard("POST", "/a", "enforce"),))
        guard_state = {
            "declared": 1, "declared_sha": "sha256:aaa", "rendered_sha": "sha256:aaa",
            # Reload failed: nginx is still serving something else.
            "nginx_sha": "sha256:old", "in_sync": False, "rendered": ["POST /a"],
            "pending": [], "enforced_count": 0, "pending_reason": None,
        }
        view = owui_portal.build_view(manifest, status_with([]), {},
                                      guard_state=guard_state)
        self.assertFalse(view["guards"][0]["enforced"])
        self.assertEqual(view["guards"][0]["guard_state"], "pending_service_identity")
        self.assertFalse(view["guard_summary"]["in_sync"])

    def test_manifest_render_error_is_visible(self) -> None:
        manifest = synthetic_manifest(guards=(guard("POST", "/a", "enforce"),))
        guard_state = {
            "declared": 0, "declared_sha": None, "rendered_sha": "sha256:old",
            "nginx_sha": "sha256:old", "in_sync": False, "rendered": [],
            "pending": [], "enforced_count": 0,
            "pending_reason": "guards: unknown keys ['blocked']",
        }
        view = owui_portal.build_view(manifest, status_with([]), {},
                                      guard_state=guard_state)
        self.assertFalse(view["guards"][0]["enforced"])
        self.assertIn("unknown keys", view["guard_summary"]["pending_reason"])

    def test_guard_cards_are_sorted_for_stable_rendering(self) -> None:
        view = owui_portal.build_view(real_manifest(), status_with([]), EXPORT)
        keys = [g["key"] for g in view["guards"]]
        self.assertEqual(keys, sorted(keys))


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
