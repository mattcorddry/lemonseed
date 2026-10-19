#!/usr/bin/env python3
"""Unit tests for the owui_policy engine (manifest, planner, handlers, LKG).

Pure logic + mocked db/client only — no HTTP, no Postgres (AGENTS.md Tier A).
The real in-tree ``owui-policy.json`` is itself a fixture: if the shipped
manifest stops loading or stops matching the appliance intent, these fail.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import owui_policy  # noqa: E402

REAL_MANIFEST = CP / "owui-policy.json"

BOOT_TRIGGERS = frozenset({"at_boot"})
AUDIT_TRIGGERS: frozenset[str] = frozenset()


def load_real_manifest() -> owui_policy.Manifest:
    return owui_policy.load_manifest(REAL_MANIFEST)


def manifest_with(
    settings: Optional[dict[str, owui_policy.SettingEntry]] = None,
    entities: Optional[dict[str, owui_policy.EntitySpec]] = None,
    guards: tuple = (),
) -> owui_policy.Manifest:
    return owui_policy.Manifest(
        version=1,
        min_version="0.11.0",
        settings=settings or {},
        entities=entities or {},
        guards=guards,
        api={"config.import": "POST /api/v1/configs/import"},
        policy_hash="sha256:test",
    )


def guard(
    method: str = "POST",
    path: str = "/api/v1/configs/connections",
    mode: str = "enforce",
    reason: str = "test reason",
    hint: str = "",
) -> owui_policy.GuardSpec:
    """GuardSpec for renderer tests (the loader is tested separately)."""
    return owui_policy.GuardSpec(
        method=method,
        path=path,
        regex=re.compile(path),
        mode=mode,
        reason=reason,
        hint=hint,
    )


def setting(
    key: str,
    mode: str = "managed",
    cadence: Optional[str] = "at_boot",
    value: Any = "want",
) -> owui_policy.SettingEntry:
    return owui_policy.SettingEntry(
        key=key,
        spec=owui_policy.ValueSpec("const", value=value),
        mode=mode,
        cadence=cadence,
        optional=False,
        reason="test",
    )


def models_spec(
    mode: str = "selective_management",
    own_ids: Optional[list[str]] = None,
    definitions: Optional[dict[str, dict]] = None,
) -> owui_policy.EntitySpec:
    handler = owui_policy.ENTITY_KINDS["models"]
    config = handler.validate(
        mode,
        {
            "own_by": "id",
            "own_ids": own_ids or ["lemonseed-ai"],
            "reserved_name_regex": "^Lemonseed.*$",
            "definitions": definitions or {},
        },
    )
    return owui_policy.EntitySpec(kind="models", mode=mode, config=config, reason="t")


def users_spec(scope: Optional[list[str]] = None) -> owui_policy.EntitySpec:
    handler = owui_policy.ENTITY_KINDS["users"]
    config = handler.validate(
        "full_management",
        {
            # Mirrors the shipped manifest's scope (D-18: name joined the set).
            "source": "cp.users",
            "scope": scope or ["membership", "is_admin", "name"],
            "never_touch": ["%SERVICE_ACCOUNT_ID%"],
        },
    )
    return owui_policy.EntitySpec(
        kind="users", mode="full_management", config=config, reason="t"
    )


def resolved_map(
    manifest: owui_policy.Manifest, ctx: owui_policy.ResolverContext
) -> dict[str, tuple[Any, Optional[str]]]:
    return {
        key: owui_policy.resolve_setting_value(entry, ctx)
        for key, entry in manifest.settings.items()
    }


class _FakeClient:
    """Records client.call() usage; replies from a name -> result map."""

    def __init__(self, responses: Optional[dict[str, Any]] = None) -> None:
        self.responses = responses or {}
        self.calls: list[dict[str, Any]] = []

    def call(
        self,
        name: str,
        *,
        path_params: Optional[dict[str, str]] = None,
        json_body: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append(
            {"name": name, "path_params": path_params, "json_body": json_body}
        )
        reply = self.responses.get(name, {"ok": True, "status": 200, "data": None, "error": None})
        if callable(reply):
            reply = reply(name, path_params, json_body)
        return {"ok": True, "status": 200, "data": reply, "error": None}


# ---------------------------------------------------------------------------
# Manifest loader / validator
# ---------------------------------------------------------------------------


class TestManifest(unittest.TestCase):
    def test_in_tree_manifest_loads(self) -> None:
        manifest = load_real_manifest()
        self.assertEqual(manifest.version, 1)
        self.assertEqual(manifest.min_version, "0.11.0")
        self.assertTrue(manifest.policy_hash.startswith("sha256:"))
        self.assertIn("ollama.enable", manifest.settings)
        self.assertIn("user.permissions", manifest.settings)
        self.assertEqual(set(manifest.entities), {"models", "users"})
        self.assertEqual(manifest.entities["models"].mode, "selective_management")
        self.assertEqual(manifest.entities["users"].mode, "full_management")
        self.assertTrue(manifest.guards)
        # Phase 4B day-one enforcement table. Stated as an explicit split rather
        # than "all of one mode": flipping a guard in either direction, or adding
        # one that defaults to advertise, has to be a deliberate edit that fails
        # here first.
        enforced = {f"{g.method} {g.path}" for g in manifest.guards if g.mode == "enforce"}
        advertised = {f"{g.method} {g.path}" for g in manifest.guards if g.mode == "advertise"}
        self.assertEqual(
            enforced,
            {
                "POST /api/v1/configs/connections",
                "POST /openai/config/update",
                "POST /ollama/config/update",
                # D-17b: OWUI's user panel is read-only. One path regex covers
                # every account's email/role/password edit (it is the only route
                # that can change any of the three), and the delete rule covers
                # every account; the CP account keeps its own rule because its
                # denial copy reads differently. The update id segment is
                # ``[^/]+``, not ``.*``: the greedy form also swallows Open
                # WebUI's self-service writes under the same prefix, which D-17a
                # says it owns.
                "POST /api/v1/users/[^/]+/update",
                "DELETE /api/v1/users/.*",
                "DELETE /api/v1/users/%CP_OWUI_ID%",
                # D-17b, finished by M7 P3: creating an account in Open WebUI is
                # refused too, now that the portal's People panel is the way to
                # do it. Nothing in the OWUI user panel can mutate anything.
                "POST /api/v1/auths/add",
            },
        )
        # No guard is advisory. Each one was flipped when the thing that made it
        # safe arrived, so an advisory guard here now has to name what it waits
        # for, or a reader cannot tell "not yet" from "never".
        self.assertEqual(advertised, set())
        # Any guard still advisory has to say what it waits for. None is today:
        # the create rule was the last one, and M7 P3's People panel retired the
        # wait (D-17b).
        for spec in manifest.guards:
            if spec.mode == "advertise":
                self.assertIn("until", spec.reason.lower(), spec.path)
        # Every guard ships UI copy that is safe to interpolate into the nginx
        # denial body.
        for spec in manifest.guards:
            self.assertTrue(spec.hint, f"{spec.path} has no hint")
            self.assertTrue(owui_policy._GUARD_HINT_RE.match(spec.hint), spec.path)
        for logical in ("config.export", "config.import", "models.export", "users.all",
                        "users.create", "users.update", "users.delete",
                        "apikey.create", "app.version"):
            self.assertIn(logical, manifest.api)
        # Every api entry widens the CP key's allowed_endpoints, so an unused
        # entry is dead attack surface. The six groups.* routes were deleted
        # (PLAN.md Q4): group membership rides on X-Forwarded-Groups, never on
        # an OWUI write.
        self.assertFalse([name for name in manifest.api if name.startswith("groups.")])
        self.assertFalse([route for route in manifest.api.values() if "/groups" in route])
        # D-18: the mirror manages the display name too (email stays unmanaged).
        self.assertEqual(manifest.entities["users"].config["scope"],
                         ["membership", "is_admin", "name"])

    def test_in_tree_manifest_matches_appliance_intent(self) -> None:
        """Values cross-checked against the Phase-2 env-effective set (§8)."""
        settings = load_real_manifest().settings
        self.assertIs(settings["ollama.enable"].spec.value, False)
        self.assertIs(settings["openai.enable"].spec.value, True)
        self.assertEqual(settings["openai.api_base_urls"].spec.kind, "derived")
        self.assertEqual(settings["openai.api_base_urls"].spec.name, "lemond_base")
        self.assertTrue(settings["openai.api_base_urls"].spec.wrap_list)
        self.assertEqual(settings["openai.api_keys"].spec.kind, "secret")
        self.assertEqual(settings["openai.api_keys"].spec.name, "OPENAI_API_KEY")
        params = settings["models.default_params"].spec.value
        self.assertEqual(params["function_calling"], "legacy")
        self.assertIn("Lemonseed", params["system"])
        perms = settings["user.permissions"].spec.value
        self.assertTrue(perms["features"]["api_keys"])
        self.assertTrue(perms["features"]["automations"])
        self.assertFalse(perms["features"]["channels"])
        self.assertTrue(perms["features"]["notes"])
        self.assertTrue(perms["features"]["memories"])
        self.assertFalse(perms["workspace"]["models"])
        self.assertFalse(perms["workspace"]["skills"])
        self.assertTrue(perms["chat"]["controls"])
        self.assertTrue(perms["settings"]["interface"])
        for key in ("ui.default_models", "models.default_metadata",
                    "models.default_params"):
            self.assertEqual(settings[key].mode, "initial_setup", key)
            self.assertIsNone(settings[key].cadence, key)
        for key in ("ollama.enable", "openai.enable", "web.search.enable",
                    "image_generation.enable", "images.edit.enable",
                    "ui.enable_community_sharing", "auth.enable_api_keys",
                    "user.permissions", "channels.enable",
                    "task.autocomplete.enable", "ui.enable_signup"):
            self.assertEqual(settings[key].mode, "managed", key)
            self.assertEqual(settings[key].cadence, "at_boot", key)

    def _write(self, doc: dict) -> Path:
        tmp = Path(tempfile.mkdtemp()) / "owui-policy.json"
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        return tmp

    def _minimal_doc(self) -> dict:
        return {
            "version": 1,
            "owui": {"min_version": "0.11.0"},
            "settings": {"entries": {
                "ollama.enable": {
                    "value": False, "mode": "managed", "cadence": "at_boot",
                    "reason": "test",
                }
            }},
            "entities": {},
            "guards": [],
            "api": {"config.export": "GET /api/v1/configs/export"},
        }

    def _rejects(self, mutate) -> None:
        doc = self._minimal_doc()
        mutate(doc)
        with self.assertRaises(owui_policy.ManifestError):
            owui_policy.load_manifest(self._write(doc))

    def test_rejects_unknown_top_level_key(self) -> None:
        self._rejects(lambda d: d.update({"surprise": 1}))

    def test_rejects_unknown_entry_field(self) -> None:
        self._rejects(
            lambda d: d["settings"]["entries"]["ollama.enable"].update({"color": "blue"})
        )

    def test_rejects_unknown_mode(self) -> None:
        self._rejects(
            lambda d: d["settings"]["entries"]["ollama.enable"].update({"mode": "vibes"})
        )

    def test_rejects_unknown_cadence(self) -> None:
        self._rejects(
            lambda d: d["settings"]["entries"]["ollama.enable"].update({"cadence": "hourly"})
        )

    def test_rejects_managed_without_cadence(self) -> None:
        self._rejects(
            lambda d: d["settings"]["entries"]["ollama.enable"].pop("cadence")
        )

    def test_rejects_cadence_on_initial_setup(self) -> None:
        self._rejects(
            lambda d: d["settings"]["entries"]["ollama.enable"].update(
                {"mode": "initial_setup"}
            )
        )

    def test_rejects_bad_dotted_key(self) -> None:
        self._rejects(
            lambda d: d["settings"]["entries"].update(
                {"Ollama Enable": {"value": 1, "mode": "initial_setup", "reason": "t"}}
            )
        )

    def test_rejects_missing_reason(self) -> None:
        self._rejects(
            lambda d: d["settings"]["entries"]["ollama.enable"].pop("reason")
        )

    def test_rejects_unregistered_entity_kind(self) -> None:
        self._rejects(lambda d: d.update({"entities": {"knowledge": {
            "mode": "full_management", "reason": "t"}}}))

    def test_rejects_unknown_entity_fields(self) -> None:
        doc = self._minimal_doc()
        handler = owui_policy.ENTITY_KINDS["models"]
        doc["entities"]["models"] = {
            "mode": "selective_management", "own_by": "id",
            "own_ids": ["lemonseed-ai"], "reserved_name_regex": "^Lemonseed.*$",
            "teleport": True, "reason": "t",
        }
        with self.assertRaises(owui_policy.ManifestError):
            owui_policy.load_manifest(self._write(doc))
        self.assertTrue(hasattr(handler, "validate"))

    def test_rejects_bad_guard_regex_and_placeholder(self) -> None:
        self._rejects(lambda d: d.update({"guards": [
            {"path": "GET /api/v1/[", "mode": "advertise", "reason": "t"}]}))
        self._rejects(lambda d: d.update({"guards": [
            {"path": "GET /api/v1/%SUDO%", "mode": "advertise", "reason": "t"}]}))
        self._rejects(lambda d: d.update({"guards": [
            {"path": "TELEPORT /api/v1/x", "mode": "advertise", "reason": "t"}]}))

    def test_rejects_unknown_guard_key(self) -> None:
        # Strict validation is what makes the manifest safe to extend: a typo'd
        # key must fail loudly rather than silently not guard anything.
        self._rejects(lambda d: d.update({"guards": [
            {"path": "POST /x", "mode": "enforce", "reason": "t",
             "blocked": True}]}))

    def test_rejects_unsafe_guard_hint(self) -> None:
        """The hint is interpolated into an nginx single-quoted string.

        These are exactly the characters that would let manifest copy close the
        string or start a variable, i.e. smuggle a directive.
        """
        for bad in ("don't touch this", "costs $5", "back\\slash", 'say "hi"'):
            with self.subTest(hint=bad):
                self._rejects(lambda d, hint=bad: d.update({"guards": [
                    {"path": "POST /x", "mode": "enforce", "reason": "t",
                     "hint": hint}]}))

    def test_accepts_plain_guard_hint(self) -> None:
        doc = self._minimal_doc()
        doc["guards"] = [{"path": "POST /x", "mode": "enforce", "reason": "t",
                         "hint": "Lemonseed manages this setting (re-applied at boot)."}]
        manifest = owui_policy.load_manifest(self._write(doc))
        self.assertEqual(manifest.guards[0].hint,
                         "Lemonseed manages this setting (re-applied at boot).")
        self.assertEqual(manifest.guards[0].mode, "enforce")

    def test_guard_placeholders_compile(self) -> None:
        doc = self._minimal_doc()
        doc["guards"] = [
            {"path": "DELETE /api/v1/users/.*", "mode": "advertise", "reason": "t"},
            {"path": "POST /api/v1/users/%CP_OWUI_ID%/update", "mode": "advertise",
             "reason": "t"},
        ]
        manifest = owui_policy.load_manifest(self._write(doc))
        self.assertTrue(manifest.guards[1].regex.match("/api/v1/users/abc-123/update"))

    def test_rejects_bad_api_routes_and_names(self) -> None:
        self._rejects(lambda d: d["api"].update({"not a name!": "GET /x"}))
        self._rejects(lambda d: d["api"].update({"x.y": "NOPE /api"}))

    def test_rejects_bad_version(self) -> None:
        self._rejects(lambda d: d.update({"version": "1"}))

    def test_rejects_both_secret_and_derived(self) -> None:
        self._rejects(
            lambda d: d["settings"]["entries"]["ollama.enable"].update(
                {"value": {"secret": "K", "derived": "d"}}
            )
        )

    def test_rejects_unknown_wrap(self) -> None:
        self._rejects(
            lambda d: d["settings"]["entries"]["ollama.enable"].update(
                {"value": {"secret": "K", "wrap": "tuple"}}
            )
        )

    def test_rejects_bad_json(self) -> None:
        tmp = Path(tempfile.mkdtemp()) / "owui-policy.json"
        tmp.write_text("{not json", encoding="utf-8")
        with self.assertRaises(owui_policy.ManifestError):
            owui_policy.load_manifest(tmp)

    def test_missing_file_raises_manifest_error(self) -> None:
        with self.assertRaises(owui_policy.ManifestError):
            owui_policy.load_manifest(Path("/nonexistent/owui-policy.json"))

    def test_version_gate(self) -> None:
        self.assertTrue(owui_policy.version_at_least("0.11.0", "0.11.0"))
        self.assertTrue(owui_policy.version_at_least("0.11.2", "0.9.14"))
        self.assertTrue(owui_policy.version_at_least("v1.0.0", "0.11.0"))
        self.assertFalse(owui_policy.version_at_least("0.9.5", "0.11.0"))
        self.assertFalse(owui_policy.version_at_least("garbage", "0.11.0"))


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Guard renderer (Phase 4B) — pure manifest -> nginx text
# ---------------------------------------------------------------------------


class TestGuardRenderer(unittest.TestCase):
    ID = "0f9a1b2c-3d4e-5f60-7182-93a4b5c6d7e8"
    OTHER_ID = "1a1a1a1a-2b2b-3c3c-4d4d-5e5e5e5e5e5e"

    def render(self, *guards: owui_policy.GuardSpec, placeholders: Optional[dict] = None):
        return owui_policy.render_guards(
            manifest_with(guards=guards), placeholders or {}
        )

    def test_enforce_guard_renders_one_anchored_rule(self) -> None:
        render = self.render(
            guard(hint="Lemonseed manages model connections."),
        )
        self.assertEqual(render.enforced, ("POST /api/v1/configs/connections",))
        self.assertEqual(render.pending, ())
        self.assertEqual(render.text.count("if ("), 1)
        # Anchoring is load-bearing: it is what stops /connections/update-ish
        # paths from being blocked by a prefix match. ``$request`` is the whole
        # request line ("POST /path HTTP/1.1"), so the method rides along in the
        # same pattern and the protocol version terminates the path.
        self.assertIn(
            'if ($request ~ "^POST /api/v1/configs/connections'
            + owui_policy.GUARD_REQUEST_TAIL + '") {',
            render.text,
        )
        body = "{\"detail\":\"Lemonseed manages model connections.\","
        body += "\"lemonseed\":\"managed-by-lemonseed\","
        body += "\"hint\":\"Lemonseed manages model connections.\"}"
        self.assertIn("return 403 '" + body + "';", render.text)
        # ``detail`` first: Open WebUI renders error.response.data.detail and
        # otherwise shows its own generic connection error, hiding the reason.
        self.assertIn('"detail":"Lemonseed manages model connections."', render.text)
        # nginx rejects `default_type` inside `if`; gateway.nix sets it at
        # location level, so the renderer must not duplicate it here.
        self.assertNotIn("default_type", render.text)

    def test_advertise_guard_emits_no_nginx_output(self) -> None:
        render = self.render(guard(mode="advertise"), guard(
            method="DELETE", path="/api/v1/users/.*", mode="advertise"))
        self.assertEqual(render.text, "")
        self.assertEqual(render.enforced, ())
        self.assertNotIn("connections", render.text)

    def test_placeholder_resolves_to_the_literal_id(self) -> None:
        render = self.render(
            guard(method="DELETE", path="/api/v1/users/%CP_OWUI_ID%",
                  hint="Not removable here."),
            placeholders={"CP_OWUI_ID": self.ID},
        )
        self.assertIn(
            f"^DELETE /api/v1/users/{self.ID}" + owui_policy.GUARD_REQUEST_TAIL,
            render.text,
        )
        # The validator's [^/]+ stand-in must never leak into a rendered rule:
        # it would make "protect the CP account" mean "protect every account".
        self.assertNotIn("[^/]", render.text)
        self.assertNotIn("%", render.text)

    def test_unresolved_placeholder_lands_in_pending(self) -> None:
        render = self.render(
            guard(method="DELETE", path="/api/v1/users/%CP_OWUI_ID%"),
            guard(path="/openai/config/update"),
        )
        self.assertEqual(render.pending, ("DELETE /api/v1/users/%CP_OWUI_ID%",))
        self.assertEqual(len(render.errors), 1)
        self.assertIn("unresolved", render.errors[0])
        # Only the renderable guard is emitted; nothing half-substituted.
        self.assertEqual(render.enforced, ("POST /openai/config/update",))
        self.assertEqual(render.text.count("if ("), 1)
        self.assertNotIn("users", render.text)
        self.assertNotIn("[^/]", render.text)

    def test_non_id_placeholder_is_pending_not_interpolated(self) -> None:
        render = self.render(
            guard(method="DELETE", path="/api/v1/users/%CP_OWUI_ID%"),
            placeholders={"CP_OWUI_ID": "../../etc; return"},
        )
        self.assertEqual(render.enforced, ())
        self.assertEqual(render.text, "")

    def test_unsafe_hint_raises_without_partial_render(self) -> None:
        """Loader validation is the first line of defence; the renderer asserts
        again so a hand-built Manifest cannot bypass it."""
        with self.assertRaises(owui_policy.PolicyError):
            self.render(guard(hint="Open WebUI's connection editor"))
        # A guard that fails validation aborts the whole render — never a file
        # with the safe rules written and the unsafe one silently dropped.
        with self.assertRaises(owui_policy.PolicyError):
            self.render(guard(path="/safe/path"), guard(path="/x", hint="a$b"))

    def test_default_hint_when_omitted(self) -> None:
        render = self.render(guard())
        self.assertIn(owui_policy.DEFAULT_GUARD_HINT, render.text)

    def test_marker_is_present_for_every_enforced_guard(self) -> None:
        render = self.render(
            guard(path="/openai/config/update"),
            guard(path="/ollama/config/update"),
            guard(method="DELETE", path="/api/v1/users/xyz"),
        )
        self.assertEqual(
            render.text.count(owui_policy.GUARD_DENIAL_MARKER), len(render.enforced)
        )

    def test_text_is_byte_stable_and_sensitive_to_the_enforce_set(self) -> None:
        guards = (
            guard(path="/openai/config/update", hint="a"),
            guard(method="DELETE", path="/api/v1/users/xyz", hint="b"),
        )
        first = self.render(*guards)
        second = self.render(*guards)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.text, second.text)
        # Reordering the manifest must not churn the file (a reload per reorder
        # would be pure noise in the journal).
        reordered = self.render(*reversed(guards))
        self.assertEqual(reordered.sha256, first.sha256)
        flipped = self.render(guards[0], guard(
            method="DELETE", path="/api/v1/users/xyz", mode="advertise", hint="b"))
        self.assertNotEqual(flipped.sha256, first.sha256)

    def test_dropped_guard_disappears_from_the_text(self) -> None:
        """T4-B: the file is a render, not an accumulation."""
        keep = guard(path="/openai/config/update")
        retire = guard(method="DELETE", path="/api/v1/users/xyz")
        before = self.render(keep, retire)
        self.assertIn("/api/v1/users/xyz", before.text)
        after = self.render(keep, guard(
            method="DELETE", path="/api/v1/users/xyz", mode="advertise"))
        self.assertNotIn("users", after.text)
        self.assertNotIn("xyz", after.text)
        self.assertEqual(after.enforced, ("POST /openai/config/update",))
        # Down to zero enforce guards, the file is empty rather than commented out.
        self.assertEqual(self.render(guard(mode="advertise")).text, "")

    def test_in_tree_manifest_blocks_the_admin_user_routes(self) -> None:
        """Render the *shipped* manifest and match real request lines against it.

        What nginx evaluates is not the manifest string but the anchored pattern
        the renderer builds from it, so the D-17b read-only user panel is proven
        here by matching request lines, not by reading JSON.
        """
        render = owui_policy.render_guards(
            owui_policy.load_manifest(),
            {"CP_OWUI_ID": self.ID, "SERVICE_ACCOUNT_ID": self.OTHER_ID},
        )
        self.assertEqual(render.pending, ())
        self.assertIn("POST /api/v1/users/[^/]+/update", render.enforced)
        self.assertIn("DELETE /api/v1/users/.*", render.enforced)
        self.assertIn("DELETE /api/v1/users/%CP_OWUI_ID%", render.enforced)
        # The last of them, as of M7 P3: nothing in the OWUI user panel can write.
        self.assertIn("POST /api/v1/auths/add", render.enforced)

        patterns = [
            re.compile(pattern)
            for pattern in re.findall(
                r'^if \(\$request ~ "(\^.*)"\) \{', render.text, re.MULTILINE
            )
        ]
        self.assertEqual(len(patterns), len(render.enforced))

        def blocked(request: str) -> bool:
            # ``~`` is an unanchored PCRE search; the patterns carry their own ^.
            return any(pattern.search(request) for pattern in patterns)

        target = "11111111-2222-4444-8888-555555555555"
        # Every admin user update: e-mail, role and password all travel here.
        self.assertTrue(blocked(f"POST /api/v1/users/{target}/update HTTP/1.1"))
        self.assertTrue(blocked(f"POST /api/v1/users/{self.ID}/update HTTP/1.1"))
        self.assertTrue(blocked(f"POST /api/v1/users/{target}/update?x=1 HTTP/1.1"))
        # ...and deleting any account, including the CP's own.
        self.assertTrue(blocked(f"DELETE /api/v1/users/{target} HTTP/1.1"))
        self.assertTrue(blocked(f"DELETE /api/v1/users/{self.ID} HTTP/1.1"))
        # The end anchor still holds: a path that merely starts with a guarded
        # route is not the guarded route, and the sibling routes under the same
        # prefixes — self-service password change, the API-key routes, first-run
        # signup and trusted-header sign-in — are untouched.
        self.assertFalse(blocked(f"POST /api/v1/users/{target}/update/profile HTTP/1.1"))
        self.assertTrue(blocked("POST /api/v1/auths/add HTTP/1.1"))
        self.assertFalse(blocked("POST /api/v1/auths/update/password HTTP/1.1"))
        # The id segment is matched with ``[^/]+`` rather than ``.*`` because
        # Open WebUI's own self-service writes sit under the same prefix
        # ("/api/v1/users/user/settings/update", ".../user/status/update",
        # ".../user/info/update"). D-17a says the module owns avatar, bio,
        # timezone and settings, so guarding them would be a silent feature loss
        # nobody asked for — and every real user id is a slash-free UUID, so
        # nothing is given up in coverage.
        self.assertFalse(blocked("POST /api/v1/users/user/info/update HTTP/1.1"))
        self.assertFalse(blocked("POST /api/v1/users/user/settings/update HTTP/1.1"))
        self.assertFalse(blocked("POST /api/v1/users/user/status/update HTTP/1.1"))

    def test_backslash_in_path_is_rejected(self) -> None:
        """nginx strips backslashes inside quoted config strings, so \\. and \\?
        would silently change meaning. Bracket classes are the supported form."""
        with self.assertRaises(owui_policy.PolicyError):
            self.render(guard(path="/api/v1/configs\\.export"))

    def test_dollar_in_path_is_rejected(self) -> None:
        """The renderer owns the end anchor, so a self-anchored path pattern
        would quietly stop matching rather than doing what it looks like."""
        with self.assertRaises(owui_policy.PolicyError):
            self.render(guard(path="/api/v1/configs/export$"))

    def test_rendered_condition_is_a_single_variable(self) -> None:
        """nginx's ``if`` only accepts one variable on the left.

        Concatenating two ("$request_method$request_uri") parses as an
        "invalid condition" — this is the assertion that would have caught it
        without needing an nginx binary. Real nginx acceptance is proven by the
        QEMU guard cases.
        """
        render = self.render(guard(), guard(method="DELETE", path="/x/y"))
        conditions = re.findall(r"^if \((.+?) ~ \"", render.text, re.MULTILINE)
        self.assertEqual(conditions, ["$request", "$request"])
        for line in render.text.splitlines():
            # A quoted config string with a backslash or an unbalanced quote is
            # how a render turns into a broken gateway.
            if '"' in line:
                self.assertEqual(line.count('"') % 2, 0, line)
        self.assertNotIn("\\", render.text)

    def test_sha_is_over_the_rendered_text(self) -> None:
        render = self.render(guard())
        self.assertEqual(
            render.sha256,
            hashlib.sha256(render.text.encode("utf-8")).hexdigest(),
        )
        empty = self.render()
        self.assertEqual(empty.text, "")
        self.assertEqual(
            empty.sha256, hashlib.sha256(b"").hexdigest()
        )


# ---------------------------------------------------------------------------
# Resolvers (§5.2)
# ---------------------------------------------------------------------------


class TestResolvers(unittest.TestCase):
    def test_const_is_deep_copied(self) -> None:
        entry = setting("k", "initial_setup", None, value={"a": [1]})
        ctx = owui_policy.ResolverContext()
        value, err = owui_policy.resolve_setting_value(entry, ctx)
        self.assertIsNone(err)
        value["a"].append(2)
        again, _ = owui_policy.resolve_setting_value(entry, ctx)
        self.assertEqual(again, {"a": [1]})

    def test_secret_happy_and_missing(self) -> None:
        entry = owui_policy.SettingEntry(
            key="k", spec=owui_policy.ValueSpec("secret", name="OPENAI_API_KEY"),
            mode="managed", cadence="at_boot", optional=False, reason="t",
        )
        ctx = owui_policy.ResolverContext(secrets={"OPENAI_API_KEY": "sk-lemon"})
        value, err = owui_policy.resolve_setting_value(entry, ctx)
        self.assertEqual((value, err), ("sk-lemon", None))
        for empty in ({}, {"OPENAI_API_KEY": ""}):
            value, err = owui_policy.resolve_setting_value(
                entry, owui_policy.ResolverContext(secrets=empty)
            )
            self.assertIsNone(value)
            self.assertIn("OPENAI_API_KEY", err or "")

    def test_wrap_list(self) -> None:
        entry = owui_policy.SettingEntry(
            key="k",
            spec=owui_policy.ValueSpec("secret", name="KEY", wrap_list=True),
            mode="managed", cadence="at_boot", optional=False, reason="t",
        )
        value, _err = owui_policy.resolve_setting_value(
            entry, owui_policy.ResolverContext(secrets={"KEY": "v"})
        )
        self.assertEqual(value, ["v"])

    def test_derived_lemond_base(self) -> None:
        entry = owui_policy.SettingEntry(
            key="k",
            spec=owui_policy.ValueSpec("derived", name="lemond_base", wrap_list=True),
            mode="managed", cadence="at_boot", optional=False, reason="t",
        )
        ctx = owui_policy.ResolverContext(lemond_base="http://lemond/v1")
        value, err = owui_policy.resolve_setting_value(entry, ctx)
        self.assertEqual(value, ["http://lemond/v1"])
        value, err = owui_policy.resolve_setting_value(entry, owui_policy.ResolverContext())
        self.assertIsNone(value)
        self.assertIn("lemond", err or "")

    def test_derived_api_configs_shape(self) -> None:
        ctx = owui_policy.ResolverContext(lemond_base="http://lemond/v1")
        self.assertEqual(
            owui_policy.RESOLVERS["lemond_api_configs"](ctx),
            {"http://lemond/v1": {"valves": {}}},
        )

    def test_derived_allowed_endpoints_from_real_manifest(self) -> None:
        manifest = load_real_manifest()
        derived = owui_policy.derive_allowed_endpoints(manifest.api)
        self.assertTrue(derived)
        self.assertNotIn("{", derived)
        parts = derived.split(",")
        self.assertEqual(parts, sorted(parts))
        # OWUI prefix semantics: parameterized routes cut at their first
        # {param} segment; static routes stay exact.
        self.assertIn("/api/v1/users", parts)
        self.assertIn("/api/v1/models/export", parts)
        self.assertIn("/api/v1/configs/export", parts)
        self.assertIn("/api/config", parts)
        self.assertFalse(any(p.endswith("/") for p in parts))

    def test_unknown_derived_name_errors(self) -> None:
        entry = owui_policy.SettingEntry(
            key="k", spec=owui_policy.ValueSpec("derived", name="nonsense"),
            mode="managed", cadence="at_boot", optional=False, reason="t",
        )
        value, err = owui_policy.resolve_setting_value(entry, owui_policy.ResolverContext())
        self.assertIsNone(value)
        self.assertIn("nonsense", err or "")


# ---------------------------------------------------------------------------
# Planner (pure mode semantics, §5.3 table row by row)
# ---------------------------------------------------------------------------


class TestPlanner(unittest.TestCase):
    def _resolve(self, manifest, ctx=None):
        return resolved_map(manifest, ctx or owui_policy.ResolverContext())

    def test_initial_setup_missing_backfills(self) -> None:
        manifest = manifest_with({"ui.default_models": setting(
            "ui.default_models", "initial_setup", None, value=[])})
        result = owui_policy.plan(
            manifest, self._resolve(manifest), {}, {}, {}, BOOT_TRIGGERS,
        )
        self.assertEqual(len(result.actions), 1)
        action = result.actions[0]
        self.assertEqual((action.kind, action.op), ("setting", "create"))
        self.assertEqual(action.payload, {"ui.default_models": []})

    def test_initial_setup_present_never_touched_even_if_different(self) -> None:
        manifest = manifest_with({"ui.default_models": setting(
            "ui.default_models", "initial_setup", None, value=[])})
        result = owui_policy.plan(
            manifest, self._resolve(manifest),
            {"ui.default_models": ["user-chose-this"]}, {}, {}, BOOT_TRIGGERS,
        )
        self.assertEqual(result.actions, [])
        self.assertEqual(result.entries[0].state, "ok")

    def test_managed_diff_fires_on_cadence(self) -> None:
        manifest = manifest_with({"web.search.enable": setting(
            "web.search.enable", value=False)})
        result = owui_policy.plan(
            manifest, self._resolve(manifest),
            {"web.search.enable": True}, {}, {}, BOOT_TRIGGERS,
        )
        self.assertEqual(len(result.actions), 1)
        action = result.actions[0]
        self.assertEqual(action.op, "update")
        self.assertEqual((action.before, action.after), (True, False))
        self.assertEqual(result.entries[0].state, "to_fix")

    def test_managed_diff_gated_by_cadence_is_pending(self) -> None:
        manifest = manifest_with({"models.default_x": setting(
            "models.default_x", "managed", cadence="on_upgrade", value=9)})
        result = owui_policy.plan(
            manifest, self._resolve(manifest), {"models.default_x": 1}, {}, {},
            frozenset({"at_boot"}),
        )
        self.assertEqual(result.actions, [])
        self.assertEqual(result.entries[0].state, "pending")

    def test_managed_equal_is_ok(self) -> None:
        manifest = manifest_with({"ollama.enable": setting(
            "ollama.enable", value=False)})
        result = owui_policy.plan(
            manifest, self._resolve(manifest), {"ollama.enable": False}, {}, {},
            BOOT_TRIGGERS,
        )
        self.assertEqual(result.actions, [])
        self.assertEqual(result.entries[0].state, "ok")

    def test_managed_missing_row_creates(self) -> None:
        manifest = manifest_with({"new.key": setting("new.key", value=5)})
        result = owui_policy.plan(manifest, self._resolve(manifest), {}, {}, {},
                                  BOOT_TRIGGERS)
        self.assertEqual(result.actions[0].op, "create")

    def test_resolution_error_is_reported_not_written(self) -> None:
        manifest = manifest_with({"openai.api_keys": owui_policy.SettingEntry(
            key="openai.api_keys",
            spec=owui_policy.ValueSpec("secret", name="OPENAI_API_KEY", wrap_list=True),
            mode="managed", cadence="at_boot", optional=False, reason="t",
        )})
        result = owui_policy.plan(manifest, self._resolve(manifest), {}, {}, {},
                                  BOOT_TRIGGERS)
        self.assertEqual(result.actions, [])
        self.assertEqual(result.entries[0].state, "error")
        self.assertIn("OPENAI_API_KEY", result.entries[0].note)

    def test_wiring_keys_apply_before_other_settings(self) -> None:
        manifest = manifest_with({
            "zzz.enable": setting("zzz.enable", value=1),
            "auth.enable_api_keys": setting("auth.enable_api_keys", value=True),
            "aaa.enable": setting("aaa.enable", value=1),
        })
        observed = {"zzz.enable": 0, "auth.enable_api_keys": False, "aaa.enable": 0}
        result = owui_policy.plan(manifest, self._resolve(manifest), observed, {}, {},
                                  BOOT_TRIGGERS)
        order = [a.target for a in result.actions]
        self.assertEqual(order[0], "auth.enable_api_keys")

    # -- models (selective_management) --------------------------------------

    def _models_plan(self, desired, observed, triggers=BOOT_TRIGGERS, definitions=None):
        manifest = manifest_with(
            entities={"models": models_spec(definitions=definitions)},
        )
        return owui_policy.plan(
            manifest, {}, {}, {"models": observed}, {"models": desired}, triggers,
        )

    def test_models_owned_missing_creates_owned_id(self) -> None:
        want = {"id": "lemonseed-ai", "name": "Lemonseed AI", "data": {"x": 1}}
        result = self._models_plan(
            {"lemonseed-ai": want}, {},
            definitions={"lemonseed-ai": want},
        )
        self.assertEqual(len(result.actions), 1)
        self.assertEqual((result.actions[0].op, result.actions[0].entity_id),
                         ("create", "lemonseed-ai"))

    def test_models_owned_drift_updates_changed_keys(self) -> None:
        want = {"id": "lemonseed-ai", "name": "Lemonseed AI", "data": {"x": 2}}
        have = {"id": "lemonseed-ai", "name": "Lemonseed AI", "data": {"x": 1},
                "user_id": "server-owned"}
        result = self._models_plan(
            {"lemonseed-ai": want}, {"lemonseed-ai": have},
            definitions={"lemonseed-ai": want},
        )
        self.assertEqual(result.actions[0].op, "update")
        self.assertEqual(result.actions[0].payload, {"data": {"x": 2}})

    def test_models_owned_id_without_definition_is_silent(self) -> None:
        # Phase-5 armed: our id exists but definitions not shipped — untouched.
        have = {"id": "lemonseed-ai", "name": "Lemonseed AI"}
        result = self._models_plan({}, {"lemonseed-ai": have})
        self.assertEqual(result.actions, [])
        self.assertEqual(result.entries, [])

    def test_models_foreign_reserved_name_sweeps_case_insensitive(self) -> None:
        have = {"id": "uuid-foreign", "name": "lemonseed ROBOT"}
        result = self._models_plan({}, {"uuid-foreign": have})
        self.assertEqual([a.op for a in result.actions], ["sweep"])
        self.assertEqual(result.entries[0].state, "to_fix")

    def test_models_foreign_reserved_name_reported_on_audit_tick(self) -> None:
        have = {"id": "uuid-foreign", "name": "Lemonseed Robot"}
        result = self._models_plan({}, {"uuid-foreign": have},
                                   triggers=AUDIT_TRIGGERS)
        self.assertEqual(result.actions, [])
        self.assertEqual(result.entries[0].state, "pending")

    def test_models_foreign_unreserved_never_touched_or_reported(self) -> None:
        have = {"id": "uuid-foreign", "name": "My Custom Model"}
        result = self._models_plan({}, {"uuid-foreign": have})
        self.assertEqual(result.actions, [])
        self.assertEqual(result.entries, [])

    # -- users (full_management) ---------------------------------------------

    def _users_plan(self, desired, observed, triggers=BOOT_TRIGGERS):
        manifest = manifest_with(entities={"users": users_spec()})
        return owui_policy.plan(
            manifest, {}, {}, {"users": observed}, {"users": desired}, triggers,
        )

    def test_users_missing_in_owui_creates(self) -> None:
        want = {"email": "ana@lemonseed.local", "name": "Ana", "is_admin": False}
        result = self._users_plan({"ana@lemonseed.local": want}, {})
        self.assertEqual([a.op for a in result.actions], ["create"])

    def test_users_role_drift_pushes_update(self) -> None:
        want = {"email": "ana@lemonseed.local", "name": "Ana", "is_admin": False}
        have = {"id": "u-1", "email": "ana@lemonseed.local", "role": "admin",
                "name": "Ana"}
        result = self._users_plan({"ana@lemonseed.local": want},
                                  {"ana@lemonseed.local": have})
        self.assertEqual(result.actions[0].op, "update")
        self.assertEqual(result.actions[0].payload, {"role": "user"})

    def test_users_rename_pushes_name_only(self) -> None:
        # A CP-side rename has to converge: OWUI reads the trusted name header
        # only when it creates the account, so nothing else fixes it (D-17a).
        want = {"email": "ana@lemonseed.local", "name": "Ana Maria", "is_admin": False}
        have = {"id": "u-1", "email": "ana@lemonseed.local", "role": "user",
                "name": "Ana"}
        result = self._users_plan({"ana@lemonseed.local": want},
                                  {"ana@lemonseed.local": have})
        self.assertEqual(result.actions[0].op, "update")
        self.assertEqual(result.actions[0].payload, {"name": "Ana Maria"})

    def test_users_keyed_by_owui_id_updates_rather_than_creates(self) -> None:
        # D-18: both sides carry CP-known owui_id keys, so the planner matches
        # the row even though its email differs from what we intended.
        want = {"email": "ana@localhost", "name": "Ana", "is_admin": True}
        have = {"id": "u-1", "email": "ana@localhost", "role": "user",
                "name": "Ana"}
        result = self._users_plan({"u-1": want}, {"u-1": have})
        self.assertEqual([a.op for a in result.actions], ["update"])
        self.assertEqual(result.actions[0].payload, {"role": "admin"})
        self.assertEqual(result.actions[0].entity_id, "u-1")

    def test_users_unknown_id_matches_by_email_and_never_creates(self) -> None:
        # owui_id unknown (empty) => key is the identity email, which must meet
        # the observed row at that email instead of minting a duplicate.
        want = {"email": "ana@localhost", "name": "Ana", "is_admin": False}
        have = {"id": "u-9", "email": "ana@localhost", "role": "user",
                "name": "Ana"}
        result = self._users_plan({"ana@localhost": want},
                                  {"ana@localhost": have})
        self.assertEqual(result.actions, [])
        self.assertEqual([e.state for e in result.entries], ["ok"])

    def test_users_in_owui_not_in_cp_is_drift_never_delete(self) -> None:
        have = {"id": "u-9", "email": "ghost@lemonseed.local", "role": "user"}
        result = self._users_plan({}, {"ghost@lemonseed.local": have})
        self.assertEqual(result.actions, [])
        self.assertEqual(result.entries[0].state, "drift")

    def test_users_loop_never_emits_a_delete(self) -> None:
        # Extra observed rows, an unknown-id desired row and a stale-id desired
        # row, all in one tick: the planner may create/update/report drift, but
        # it must never plan a delete or a sweep (DESIGN rule; D-16a keeps
        # deletion in the portal handler).
        observed = {
            "u-1": {"id": "u-1", "email": "ana@localhost", "role": "user",
                    "name": "Ana"},
            "u-9": {"id": "u-9", "email": "ghost@localhost", "role": "admin",
                    "name": "Ghost"},
        }
        desired = {
            "u-1": {"email": "ana@localhost", "name": "Ana", "is_admin": True},
            "bob@localhost": {"email": "bob@localhost", "name": "Bob",
                              "is_admin": False},
            "u-gone": {"email": "gone@localhost", "name": "Gone", "is_admin": False},
        }
        for triggers in (BOOT_TRIGGERS, AUDIT_TRIGGERS):
            result = self._users_plan(desired, observed, triggers=triggers)
            self.assertNotIn("delete", [a.op for a in result.actions])
            self.assertNotIn("sweep", [a.op for a in result.actions])
        self.assertEqual(
            sorted((a.op, a.entity_id) for a in self._users_plan(desired, observed).actions),
            [("create", "bob@localhost"), ("create", "u-gone"), ("update", "u-1")],
        )

    def test_users_audit_tick_suppresses_writes(self) -> None:
        want = {"email": "ana@lemonseed.local", "name": "Ana", "is_admin": False}
        have = {"id": "u-1", "email": "ana@lemonseed.local", "role": "admin"}
        result = self._users_plan({"ana@lemonseed.local": want},
                                  {"ana@lemonseed.local": have},
                                  triggers=AUDIT_TRIGGERS)
        self.assertEqual(result.actions, [])
        self.assertEqual(result.entries[0].state, "pending")

    def test_settings_ordered_before_entities(self) -> None:
        manifest = manifest_with(
            {"ollama.enable": setting("ollama.enable", value=False)},
            {"users": users_spec()},
        )
        want = {"ana@lemonseed.local": {"email": "ana@lemonseed.local",
                                        "name": "Ana", "is_admin": False}}
        result = owui_policy.plan(manifest, resolved_map(manifest, owui_policy.ResolverContext()),
                                  {"ollama.enable": True}, {}, {"users": want},
                                  BOOT_TRIGGERS)
        kinds = [a.kind for a in result.actions]
        self.assertEqual(kinds, ["setting", "entity"])


# ---------------------------------------------------------------------------
# Entity handlers
# ---------------------------------------------------------------------------


class TestModelsHandler(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = owui_policy.ENTITY_KINDS["models"]

    def test_validate_rejects_bad_configs(self) -> None:
        base = {"own_by": "id", "own_ids": ["lemonseed-ai"],
                "reserved_name_regex": "^Lemonseed.*$"}
        with self.assertRaises(ValueError):  # unknown own_by
            self.handler.validate("selective_management", {**base, "own_by": "name"})
        with self.assertRaises(ValueError):  # bad regex
            self.handler.validate("selective_management",
                                  {**base, "reserved_name_regex": "^Lemonseed("})
        with self.assertRaises(ValueError):  # definitions outside own_ids
            self.handler.validate("selective_management",
                                  {**base, "definitions": {"other": {"id": "other"}}})
        with self.assertRaises(ValueError):  # wrong mode family
            self.handler.validate("full_ownership", base)

    def test_observe_keys_by_cp_id(self) -> None:
        client = _FakeClient({"models.export": [
            {"id": "lemonseed-ai", "name": "Lemonseed AI"},
            {"id": "theirs", "name": "Theirs"},
        ]})
        observed = self.handler.observe(client, models_spec(), owui_policy.ResolverContext())
        self.assertEqual(set(observed), {"lemonseed-ai", "theirs"})

    def test_observe_failure_raises_policy_error(self) -> None:
        class _Down:
            def call(self, *_a, **_k):
                return {"ok": False, "status": None, "data": None, "error": "refused"}

        with self.assertRaises(owui_policy.PolicyError):
            self.handler.observe(_Down(), models_spec(), owui_policy.ResolverContext())

    def test_reserved_exempt_own_ids(self) -> None:
        spec = models_spec(own_ids=["lemonseed-ai"])
        self.assertFalse(self.handler.reserved(spec, {"id": "lemonseed-ai",
                                                      "name": "Lemonseed AI"}))
        self.assertTrue(self.handler.reserved(spec, {"id": "uuid", "name": "lemonseed clone"}))
        self.assertFalse(self.handler.reserved(spec, {"id": "uuid", "name": "Plain"}))

    def test_apply_ops(self) -> None:
        client = _FakeClient()
        spec = models_spec()
        create = owui_policy.Action(kind="entity", op="create", target="models:lemonseed-ai",
                                    after={"id": "lemonseed-ai"}, entity_kind="models",
                                    entity_id="lemonseed-ai")
        self.handler.apply(client, create)
        update = owui_policy.Action(kind="entity", op="update", target="models:lemonseed-ai",
                                    payload={"data": {}}, entity_kind="models",
                                    entity_id="lemonseed-ai")
        self.handler.apply(client, update)
        sweep = owui_policy.Action(kind="entity", op="sweep", target="models:x",
                                   entity_kind="models", entity_id="x")
        self.handler.apply(client, sweep)
        names = [c["name"] for c in client.calls]
        self.assertEqual(names, ["models.create", "models.update", "models.delete"])
        self.assertEqual(client.calls[1]["json_body"], {"id": "lemonseed-ai", "data": {}})
        self.assertEqual(client.calls[2]["json_body"], {"id": "x"})
        with self.assertRaises(owui_policy.PolicyError):
            self.handler.apply(client, owui_policy.Action(
                kind="entity", op="teleport", target="models:x", entity_kind="models",
                entity_id="x"))
        self.assertEqual(spec.kind, "models")


class TestUsersHandler(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = owui_policy.ENTITY_KINDS["users"]
        self.spec = users_spec()

    def test_validate_rejects_bad_configs(self) -> None:
        with self.assertRaises(ValueError):  # wrong mode
            owui_policy.UsersHandler().validate(
                "selective_management",
                {"source": "cp.users", "scope": ["membership"]},
            )
        with self.assertRaises(ValueError):  # bad scope entry
            self.handler.validate("full_management",
                                  {"source": "cp.users", "scope": ["souls"]})
        with self.assertRaises(ValueError):  # bad source
            self.handler.validate("full_management",
                                  {"source": "ldap", "scope": ["membership"]})
        with self.assertRaises(ValueError):  # never_touch must be placeholders
            self.handler.validate("full_management",
                                  {"source": "cp.users", "scope": ["membership"],
                                   "never_touch": ["some-real-uuid"]})

    def test_observe_excludes_service_account_by_id(self) -> None:
        # v0.11.0 /users/all envelope: {"users": [...], "total": N} — the
        # real shape (the QEMU run caught a bare-list fake hiding a bug).
        client = _FakeClient({"users.all": {"users": [
            {"id": "svc-1", "email": "lemonseed-cp@localhost", "role": "admin"},
            {"id": "u-1", "email": "Ana@Lemonseed.Local", "role": "user"},
        ], "total": 2}})
        ctx = owui_policy.ResolverContext(placeholders={"SERVICE_ACCOUNT_ID": "svc-1"})
        observed = self.handler.observe(client, self.spec, ctx)
        self.assertEqual(set(observed), {"ana@lemonseed.local"})
        self.assertEqual(observed["ana@lemonseed.local"]["id"], "u-1")

    def test_observe_accepts_bare_list_shape(self) -> None:
        client = _FakeClient({"users.all": [{"id": "u-1", "email": "a@x", "role": "user"}]})
        observed = self.handler.observe(client, self.spec, owui_policy.ResolverContext())
        self.assertEqual(set(observed), {"a@x"})

    def test_observe_shape_mismatch_reports_shape(self) -> None:
        client = _FakeClient({"users.all": {"items": []}})
        with self.assertRaises(owui_policy.PolicyError) as caught:
            self.handler.observe(client, self.spec, owui_policy.ResolverContext())
        self.assertIn("unexpected response shape", str(caught.exception))

    def test_observe_keys_by_owui_id_when_the_cp_holds_it(self) -> None:
        # D-18: the id is retained, not just used to filter never_touch. u-1 is
        # a CP-owned account (its owui_id is in cp_users); u-2 is not, so it
        # stays email-keyed and reads as drift.
        client = _FakeClient({"users.all": {"users": [
            {"id": "svc-1", "email": "lemonseed-cp@localhost", "role": "admin"},
            {"id": "u-1", "email": "ana@localhost", "role": "user"},
            {"id": "u-2", "email": "ghost@localhost", "role": "user"},
        ], "total": 3}})
        ctx = owui_policy.ResolverContext(
            placeholders={"SERVICE_ACCOUNT_ID": "svc-1"},
            cp_users=[{"email": "ana@localhost", "name": "Ana", "is_admin": False,
                       "owui_id": "u-1"}],
        )
        observed = self.handler.observe(client, self.spec, ctx)
        self.assertEqual(set(observed), {"u-1", "ghost@localhost"})
        self.assertEqual(observed["u-1"]["id"], "u-1")

    def test_observe_falls_back_to_email_when_id_is_unknown(self) -> None:
        # owui_id "" means "we never captured it", not "no account exists":
        # the row must key by email so it matches instead of being re-created.
        client = _FakeClient({"users.all": {"users": [
            {"id": "u-9", "email": "ana@localhost", "role": "user"},
        ], "total": 1}})
        ctx = owui_policy.ResolverContext(cp_users=[
            {"email": "ana@localhost", "name": "Ana", "is_admin": False, "owui_id": ""},
        ])
        observed = self.handler.observe(client, self.spec, ctx)
        self.assertEqual(set(observed), {"ana@localhost"})

    def test_desired_skips_disabled_and_normalizes(self) -> None:
        ctx = owui_policy.ResolverContext(cp_users=[
            {"email": "Bob@Lemonseed.Local", "name": "Bob", "is_admin": True,
             "disabled": False},
            {"email": "gone@lemonseed.local", "name": "Gone", "is_admin": False,
             "disabled": True},
        ])
        desired = self.handler.desired(self.spec, ctx)
        self.assertEqual(set(desired), {"bob@lemonseed.local"})
        self.assertTrue(desired["bob@lemonseed.local"]["is_admin"])

    def test_desired_keys_by_owui_id_but_never_sends_it(self) -> None:
        # Key = owui_id; the payload stays intent-only (no id, no password).
        ctx = owui_policy.ResolverContext(cp_users=[
            {"email": "ana@localhost", "name": "Ana", "is_admin": False,
             "owui_id": "7d1e-a"},
            {"email": "bob@localhost", "name": "Bob", "is_admin": False,
             "owui_id": ""},
        ])
        desired = self.handler.desired(self.spec, ctx)
        self.assertEqual(set(desired), {"7d1e-a", "bob@localhost"})
        self.assertEqual(desired["7d1e-a"]["email"], "ana@localhost")
        self.assertEqual(set(desired["7d1e-a"]), {"email", "name", "is_admin"})

    def test_changes_emits_role_and_name_diffs(self) -> None:
        want = {"email": "a@x", "name": "A", "is_admin": False}
        in_sync = {"id": "u", "email": "a@x", "role": "user", "name": "A"}
        self.assertIsNone(self.handler.changes(self.spec, want, in_sync))
        self.assertEqual(self.handler.changes(self.spec, want, {**in_sync, "role": "admin"}),
                         {"role": "user"})
        # A rename made on the CP side must converge (D-18).
        self.assertEqual(
            self.handler.changes(self.spec, want,
                                 {**in_sync, "name": "Renamed By User"}),
            {"name": "A"},
        )
        self.assertEqual(
            self.handler.changes(self.spec, want,
                                 {**in_sync, "role": "admin", "name": "Old"}),
            {"role": "user", "name": "A"},
        )
        # An empty desired name is "we do not know it", never "blank their name".
        self.assertIsNone(self.handler.changes(self.spec, {**want, "name": ""},
                                               {**in_sync, "name": "Renamed By User"}))
        # Pending accounts sync at trusted sign-in; pushing now 404s on UX.
        self.assertIsNone(self.handler.changes(self.spec, want,
                                               {"id": "u", "email": "a@x",
                                                "role": "pending", "name": "Old"}))
        # Scope decides each field: no is_admin, no role diff; no name, no name
        # diff (the shipped manifest scopes both).
        self.assertEqual(
            self.handler.changes(users_spec(scope=["membership", "name"]), want,
                                 {**in_sync, "role": "admin", "name": "Old"}),
            {"name": "A"},
        )
        self.assertEqual(
            self.handler.changes(users_spec(scope=["membership", "is_admin"]), want,
                                 {**in_sync, "role": "admin", "name": "Old"}),
            {"role": "user"},
        )
        narrow = users_spec(scope=["membership"])
        self.assertIsNone(self.handler.changes(narrow, want,
                                               {"id": "u", "email": "a@x",
                                                "role": "admin", "name": "Old"}))
        # A diff is never a password: users.update would happily bcrypt one.
        self.assertNotIn("password",
                         self.handler.changes(self.spec, want, {**in_sync, "role": "admin"})
                         or {})

    def test_apply_create_and_update_never_delete(self) -> None:
        client = _FakeClient()
        create = owui_policy.Action(
            kind="entity", op="create", target="users:a@x",
            after={"email": "a@x", "name": "A", "is_admin": True},
            entity_kind="users", entity_id="a@x")
        self.handler.apply(client, create)
        body = client.calls[0]["json_body"]
        self.assertEqual(body["email"], "a@x")
        self.assertEqual(body["role"], "admin")
        self.assertTrue(body["password"])  # unusable random, never handed out
        update = owui_policy.Action(
            kind="entity", op="update", target="users:a@x",
            payload={"role": "user", "name": "A"},
            before={"id": "u-1"}, entity_kind="users", entity_id="a@x")
        self.handler.apply(client, update)
        self.assertEqual(client.calls[1]["path_params"], {"user_id": "u-1"})
        # The update body carries exactly the drifted fields -- never a password,
        # which users.update would hash and store (D-17 finding 2).
        self.assertEqual(client.calls[1]["json_body"], {"role": "user", "name": "A"})
        self.assertNotIn("password", client.calls[1]["json_body"])
        delete = owui_policy.Action(kind="entity", op="delete", target="users:a@x",
                                    entity_kind="users", entity_id="a@x")
        with self.assertRaises(owui_policy.PolicyError):
            self.handler.apply(client, delete)


# ---------------------------------------------------------------------------
# State + LKG (mocked psycopg2 via db helpers)
# ---------------------------------------------------------------------------


class TestStateAndLkg(unittest.TestCase):
    def test_state_roundtrip_through_cp_meta(self) -> None:
        with mock.patch.object(owui_policy.db, "cp_get_meta",
                               return_value={"policy_hash": "sha256:x"}) as get, \
             mock.patch.object(owui_policy.db, "cp_set_meta", return_value=True) as setm:
            state = owui_policy.load_state()
            self.assertEqual(state["policy_hash"], "sha256:x")
            get.assert_called_once_with(owui_policy.STATE_META_KEY)
            self.assertTrue(owui_policy.save_state({"policy_hash": "sha256:y"}))
            setm.assert_called_once_with(owui_policy.STATE_META_KEY,
                                         {"policy_hash": "sha256:y"})

    def test_load_state_tolerates_missing_or_bad(self) -> None:
        for junk in (None, "not-a-dict", 42):
            with mock.patch.object(owui_policy.db, "cp_get_meta", return_value=junk):
                self.assertEqual(owui_policy.load_state(), {})

    def _fake_conn(self, rows):
        conn = mock.MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = rows
        return conn, cur

    def test_write_lkg_upserts_single_slot(self) -> None:
        conn, cur = self._fake_conn(None)
        with mock.patch.object(owui_policy.db, "cp_connect", return_value=conn):
            self.assertTrue(owui_policy.write_lkg({"a": 1}, "sha256:h", "0.11.0"))
        sql = cur.execute.call_args[0][0]
        self.assertIn("owui_config_lkg", sql)
        self.assertIn("ON CONFLICT (id) DO UPDATE", sql)
        args = cur.execute.call_args[0][1]
        self.assertEqual(args[0], "sha256:h")
        self.assertEqual(json.loads(args[2]), {"a": 1})

    def test_write_lkg_swallows_db_errors(self) -> None:
        with mock.patch.object(owui_policy.db, "cp_connect",
                               side_effect=RuntimeError("pg flap")):
            self.assertFalse(owui_policy.write_lkg({}, "h", "v"))

    def test_read_lkg_shapes(self) -> None:
        import datetime
        now = datetime.datetime(2026, 10, 6, tzinfo=datetime.timezone.utc)
        with mock.patch.object(owui_policy.db, "cp_connect",
                               return_value=self._fake_conn(
                                   ("sha256:h", "0.11.0", {"k": "v"}, now))[0]):
            row = owui_policy.read_lkg()
        self.assertEqual(row["config_map"], {"k": "v"})
        self.assertTrue(row["created_at"].startswith("2026-10-06"))
        with mock.patch.object(owui_policy.db, "cp_connect",
                               return_value=self._fake_conn(None)[0]):
            self.assertIsNone(owui_policy.read_lkg())

    def test_read_lkg_decodes_json_string(self) -> None:
        with mock.patch.object(owui_policy.db, "cp_connect",
                               return_value=self._fake_conn(
                                   ("h", "0.11.0", '{"k": "v"}', None))[0]):
            row = owui_policy.read_lkg()
        self.assertEqual(row["config_map"], {"k": "v"})

    def test_restore_lkg_requires_version_match(self) -> None:
        snapshot = {"policy_hash": "h", "owui_version": "0.11.0",
                    "config_map": {"ollama.enable": False}, "created_at": "now"}
        matched = _FakeClient({"app.version": {"version": "0.11.0"}})
        result = owui_policy.restore_lkg(matched, snapshot)
        self.assertTrue(result["ok"])
        imported = next(c for c in matched.calls if c["name"] == "config.import")
        self.assertEqual(imported["json_body"], {"config": {"ollama.enable": False}})

        mismatched = _FakeClient({"app.version": {"version": "0.12.0"}})
        result = owui_policy.restore_lkg(mismatched, snapshot)
        self.assertFalse(result["ok"])
        self.assertFalse(any(c["name"] == "config.import" for c in mismatched.calls))

        empty = _FakeClient({})
        with mock.patch.object(owui_policy, "read_lkg", return_value=None):
            self.assertFalse(owui_policy.restore_lkg(empty)["ok"])


class TestReconcile(unittest.TestCase):
    """End-to-end reconcile over a stateful in-memory fake OWUI.

    The fake applies writes for real, so the second run asserts true
    idempotence (zero writes when reality matches intent).
    """

    BOOT = frozenset({"at_boot"})

    def setUp(self) -> None:
        # These are module-level globals shared with every other test class, so
        # snapshot and restore them; leaving them mutated leaks a "reconciled"
        # status into whichever class runs next (order-dependent failures). See
        # DESIGN.md "test isolation".
        self.addCleanup(setattr, owui_policy, "_last_good_manifest",
                        owui_policy._last_good_manifest)
        status_snapshot = dict(owui_policy._status)

        def _restore_status() -> None:
            owui_policy._status.clear()
            owui_policy._status.update(status_snapshot)

        self.addCleanup(_restore_status)
        owui_policy._last_good_manifest = None
        owui_policy._status.update({
            "ok": False, "state": "idle", "manifest_error": None,
            "key_ready": False, "last_error": None, "last_reconciled": None,
            "last_trigger": [], "drift": None,
        })
        self.saved: list[dict] = []
        self.lkg_writes: list[tuple] = []
        patcher_save = mock.patch.object(owui_policy, "save_state",
                                         side_effect=lambda s: self.saved.append(s) or True)
        patcher_lkg = mock.patch.object(owui_policy, "write_lkg",
                                        side_effect=self._spy_lkg)
        patcher_save.start()
        patcher_lkg.start()
        self.addCleanup(patcher_save.stop)
        self.addCleanup(patcher_lkg.stop)

    def _spy_lkg(self, config_map, policy_hash, owui_version):
        self.lkg_writes.append((config_map, policy_hash, owui_version))
        return True

    def _deps(self, fake: "FakeOwui", manifest_path=None, cp_users=None) -> owui_policy.ReconcileDeps:
        return owui_policy.ReconcileDeps(
            build_client=fake.build,
            service_identity={"email": "lemonseed-cp@localhost",
                              "name": "Lemonseed Control Plane", "role": "admin"},
            load_api_key=lambda: fake.stored_key,
            persist_api_key=lambda key: (setattr(fake, "stored_key", key), True)[1],
            refresh_context=lambda: owui_policy.ResolverContext(
                secrets={"OPENAI_API_KEY": "sk-lemon"},
                lemond_base="http://host.docker.internal/_lemonseed/lemond/v1",
                placeholders={"SERVICE_ACCOUNT_ID": "svc-1", "CP_OWUI_ID": "svc-1"},
                cp_users=cp_users if cp_users is not None else [
                    {"email": "ana@lemonseed.local", "name": "Ana",
                     "is_admin": True, "disabled": False},
                    {"email": "bob@lemonseed.local", "name": "Bob",
                     "is_admin": False, "disabled": False},
                ],
            ),
            manifest_path=manifest_path or REAL_MANIFEST,
        )

    def test_fresh_boot_converges_then_is_idempotent(self) -> None:
        fake = FakeOwui(
            config={"web.search.enable": True},  # seeded-wrong env default
            users=[{"id": "svc-1", "email": "lemonseed-cp@localhost", "role": "admin"}],
            models=[{"id": "uuid-k", "name": "Lemonseed Knockoff"}],
        )
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertTrue(state["ok"], state.get("last_error"))
        # Settings: every manifest key landed (creates for missing + update
        # for the seeded-wrong one); the bad default was corrected.
        imported = {}
        for chunk in fake.imports:
            imported.update(chunk)
        manifest = load_real_manifest()
        for key in manifest.settings:
            self.assertIn(key, imported, key)
        self.assertIs(fake.config["web.search.enable"], False)
        self.assertIs(fake.config["ollama.enable"], False)
        self.assertEqual(fake.config["openai.api_base_urls"],
                         ["http://host.docker.internal/_lemonseed/lemond/v1"])
        self.assertEqual(fake.config["openai.api_keys"], ["sk-lemon"])
        self.assertEqual(fake.config["ui.default_models"], [])
        # Keys for the CP: minted after settings applied, persisted once.
        self.assertTrue(state["key_ready"])
        self.assertEqual(fake.stored_key, "sk-1")
        self.assertEqual(fake.mints, 1)
        # Users: both created with the right roles; the service account was
        # filtered (no create, no drift entry, no update).
        by_email = {u["email"]: u for u in fake.users}
        self.assertEqual(by_email["ana@lemonseed.local"]["role"], "admin")
        self.assertEqual(by_email["bob@lemonseed.local"]["role"], "user")
        drift_keys = {e["key"] for e in state["drift"]["entries"] if e["state"] == "drift"}
        self.assertNotIn("users:lemonseed-cp@localhost", drift_keys)
        # Models: foreign reserved-name swept; zero definitions = zero creates.
        self.assertEqual(fake.model_deletes, ["uuid-k"])
        self.assertEqual(fake.model_creates, [])
        # LKG written from a fresh export on the ok run; timestamp surfaces.
        self.assertEqual(len(self.lkg_writes), 1)
        written_map, policy_hash, version = self.lkg_writes[0]
        self.assertEqual(written_map["ollama.enable"], False)
        self.assertEqual(policy_hash, manifest.policy_hash)
        self.assertEqual(version, "0.11.0")
        self.assertTrue(state["lkg_at"])
        # -- second run: nothing to do --------------------------------------
        fake.imports.clear()
        fake.user_creates.clear()
        fake.user_updates.clear()
        state2 = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertTrue(state2["ok"], state2.get("last_error"))
        self.assertEqual(fake.imports, [])
        self.assertEqual(fake.user_creates, [])
        self.assertEqual(fake.user_updates, [])
        self.assertEqual(fake.mints, 1)  # persisted key reused; no re-mint

    def test_private_model_becomes_public_and_stays_there(self) -> None:
        # Bare-metal review: the OWUI model editor defaults an entry to
        # owner-only access, which hid the chat model from every non-admin.
        fake = FakeOwui(models=[{"id": "m-1", "name": "Lemonade 8B",
                                 "access_grants": []}])
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertTrue(state["ok"], state.get("last_error"))
        self.assertEqual(fake.model_access_updates, [("m-1", [
            {"principal_type": "user", "principal_id": "*", "permission": "read"},
        ])])
        entry = next(e for e in state["drift"]["entries"]
                     if e["key"] == "models:access:m-1")
        self.assertEqual(entry["state"], "fixed")
        # Second run: the wildcard grant is present, so nothing writes.
        fake.model_access_updates.clear()
        state2 = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertTrue(state2["ok"], state2.get("last_error"))
        self.assertEqual(fake.model_access_updates, [])
        self.assertFalse(any(e["key"].startswith("models:access:")
                             for e in state2["drift"]["entries"]))

    def test_public_model_and_sweep_target_are_left_alone(self) -> None:
        fake = FakeOwui(models=[
            {"id": "m-pub", "name": "Lemonade 8B", "access_grants": [
                {"principal_type": "user", "principal_id": "*",
                 "permission": "read"},
            ]},
            {"id": "m-res", "name": "Lemonseed Knockoff"},
        ])
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertTrue(state["ok"], state.get("last_error"))
        self.assertEqual(fake.model_access_updates, [])
        self.assertEqual(fake.model_deletes, ["m-res"])  # swept, never granted

    def test_audit_tick_reports_a_private_model_but_never_writes(self) -> None:
        fake = FakeOwui(models=[{"id": "m-1", "name": "Lemonade 8B",
                                 "access_grants": []}])
        state = owui_policy.reconcile(18080, frozenset(), self._deps(fake))
        self.assertEqual(fake.model_access_updates, [])
        entry = next(e for e in state["drift"]["entries"]
                     if e["key"] == "models:access:m-1")
        self.assertEqual(entry["state"], "pending")

    def test_out_of_band_drift_fixes_at_next_boot(self) -> None:
        fake = FakeOwui()
        owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        fake.imports.clear()
        fake.user_creates.clear()
        fake.config["web.search.enable"] = True  # admin flips it in the OWUI UI
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertTrue(state["ok"])
        self.assertEqual(fake.imports, [{"web.search.enable": False}])
        entry = next(e for e in state["drift"]["entries"]
                     if e["key"] == "web.search.enable")
        self.assertEqual(entry["state"], "fixed")

    def test_audit_tick_reports_but_never_writes(self) -> None:
        fake = FakeOwui()
        state_boot = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        fake.imports.clear()
        fake.user_creates.clear()
        fake.config["web.search.enable"] = True
        # The fixture's save_state does not round-trip; emulate the real
        # cp_meta so the boot run's lkg_at is visible to the audit run.
        with mock.patch.object(owui_policy, "load_state",
                               return_value={"lkg_at": state_boot["lkg_at"]}):
            state = owui_policy.reconcile(18080, frozenset(), self._deps(fake))
        self.assertTrue(state["ok"])  # pending drift does not block the gate
        self.assertEqual(fake.imports, [])
        entry = next(e for e in state["drift"]["entries"]
                     if e["key"] == "web.search.enable")
        self.assertEqual(entry["state"], "pending")
        # LKG must not be refreshed from an observe-only tick: the drifted
        # config is not last-known-good.
        self.assertEqual(len(self.lkg_writes), 1)
        self.assertEqual(state["lkg_at"], state_boot["lkg_at"])

    def test_initial_setup_never_reverts(self) -> None:
        fake = FakeOwui()
        owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        fake.config["ui.default_models"] = ["user-picked-model"]
        fake.imports.clear()
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertTrue(state["ok"])
        self.assertNotIn("ui.default_models", {k for c in fake.imports for k in c})
        self.assertEqual(fake.config["ui.default_models"], ["user-picked-model"])
        entry = next(e for e in state["drift"]["entries"] if e["key"] == "ui.default_models")
        self.assertEqual(entry["state"], "ok")

    def test_user_role_push_both_directions(self) -> None:
        fake = FakeOwui(users=[
            {"id": "svc-1", "email": "lemonseed-cp@localhost", "role": "admin"},
            {"id": "u-a", "email": "ana@lemonseed.local", "role": "user"},
            {"id": "u-b", "email": "bob@lemonseed.local", "role": "admin"},
        ])
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertTrue(state["ok"])
        roles = {u["email"]: u["role"] for u in fake.users}
        self.assertEqual(roles["ana@lemonseed.local"], "admin")   # elevated
        self.assertEqual(roles["bob@lemonseed.local"], "user")    # demoted
        self.assertEqual(roles["lemonseed-cp@localhost"], "admin")  # untouched

    def test_reconcile_matches_accounts_by_owui_id(self) -> None:
        # D-18 end to end: the CP holds u-a/u-b, and OWUI's rows carry emails
        # an admin edited by hand. Matching on id still converges, and nothing
        # is created twice (the D-15 duplicate-account class stays shut).
        fake = FakeOwui(users=[
            {"id": "svc-1", "email": "lemonseed-cp@localhost", "role": "admin"},
            {"id": "u-a", "email": "ana@edited.example", "role": "user",
             "name": "Ana"},
            {"id": "u-b", "email": "bob@edited.example", "role": "admin",
             "name": "Bob"},
        ])
        deps = self._deps(fake, cp_users=[
            {"email": "ana@localhost", "name": "Ana", "is_admin": True,
             "disabled": False, "owui_id": "u-a"},
            {"email": "bob@localhost", "name": "Bob", "is_admin": False,
             "disabled": False, "owui_id": "u-b"},
        ])
        state = owui_policy.reconcile(18080, self.BOOT, deps)
        self.assertTrue(state["ok"], state.get("last_error"))
        self.assertEqual(fake.user_creates, [])
        self.assertEqual(len(fake.users), 3)  # nothing added, nothing removed
        roles = {u["id"]: u["role"] for u in fake.users}
        self.assertEqual(roles["u-a"], "admin")   # elevated by id, not by email
        self.assertEqual(roles["u-b"], "user")    # demoted

    def test_reconcile_unknown_owui_id_matches_by_email_not_duplicates(self) -> None:
        # owui_id unknown (the create path could not reach OWUI): the row must
        # meet the existing account at its identity email instead of minting a
        # second one, which OWUI would reject with EMAIL_TAKEN anyway.
        fake = FakeOwui(users=[
            {"id": "svc-1", "email": "lemonseed-cp@localhost", "role": "admin"},
            {"id": "u-a", "email": "ana@lemonseed.local", "role": "user",
             "name": "Ana"},
        ])
        deps = self._deps(fake, cp_users=[
            {"email": "ana@lemonseed.local", "name": "Ana", "is_admin": True,
             "disabled": False, "owui_id": ""},
        ])
        state = owui_policy.reconcile(18080, self.BOOT, deps)
        self.assertTrue(state["ok"], state.get("last_error"))
        self.assertEqual(fake.user_creates, [])
        self.assertEqual(len(fake.users), 2)
        self.assertEqual([u["role"] for u in fake.users if u["id"] == "u-a"], ["admin"])

    def test_reconcile_display_name_rename_converges_and_is_idempotent(self) -> None:
        fake = FakeOwui(users=[
            {"id": "svc-1", "email": "lemonseed-cp@localhost", "role": "admin"},
            {"id": "u-a", "email": "ana@localhost", "role": "admin", "name": "Ana"},
        ])
        deps = self._deps(fake, cp_users=[
            {"email": "ana@localhost", "name": "Ana Maria", "is_admin": True,
             "disabled": False, "owui_id": "u-a"},
        ])
        state = owui_policy.reconcile(18080, self.BOOT, deps)
        self.assertTrue(state["ok"], state.get("last_error"))
        # Exactly the rename: never an email, never a password.
        self.assertEqual(fake.user_updates, [("u-a", {"name": "Ana Maria"})])
        fake.user_updates.clear()
        owui_policy.reconcile(18080, self.BOOT, deps)
        self.assertEqual(fake.user_updates, [])

    def test_reconcile_never_deletes_an_account(self) -> None:
        # One row owned by id, one foreign row, one unknown-id desired row: no
        # tick, whatever its triggers, may remove an OWUI account (DESIGN rule;
        # deletion is the portal handler's job — D-16a).
        fake = FakeOwui(users=[
            {"id": "svc-1", "email": "lemonseed-cp@localhost", "role": "admin"},
            {"id": "u-a", "email": "ana@localhost", "role": "user", "name": "Ana"},
            {"id": "u-x", "email": "ghost@localhost", "role": "user", "name": "Ghost"},
        ])
        deps = self._deps(fake, cp_users=[
            {"email": "ana@localhost", "name": "Ana", "is_admin": True,
             "disabled": False, "owui_id": "u-a"},
            {"email": "bob@localhost", "name": "Bob", "is_admin": False,
             "disabled": False, "owui_id": ""},
        ])
        boot = owui_policy.reconcile(18080, self.BOOT, deps)
        self.assertTrue(boot["ok"], boot.get("last_error"))
        ids = [u["id"] for u in fake.users]
        self.assertIn("u-x", ids)        # the foreign account survives
        self.assertIn("svc-1", ids)      # so does the service account
        self.assertIn("bob@localhost", [u["email"] for u in fake.users])
        self.assertEqual(len(ids), 4)    # exactly one create (bob), no removals
        owui_policy.reconcile(18080, frozenset(), deps)
        self.assertEqual([u["id"] for u in fake.users], ids)  # audit tick: no writes
        drift_keys = {e["key"] for e in boot["drift"]["entries"]
                      if e["state"] == "drift"}
        self.assertEqual(drift_keys, {"users:ghost@localhost"})

    def test_reconcile_stale_owui_id_reports_instead_of_deleting(self) -> None:
        # The CP holds an id OWUI no longer has while a row sits at that email
        # (deleted and re-created by hand, say). The loop cannot update a row it
        # cannot find, so it attempts the create; real OWUI answers EMAIL_TAKEN,
        # which surfaces as a failed action — it must never be "fixed" by
        # deleting anyone.
        class EmailTakenOwui(FakeOwui):
            """Real OWUI refuses a second row at a taken email (users.py)."""

            def call(self, name, *, path_params=None, json_body=None, **kwargs):
                if name == "users.create":
                    return {"ok": False, "status": 400, "data": None,
                            "error": "EMAIL_TAKEN"}
                return super().call(name, path_params=path_params,
                                    json_body=json_body, **kwargs)

        fake = EmailTakenOwui(users=[
            {"id": "svc-1", "email": "lemonseed-cp@localhost", "role": "admin"},
            {"id": "u-9", "email": "ana@localhost", "role": "user", "name": "Ana"},
        ])

        deps = self._deps(fake, cp_users=[
            {"email": "ana@localhost", "name": "Ana", "is_admin": True,
             "disabled": False, "owui_id": "u-gone"},
        ])
        state = owui_policy.reconcile(18080, self.BOOT, deps)
        self.assertFalse(state["ok"])              # drift is reported, not hidden
        self.assertIn("EMAIL_TAKEN", state["last_error"])
        self.assertEqual(len(fake.users), 2)       # nothing deleted, nothing added

    def test_foreign_owui_account_is_drift_not_delete(self) -> None:
        fake = FakeOwui(users=[
            {"id": "svc-1", "email": "lemonseed-cp@localhost", "role": "admin"},
            {"id": "u-x", "email": "ghost@lemonseed.local", "role": "user"},
        ])
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertTrue(state["ok"])  # drift never blocks; loop never deletes
        self.assertEqual([u["email"] for u in fake.users].count("ghost@lemonseed.local"), 1)
        entry = next(e for e in state["drift"]["entries"]
                     if e["key"] == "users:ghost@lemonseed.local")
        self.assertEqual(entry["state"], "drift")

    def test_version_gate_refuses_writes(self) -> None:
        fake = FakeOwui(version="0.9.9")
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertFalse(state["ok"])
        self.assertEqual(fake.imports, [])
        self.assertIn("min_version", state["last_error"])

    def test_bad_manifest_after_good_keeps_last_good(self) -> None:
        fake = FakeOwui()
        self.assertTrue(owui_policy.reconcile(18080, self.BOOT,
                                              self._deps(fake))["ok"])
        broken = Path(tempfile.mkdtemp()) / "broken-policy.json"
        broken.write_text("{not json", encoding="utf-8")
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake, broken))
        self.assertTrue(state["ok"])  # last-good manifest keeps reconciling
        self.assertIsNotNone(owui_policy.state_summary()["manifest_error"])

    def test_bad_manifest_with_no_last_good_refuses(self) -> None:
        fake = FakeOwui()
        broken = Path(tempfile.mkdtemp()) / "broken-policy2.json"
        broken.write_text("{not json", encoding="utf-8")
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake, broken))
        self.assertFalse(state["ok"])
        self.assertIn("no valid manifest", state["last_error"])

    def test_apply_failure_records_and_converges_next_run(self) -> None:
        fake = FakeOwui(fail_import_keys={"ollama.enable"})
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertFalse(state["ok"])
        entry = next(e for e in state["drift"]["entries"] if e["key"] == "ollama.enable")
        self.assertEqual(entry["state"], "error")
        self.assertIn("ollama.enable", state["last_error"])
        state2 = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertTrue(state2["ok"])  # retry converges
        self.assertIs(fake.config["ollama.enable"], False)

    def test_signin_failure_aborts_run(self) -> None:
        fake = FakeOwui(signin_ok=False)
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertFalse(state["ok"])
        self.assertEqual(fake.imports, [])

    def test_reconcile_never_raises(self) -> None:
        def _boom(*_a, **_k):
            raise RuntimeError("client exploded")

        deps = self._deps(FakeOwui())
        deps.build_client = _boom
        state = owui_policy.reconcile(18080, self.BOOT, deps)
        self.assertFalse(state["ok"])
        self.assertIn("client exploded", state["last_error"])

    def test_lock_busy_skips_without_side_effects(self) -> None:
        fake = FakeOwui()
        owui_policy._reconcile_lock.acquire()
        self.addCleanup(owui_policy._reconcile_lock.release)
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertIn("skipped", state)
        self.assertEqual(fake.signins, 0)

    def test_state_summary_shape(self) -> None:
        fake = FakeOwui()
        owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        summary = owui_policy.state_summary()
        self.assertTrue(summary["ok"])
        self.assertTrue(summary["key_ready"])
        self.assertEqual(summary["owui_version"], "0.11.0")
        self.assertEqual(summary["last_trigger"], ["at_boot"])
        self.assertTrue(summary["lkg_at"])
        self.assertIn("entries", summary["drift"])

    def test_serialized_state_never_carries_the_plaintext_api_key(self) -> None:
        """The cp_meta snapshot is served by /api/modules (auth-gated, not
        admin-gated), so a managed secret in it is readable by any member.
        The engine must still *apply* the real key while serializing it masked.
        """
        fake = FakeOwui(config={"openai.api_keys": ["sk-stray-key"]})
        state = owui_policy.reconcile(18080, self.BOOT, self._deps(fake))
        self.assertTrue(state["ok"], state.get("last_error"))
        self.assertEqual(fake.config["openai.api_keys"], ["sk-lemon"])  # applied for real
        entry = next(e for e in state["drift"]["entries"] if e["key"] == "openai.api_keys")
        self.assertEqual(entry["state"], "fixed")
        self.assertEqual(entry["found"], owui_policy.MASK)
        self.assertEqual(entry["want"], owui_policy.MASK)
        blob = json.dumps(state)
        self.assertNotIn("sk-stray-key", blob, "observed secret must not be serialized")
        self.assertNotIn("sk-lemon", blob, "intended secret must not be serialized")
        self.assertNotIn("sk-lemon", json.dumps(self.saved[-1]))


class TestMasking(unittest.TestCase):
    """Display redaction (Phase 4A).

    ``configs/export`` is unredacted and ``/api/modules`` is member-visible, so
    masking happens at serialization: ``plan()`` keeps seeing real values (it
    must, to detect drift) and only display payloads get the mask.
    """

    def test_secret_value_kind_is_authoritative(self) -> None:
        spec = owui_policy.ValueSpec("secret", name="OPENAI_API_KEY")
        self.assertTrue(owui_policy.is_secret_key("openai.credentials_blob", spec))

    def test_secret_leaf_names_are_matched_exactly(self) -> None:
        for key in ("openai.api_keys", "service.api_key", "tts.speech_to_text.token",
                    "service.client_secret", "storage.private_key"):
            self.assertTrue(owui_policy.is_secret_key(key), key)
        # Flags and path lists merely *mention* keys; they are not credentials.
        for key in ("auth.enable_api_keys", "auth.api_key.allowed_endpoints",
                    "auth.api_key.endpoint_restrictions", "user.permissions",
                    "openai.api_configs", "ui.api_key_label"):
            self.assertFalse(owui_policy.is_secret_key(key), key)

    def test_secret_value_is_replaced_by_the_mask(self) -> None:
        self.assertEqual(owui_policy.mask_setting("openai.api_keys", ["sk-1", "sk-2"]),
                         owui_policy.MASK)

    def test_flags_and_numbers_pass_through(self) -> None:
        self.assertIs(owui_policy.mask_setting("auth.enable_api_keys", True), True)
        self.assertIs(owui_policy.mask_setting("web.search.enable", False), False)
        self.assertEqual(owui_policy.mask_setting("rag.top_k", 5), 5)
        self.assertIsNone(owui_policy.mask_setting("ui.default_models", None))

    def test_structured_values_keep_shape_with_secret_leaves_masked(self) -> None:
        value = {"0": {"api_key": "sk-1", "openai_api_base_url": "http://x",
                       "display_name": "Lemonade"}}
        masked = owui_policy.mask_setting("openai.api_configs", value)
        self.assertEqual(masked, {"0": {"api_key": owui_policy.MASK,
                                        "openai_api_base_url": "http://x",
                                        "display_name": "Lemonade"}})
        # Masking must not mutate the value the planner still needs.
        self.assertEqual(value["0"]["api_key"], "sk-1")

    def test_nested_list_items_are_masked(self) -> None:
        masked = owui_policy.mask_setting(
            "tool_server.connections",
            [{"name": "ok", "bearer_token": "tok"}],
        )
        self.assertEqual(masked, [{"name": "ok", "bearer_token": owui_policy.MASK}])

    def test_report_entry_masks_secret_setting_values(self) -> None:
        manifest = manifest_with({
            "openai.api_keys": owui_policy.SettingEntry(
                key="openai.api_keys",
                spec=owui_policy.ValueSpec("secret", name="OPENAI_API_KEY", wrap_list=True),
                mode="managed", cadence="at_boot", optional=False, reason="t",
            ),
        })
        entry = owui_policy.ReportEntry(
            key="openai.api_keys", mode="managed", cadence="at_boot", state="pending",
            found=["sk-live"], want=["sk-want"], note="drift",
        )
        out = owui_policy.masked_report_entry(entry, manifest)
        self.assertEqual(out["found"], owui_policy.MASK)
        self.assertEqual(out["want"], owui_policy.MASK)
        self.assertEqual(out["state"], "pending")
        self.assertEqual(out["note"], "drift")
        self.assertEqual(entry.found, ["sk-live"], "the entry itself must stay intact")

    def test_report_entry_masks_person_labels(self) -> None:
        entry = owui_policy.ReportEntry(
            key="users:bob@lemonseed.local", mode="full_management", cadence=None,
            state="drift", found="bob@lemonseed.local", want=None,
            note="exists in Open WebUI but not in Lemonseed",
        )
        out = owui_policy.masked_report_entry(entry, manifest_with())
        self.assertEqual(out["found"], owui_policy.MASK)
        self.assertEqual(out["key"], "users:bob@lemonseed.local")
        self.assertEqual(out["state"], "drift")

    def test_model_labels_stay_readable(self) -> None:
        # Model drift is reported by name so an admin can see the collision.
        entry = owui_policy.ReportEntry(
            key="models:lemonseed-ai", mode="selective_management", cadence=None,
            state="pending", found="Lemonseed AI knockoff", note="reserved-name collision",
        )
        out = owui_policy.masked_report_entry(entry, manifest_with())
        self.assertEqual(out["found"], "Lemonseed AI knockoff")


class FakeOwui:
    """Small stateful Open WebUI stand-in: applies writes for real."""

    def __init__(self, config=None, users=None, models=None, version="0.11.0",
                 fail_import_keys=(), signin_ok: bool = True) -> None:
        self.config = dict(config or {})
        self.users = list(users or [])
        self.models = list(models or [])
        self.version = version
        self.signin_ok = signin_ok
        self.fail_import_keys = set(fail_import_keys)
        self.imports: list[dict] = []
        self.model_creates: list[str] = []
        self.model_updates: list[str] = []
        self.model_access_updates: list[tuple[str, list[dict]]] = []
        self.model_deletes: list[str] = []
        self.user_creates: list[str] = []
        self.user_updates: list = []
        self.signins = 0
        self.mints = 0
        self.stored_key: Optional[str] = None
        self.api_key: Optional[str] = None
        self._on_remint = None
        self._next_id = 100

    def build(self, api_map, api_key, on_remint):
        self.api_key = api_key or self.api_key
        self._on_remint = on_remint
        return self

    # -- client surface used by reconcile ------------------------------------

    def sign_in_trusted(self, **kwargs):
        self.signins += 1
        if not self.signin_ok:
            return {"ok": False, "status": 503, "data": None, "error": "boom"}
        return {"ok": True, "status": 200,
                "data": {"token": "jwt-1", "id": "svc-1"}, "error": None}

    def app_version(self):
        return {"ok": True, "status": 200, "data": {"version": self.version}, "error": None}

    def config_export(self):
        return {"ok": True, "status": 200, "data": copy.deepcopy(self.config), "error": None}

    def config_import(self, config_map):
        self.imports.append(dict(config_map))
        for key, value in config_map.items():
            if key in self.fail_import_keys:
                self.fail_import_keys.discard(key)
                return {"ok": False, "status": 500, "data": None, "error": "simulated"}
            self.config[key] = value
        return {"ok": True, "status": 200, "data": True, "error": None}

    def mint_api_key(self):
        self.mints += 1
        self.api_key = f"sk-{self.mints}"
        if self._on_remint:
            self._on_remint(self.api_key)
        return self.api_key

    def call(self, name, *, path_params=None, json_body=None, **kwargs):
        ok = {"ok": True, "status": 200, "error": None}
        if name == "models.export":
            return {**ok, "data": copy.deepcopy(self.models)}
        if name == "users.all":
            # Match v0.11.0 UserInfoListResponse, not the bare-list guess the
            # first fakes baked in.
            users = copy.deepcopy(self.users)
            return {**ok, "data": {"users": users, "total": len(users)}}
        if name == "models.create":
            self.models.append(copy.deepcopy(json_body))
            self.model_creates.append(json_body["id"])
            return {**ok, "data": json_body}
        if name == "models.update":
            for model in self.models:
                if model.get("id") == json_body.get("id"):
                    model.update(json_body)
            self.model_updates.append(json_body.get("id"))
            return {**ok, "data": json_body}
        if name == "models.access_update":
            # v0.11.0: replaces the whole grant list; backfills a minimal row
            # for a non-preset model that has none yet.
            grants = copy.deepcopy(json_body.get("access_grants", []))
            row = next((m for m in self.models if m.get("id") == json_body.get("id")), None)
            if row is None:
                row = {"id": json_body["id"], "name": json_body["id"]}
                self.models.append(row)
            row["access_grants"] = grants
            self.model_access_updates.append((json_body["id"], grants))
            return {**ok, "data": json_body}
        if name == "models.delete":
            self.models = [m for m in self.models if m.get("id") != json_body.get("id")]
            self.model_deletes.append(json_body["id"])
            return {**ok, "data": True}
        if name == "users.create":
            self._next_id += 1
            self.users.append({
                "id": f"u-{self._next_id}",
                "email": json_body["email"], "name": json_body.get("name", ""),
                "role": json_body.get("role", "user"),
            })
            self.user_creates.append(json_body["email"])
            return {**ok, "data": {"id": f"u-{self._next_id}"}}
        if name == "users.update":
            for user in self.users:
                if user["id"] == (path_params or {}).get("user_id"):
                    user.update(json_body or {})
                    self.user_updates.append((user["id"], json_body))
            return {**ok, "data": True}
        return {"ok": False, "status": 404, "data": None, "error": f"fake: {name}"}


if __name__ == "__main__":
    unittest.main()
