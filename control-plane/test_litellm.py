#!/usr/bin/env python3
"""Unit tests for litellm.py (mocked docker; no containers required)."""

from __future__ import annotations

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import litellm  # noqa: E402
from image_pins import digest_ref, tag_ref  # noqa: E402


class ConfigRenderTests(unittest.TestCase):
    """The generated config.yaml is the whole product here: exact bytes, or the
    hash-driven recreate below has nothing to stand on."""

    def setUp(self) -> None:
        self._key_patcher = mock.patch.object(litellm, "master_key", return_value="sk-master-1")
        self._key_patcher.start()
        self._lemond_key_patcher = mock.patch.object(
            litellm.hardware, "lemonade_api_key", return_value="sk-lemond-1"
        )
        self._lemond_key_patcher.start()
        self.addCleanup(self._key_patcher.stop)
        self.addCleanup(self._lemond_key_patcher.stop)

    def test_both_entities_same_backend_no_lemond_name_leak(self) -> None:
        text = litellm.render_config("qwen3-8b", filtered_enabled=True)
        self.assertIn(f"model_name: {litellm.ENTITY_PLAIN}", text)
        self.assertIn(f"model_name: {litellm.ENTITY_FILTERED}", text)
        self.assertEqual(text.count("model: openai/qwen3-8b"), 2)
        self.assertIn(f"api_base: {litellm.LEMOND_BASE}", text)
        # The active model's native name must never appear as a servable name.
        self.assertNotIn("model_name: qwen3-8b", text)

    def test_filtered_entity_only_when_enabled(self) -> None:
        plain = litellm.render_config("qwen3-8b", filtered_enabled=False)
        self.assertNotIn(litellm.ENTITY_FILTERED, plain)
        self.assertNotIn("guardrails:", plain)
        full = litellm.render_config("qwen3-8b", filtered_enabled=True)
        self.assertIn(f"guardrails: [{litellm.GUARDRAIL_NAME}]", full)
        self.assertIn(f"guardrail: {litellm.GUARDRAIL_ID}", full)
        # Exactly one top-level guardrails block; the per-model attachment is
        # indented inside litellm_params (verified in its own test above).
        self.assertEqual(full.count("\nguardrails:"), 1)
        self.assertEqual(full.count(f"- model_name: {litellm.ENTITY_PLAIN}\n"), 1)

    def test_guardrail_attaches_inside_litellm_params(self) -> None:
        text = litellm.render_config("m1", filtered_enabled=True)
        filtered = text.split(f"- model_name: {litellm.ENTITY_FILTERED}", 1)[1]
        self.assertIn(f"      guardrails: [{litellm.GUARDRAIL_NAME}]\n", filtered)

    def test_master_key_lives_in_general_settings(self) -> None:
        text = litellm.render_config("m1", filtered_enabled=True)
        self.assertIn("general_settings:\n  master_key: sk-master-1", text)
        self.assertNotIn("litellm_settings:", text)

    def test_render_is_deterministic(self) -> None:
        one = litellm.render_config("m1", filtered_enabled=True)
        two = litellm.render_config("m1", filtered_enabled=True)
        self.assertEqual(one, two)
        self.assertNotEqual(one, litellm.render_config("m2", filtered_enabled=True))

    def test_mcp_section_reserved_but_empty(self) -> None:
        text = litellm.render_config("m1", filtered_enabled=True)
        self.assertIn("mcp_servers: []", text)


class MasterKeyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.master_key_path = root / "master-key"
        self.config_path = root / "config.yaml"
        self._orig = (litellm.MASTER_KEY_PATH, litellm.CONFIG_PATH, litellm.DATA_ROOT)
        litellm.MASTER_KEY_PATH = self.master_key_path
        litellm.CONFIG_PATH = self.config_path
        litellm.DATA_ROOT = root
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        litellm.MASTER_KEY_PATH, litellm.CONFIG_PATH, litellm.DATA_ROOT = self._orig

    def test_generated_once_persisted_0600_and_reused(self) -> None:
        first = litellm.master_key()
        self.assertTrue(first.startswith("sk-lemonseed-"))
        mode = stat.S_IMODE(self.master_key_path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(litellm.master_key(), first)

    def test_existing_key_file_wins(self) -> None:
        self.master_key_path.write_text("sk-existing\n", encoding="utf-8")
        self.assertEqual(litellm.master_key(), "sk-existing")

    def test_unwritable_path_still_returns_a_key(self) -> None:
        with mock.patch.object(Path, "mkdir", side_effect=OSError("no space")):
            key = litellm.master_key()
        self.assertTrue(key.startswith("sk-lemonseed-"))


class PinTests(unittest.TestCase):
    def test_litellm_pin_is_digest_shaped(self) -> None:
        self.assertEqual(tag_ref("litellm"), litellm.LITELLM_IMAGE)
        self.assertRegex(digest_ref("litellm"), r"^ghcr.io/berriai/litellm@sha256:[0-9a-f]{64}$")

    def test_owui_policy_base_models_match_litellm_entities(self) -> None:
        """The override rows OWUI creates must carry exactly the ids the proxy
        serves — the row merges into the connection model by id, so a rename
        on one side silently breaks both model entries."""
        policy = json.loads((CP / "owui-policy.json").read_text(encoding="utf-8"))
        models = policy["entities"]["models"]
        self.assertEqual(
            sorted(models["own_ids"]),
            sorted([litellm.ENTITY_PLAIN, litellm.ENTITY_FILTERED]),
        )
        defs = models["definitions"]
        for entity in (litellm.ENTITY_PLAIN, litellm.ENTITY_FILTERED):
            self.assertEqual(defs[entity]["id"], entity)
            self.assertIsNone(defs[entity]["base_model_id"])

    def test_policy_connections_derive_from_litellm(self) -> None:
        policy = json.loads((CP / "owui-policy.json").read_text(encoding="utf-8"))
        entries = policy["settings"]["entries"]
        self.assertEqual(entries["openai.api_base_urls"]["value"]["derived"], "litellm_base")
        self.assertEqual(entries["openai.api_keys"]["value"]["secret"], "LITELLM_MASTER_KEY")
        self.assertEqual(entries["openai.api_configs"]["value"]["derived"], "litellm_api_configs")
        # The whole cutover set is optional: unresolved while the proxy is not
        # serving must keep the reconcile gate green (OWUI stays on lemond),
        # never flip the box into a permanent policy error.
        for key in ("openai.api_base_urls", "openai.api_keys", "openai.api_configs"):
            self.assertTrue(entries[key].get("optional"), key)


class SupervisorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.master_key_path = root / "master-key"
        self.config_path = root / "config.yaml"
        self.applied_hash_path = root / "config.sha256"
        self._orig = (
            litellm.MASTER_KEY_PATH,
            litellm.CONFIG_PATH,
            litellm.DATA_ROOT,
            litellm.APPLIED_HASH_PATH,
        )
        litellm.MASTER_KEY_PATH = self.master_key_path
        litellm.CONFIG_PATH = self.config_path
        litellm.DATA_ROOT = root
        litellm.APPLIED_HASH_PATH = self.applied_hash_path
        self.addCleanup(self._restore)
        litellm._state.clear()
        litellm._state.update({litellm.LITELLM_CONTAINER: {"state": "starting"}})

    def _restore(self) -> None:
        (
            litellm.MASTER_KEY_PATH,
            litellm.CONFIG_PATH,
            litellm.DATA_ROOT,
            litellm.APPLIED_HASH_PATH,
        ) = self._orig

    def _model(self, model_id: str | None) -> mock.MagicMock:
        return mock.patch.object(
            litellm.chat_ready, "read_chat_ready", return_value={"model_id": model_id}
        )

    def test_no_model_yet_is_waiting_not_an_error(self) -> None:
        with self._model(None), mock.patch.object(litellm, "_docker") as docker:
            litellm.supervise_litellm_once()
        self.assertEqual(litellm._state[litellm.LITELLM_CONTAINER]["state"], "waiting")
        docker.assert_not_called()

    def test_first_tick_writes_config_then_starts(self) -> None:
        success = mock.Mock(returncode=0, stdout="", stderr="")

        def docker(args: list[str], timeout: float = 120) -> mock.Mock:
            return success if args[0] == "run" else mock.Mock(returncode=1, stdout="", stderr="")

        with (
            self._model("qwen3-8b"),
            mock.patch.object(litellm, "_container_running", return_value=False),
            mock.patch.object(litellm, "_container_exists", return_value=False),
            mock.patch.object(litellm, "_port_open", return_value=False),
            mock.patch.object(litellm, "_pull_image", return_value=(True, "present")),
            mock.patch.object(
                litellm, "_resolve_ref", return_value="ghcr.io/berriai/litellm@sha256:x"
            ),
            mock.patch.object(litellm, "_ensure_lemon_network", return_value=True),
            mock.patch.object(litellm, "_docker", side_effect=docker) as docker_mock,
        ):
            litellm.supervise_litellm_once()
        self.assertEqual(litellm._state[litellm.LITELLM_CONTAINER]["state"], "starting")
        self.assertTrue(self.config_path.is_file())
        run_calls = [c for c in docker_mock.call_args_list if c.args[0][0] == "run"]
        self.assertEqual(len(run_calls), 1)
        args = run_calls[0].args[0]
        self.assertIn("--network", args)
        self.assertEqual(args[args.index("--network") + 1], "lemon")
        self.assertIn("127.0.0.1:4000:4000", args)
        self.assertIn("--config", args)
        self.assertIn("--add-host", args)

    def test_ready_when_running_and_port_serving(self) -> None:
        with (
            self._model("m1"),
            mock.patch.object(litellm, "_container_running", return_value=True),
            mock.patch.object(litellm, "_container_exists", return_value=True),
            mock.patch.object(litellm, "_port_open", return_value=True),
            mock.patch.object(litellm, "_docker") as docker,
        ):
            litellm.write_config(litellm.render_config("m1", filtered_enabled=True))
            litellm._set_applied_hash(litellm.config_fingerprint())
            litellm.supervise_litellm_once()
        self.assertEqual(litellm._state[litellm.LITELLM_CONTAINER]["state"], "ready")
        docker.assert_not_called()

    def test_model_change_recreates_container(self) -> None:
        self.config_path.write_text("old\n", encoding="utf-8")
        running = mock.Mock(returncode=0, stdout="true", stderr="")
        with (
            self._model("m2"),
            mock.patch.object(litellm, "_container_running", return_value=True),
            mock.patch.object(litellm, "_container_exists", return_value=True),
            mock.patch.object(litellm, "_port_open", return_value=False),
            mock.patch.object(litellm, "_docker", return_value=running) as docker,
        ):
            # A recent start keeps the grace window open: the only rm must be
            # the config-change one, not a wedged-container recreate.
            litellm._state[litellm.LITELLM_CONTAINER] = {
                "state": "starting",
                "since": litellm.time.monotonic(),
            }
            litellm.supervise_litellm_once()
        self.assertTrue(self.config_path.read_text(encoding="utf-8").startswith("# generated"))
        rm_calls = [c for c in docker.call_args_list if c.args[0][:2] == ["rm", "-f"]]
        self.assertEqual(len(rm_calls), 1)

    def test_unchanged_config_does_not_recreate(self) -> None:
        litellm.write_config(litellm.render_config("m1", filtered_enabled=True))
        litellm._set_applied_hash(litellm.config_fingerprint())
        litellm._state[litellm.LITELLM_CONTAINER] = {
            "state": "starting",
            "since": litellm.time.monotonic(),
        }
        running = mock.Mock(returncode=0, stdout="true", stderr="")
        with (
            self._model("m1"),
            mock.patch.object(litellm, "_container_running", return_value=True),
            mock.patch.object(litellm, "_port_open", return_value=False),
            mock.patch.object(litellm, "_docker", return_value=running) as docker,
        ):
            litellm.supervise_litellm_once()
            litellm.supervise_litellm_once()
        self.assertEqual(litellm._state[litellm.LITELLM_CONTAINER]["state"], "starting")
        rm_calls = [c for c in docker.call_args_list if c.args[0][:2] == ["rm", "-f"]]
        self.assertEqual(rm_calls, [])


class SnapshotTests(unittest.TestCase):
    def test_snapshot_shape(self) -> None:
        snap = litellm.litellm_snapshot()
        self.assertEqual(snap["container"], litellm.LITELLM_CONTAINER)
        self.assertEqual(snap["endpoint"], litellm.LITELLM_BASE)
        self.assertIn("state", snap)

    def test_data_snapshot_includes_litellm(self) -> None:
        import data_services

        snap = data_services.data_snapshot()
        self.assertIn("litellm", snap)
        self.assertEqual(snap["litellm"]["container"], litellm.LITELLM_CONTAINER)


class CutoverTests(unittest.TestCase):
    """The OWUI connection is owned unconditionally (like every core service):
    the proxy is the one OpenAI-compatible endpoint, and the three connection
    settings flip together at the very first reconcile. The only degradation
    path is a broken litellm module itself, which must report unresolved
    without blocking the reconcile gate (the manifest marks the entries
    optional for exactly that)."""

    def test_policy_context_injects_connection_unconditionally(self) -> None:
        with (
            mock.patch.object(litellm, "litellm_ready", return_value=False),
            mock.patch.object(litellm, "master_key", return_value="sk-ready"),
        ):
            from modules import _policy_context

            ctx = _policy_context()
        self.assertEqual(ctx.litellm_base, litellm.LITELLM_BASE)
        self.assertEqual(ctx.secrets.get("LITELLM_MASTER_KEY"), "sk-ready")
        self.assertEqual(ctx.lemond_base, "http://host.docker.internal/_lemonseed/lemond/v1")

    def test_broken_litellm_module_reports_unresolved_not_a_gate_error(self) -> None:
        # Simulates the import failing inside _policy_context: no litellm base,
        # no master key — the resolvers then return skip-with-drift and the
        # optional manifest entries keep the reconcile gate green.
        with mock.patch.dict(sys.modules, {"litellm": None}):
            from modules import _policy_context

            ctx = _policy_context()
        self.assertEqual(ctx.litellm_base, "")
        self.assertNotIn("LITELLM_MASTER_KEY", ctx.secrets)
        entry = mock.Mock()
        entry.spec = mock.Mock(kind="derived", name="litellm_base", wrap_list=True)
        value, err = __import__("owui_policy").resolve_setting_value(entry, ctx)
        self.assertIsNone(value)
        self.assertIn("litellm", err)


class ResolverTests(unittest.TestCase):
    def test_litellm_base_resolver(self) -> None:
        import owui_policy

        entry = owui_policy.SettingEntry(
            key="k",
            spec=owui_policy.ValueSpec("derived", name="litellm_base", wrap_list=True),
            mode="managed", cadence="at_boot", optional=False, reason="t",
        )
        value, err = owui_policy.resolve_setting_value(
            entry, owui_policy.ResolverContext(litellm_base=litellm.LITELLM_BASE)
        )
        self.assertEqual(value, [litellm.LITELLM_BASE])
        value, err = owui_policy.resolve_setting_value(entry, owui_policy.ResolverContext())
        self.assertIsNone(value)
        self.assertIn("litellm", err)

    def test_litellm_api_configs_shape(self) -> None:
        import owui_policy

        ctx = owui_policy.ResolverContext(litellm_base=litellm.LITELLM_BASE)
        self.assertEqual(
            owui_policy.RESOLVERS["litellm_api_configs"](ctx),
            {litellm.LITELLM_BASE: {"valves": {}}},
        )


if __name__ == "__main__":
    unittest.main()
