#!/usr/bin/env python3
"""Unit tests for Open WebUI module lifecycle (mocked docker/nginx)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import modules  # noqa: E402


class ModulesTests(unittest.TestCase):
    def setUp(self) -> None:
        modules._state = None
        modules._health_fail_count = 0
        modules._worker = None
        if modules._lock.locked():
            modules._lock.release()

    def tearDown(self) -> None:
        modules._worker = None
        modules._health_fail_count = 0
        if modules._lock.locked():
            modules._lock.release()

    def test_other_port(self) -> None:
        self.assertEqual(modules._other_port(18080), 18081)
        self.assertEqual(modules._other_port(18081), 18080)

    def test_owui_image_default_is_pinned_release(self) -> None:
        """Module images must pin a stable tag, not floating :latest/:main."""
        image = modules.OWUI_IMAGE_DEFAULT
        self.assertTrue(
            image.startswith("ghcr.io/open-webui/open-webui:v"),
            f"unexpected image registry/name: {image}",
        )
        self.assertIn(":v0.11.", image)
        self.assertNotIn(":latest", image)
        self.assertNotIn(":main", image)
        # Default runtime pin matches the documented default unless overridden.
        self.assertEqual(modules.OWUI_IMAGE, modules.OWUI_IMAGE_DEFAULT)

    def test_http_ready_requires_version_json(self) -> None:
        class _Resp:
            status = 200

            def read(self, _n: int = -1) -> bytes:
                return b'{"version":"0.11.0"}'

            def __enter__(self) -> "_Resp":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        with mock.patch("modules.urlopen", return_value=_Resp()) as opener:
            self.assertTrue(modules.http_ready(18080))
            opener.assert_called_once()
            self.assertIn("/_app/version.json", opener.call_args.args[0])

    def test_write_upstream(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "owui-upstream.conf"
            flag = Path(tmp) / "owui-ready"
            with mock.patch.object(modules, "UPSTREAM_FILE", path), \
                    mock.patch.object(modules, "OWUI_READY_FLAG", flag):
                modules.write_upstream(18081)
            text = path.read_text(encoding="utf-8")
            self.assertIn("127.0.0.1:18081", text)
            self.assertIn("upstream lemonseed_owui", text)
            # Marking the upstream ready also flips the nginx readiness flag.
            self.assertTrue(flag.exists())

    @mock.patch.object(modules, "lemonade_api_key", return_value="test-key")
    @mock.patch.object(modules, "_write_module_secret")
    @mock.patch.object(
        modules,
        "_module_credentials",
        return_value={
            "DATABASE_URL": "postgresql://open_webui:pw@127.0.0.1:5432/open_webui",
            "DB_NAME": "open_webui",
        },
    )
    def test_enrich_secrets(
        self,
        _creds: mock.Mock,
        write: mock.Mock,
        _key: mock.Mock,
    ) -> None:
        env = modules.enrich_open_webui_secrets()
        self.assertEqual(env["WEBUI_AUTH"], "False")
        self.assertEqual(env["ENABLE_OLLAMA_API"], "False")
        self.assertEqual(env["OLLAMA_BASE_URL"], "")
        self.assertEqual(env["OPENAI_API_BASE_URLS"], modules.LEMONADE_BASE)
        self.assertEqual(env["OPENAI_API_KEY"], "test-key")
        self.assertTrue(env["OPENAI_API_BASE_URL"].endswith("/_lemonseed/lemond/v1"))
        self.assertNotIn(":13305", env["OPENAI_API_BASE_URL"])
        self.assertIn(f"@{modules.POSTGRES_SERVICE}:", env["DATABASE_URL"])
        self.assertNotIn("@127.0.0.1:", env["DATABASE_URL"])
        self.assertNotIn("@host.docker.internal:", env["DATABASE_URL"])
        # M6.3a policy seeds
        self.assertEqual(env["ENABLE_AUTOCOMPLETE_GENERATION"], "False")
        self.assertEqual(env["ENABLE_AUTOMATIONS"], "True")
        self.assertEqual(env["USER_PERMISSIONS_FEATURES_AUTOMATIONS"], "True")
        self.assertEqual(env["ENABLE_NOTES"], "True")
        self.assertEqual(env["ENABLE_CHANNELS"], "False")
        self.assertEqual(env["ENABLE_VERSION_UPDATE_CHECK"], "False")
        self.assertEqual(env["OFFLINE_MODE"], "True")
        self.assertEqual(env["ENABLE_WEB_SEARCH"], "False")
        self.assertEqual(env["ENABLE_IMAGE_GENERATION"], "False")
        self.assertEqual(env["ENABLE_COMMUNITY_SHARING"], "False")
        self.assertEqual(env["USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS"], "False")
        params = json.loads(env["DEFAULT_MODEL_PARAMS"])
        self.assertIn("Lemonseed", params["system"])
        self.assertIn("{{ USER_NAME }}", params["system"])
        self.assertEqual(params["function_calling"], "legacy")
        meta = json.loads(env["DEFAULT_MODEL_METADATA"])
        self.assertFalse(meta["capabilities"]["builtin_tools"])
        self.assertFalse(meta["builtinTools"]["knowledge"])
        self.assertFalse(meta["builtinTools"]["files"])
        write.assert_called_once()

    def test_module_database_url(self) -> None:
        url = modules._module_database_url(
            "postgresql://open_webui:pw@127.0.0.1:5432/open_webui"
        )
        self.assertEqual(
            url, "postgresql://open_webui:pw@lemonseed-postgres:5432/open_webui"
        )
        already = modules._module_database_url(
            "postgresql://open_webui:pw@host.docker.internal:5432/open_webui"
        )
        self.assertEqual(
            already, "postgresql://open_webui:pw@lemonseed-postgres:5432/open_webui"
        )

    @mock.patch.object(modules, "reload_nginx", return_value=True)
    @mock.patch.object(modules, "write_upstream")
    @mock.patch.object(modules, "_run_container", return_value=(True, "lemonseed-owui-18080"))
    @mock.patch.object(modules, "_pull_image", return_value=(True, "pulled"))
    @mock.patch.object(modules, "enrich_open_webui_secrets")
    @mock.patch("data_services.ensure_provisioned", return_value={"state": "ready"})
    def test_ensure_open_webui_ready(
        self,
        _prov: mock.Mock,
        enrich: mock.Mock,
        _pull: mock.Mock,
        _run: mock.Mock,
        write_up: mock.Mock,
        _reload: mock.Mock,
    ) -> None:
        enrich.return_value = {
            "DATABASE_URL": "postgresql://x",
            "OPENAI_API_KEY": "k",
        }
        ready_calls = {"n": 0}

        def fake_ready(_port: int, timeout: float = 3.0) -> bool:
            ready_calls["n"] += 1
            # First probe (active slot) is cold; later probes succeed.
            return ready_calls["n"] >= 2

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "open-webui.env"
            env_path.write_text("DATABASE_URL=postgresql://x\n", encoding="utf-8")
            with mock.patch.object(modules, "http_ready", side_effect=fake_ready):
                with mock.patch.object(modules, "http_ready_stable", return_value=True):
                    with mock.patch.object(modules, "_env_file_path", return_value=env_path):
                        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
                            with mock.patch.object(modules, "_policy_env_ok", return_value=False):
                                with mock.patch.object(modules, "_container_running", return_value=False):
                                    with mock.patch.object(modules, "_stop_container"):
                                        result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["port"], 18080)
        self.assertEqual(result["path"], modules.OWUI_EMBED_PATH)
        write_up.assert_called_with(18080)

    @mock.patch.object(modules, "_policy_env_ok", return_value=True)
    @mock.patch.object(modules, "_container_running", return_value=True)
    @mock.patch.object(modules, "http_ready", return_value=False)
    @mock.patch.object(modules, "enrich_open_webui_secrets", return_value={"DATABASE_URL": "postgresql://x", "OPENAI_API_KEY": "k"})
    @mock.patch("data_services.ensure_provisioned", return_value={"state": "ready"})
    def test_ensure_keeps_running_container_on_probe_blip(
        self,
        _prov: mock.Mock,
        _enrich: mock.Mock,
        _ready: mock.Mock,
        _running: mock.Mock,
        _env: mock.Mock,
    ) -> None:
        modules._state = {"state": "ready", "port": 18080}
        with mock.patch.object(modules, "_pull_image") as pull:
            with mock.patch.object(modules, "_run_container") as run:
                result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "ready")
        pull.assert_not_called()
        run.assert_not_called()

    def test_policy_env_ok_detects_stale_model_params(self) -> None:
        desired = {
            "ENABLE_OLLAMA_API": "False",
            "OPENAI_API_BASE_URL": modules.LEMONADE_BASE,
            "OPENAI_API_BASE_URLS": modules.LEMONADE_BASE,
            "DEFAULT_MODEL_PARAMS": json.dumps(modules.DEFAULT_MODEL_PARAMS),
            "DEFAULT_MODEL_METADATA": json.dumps(modules.DEFAULT_MODEL_METADATA),
            "ENABLE_PERSISTENT_CONFIG": "False",
            "ENABLE_VERSION_UPDATE_CHECK": "False",
            "OFFLINE_MODE": "True",
            "WEBUI_NAME": "Lemonseed",
            "ENABLE_WEB_SEARCH": "False",
            "ENABLE_IMAGE_GENERATION": "False",
            "ENABLE_CHANNELS": "False",
        }
        stale = dict(desired)
        stale["DEFAULT_MODEL_PARAMS"] = json.dumps({"system": "old"})
        with mock.patch.object(modules, "_container_env_map", return_value=stale):
            self.assertFalse(modules._policy_env_ok("c", desired))
        with mock.patch.object(modules, "_container_env_map", return_value=desired):
            self.assertTrue(modules._policy_env_ok("c", desired))

    @mock.patch.object(modules, "reload_nginx", return_value=True)
    @mock.patch.object(modules, "write_upstream")
    @mock.patch.object(modules, "_run_container", return_value=(True, "lemonseed-owui-18081"))
    @mock.patch.object(modules, "_pull_image", return_value=(True, "pulled"))
    @mock.patch.object(modules, "http_ready_stable", return_value=True)
    @mock.patch.object(modules, "http_ready", return_value=True)
    @mock.patch.object(modules, "_policy_env_ok", return_value=False)
    @mock.patch.object(modules, "_container_running", return_value=True)
    @mock.patch.object(modules, "enrich_open_webui_secrets")
    @mock.patch("data_services.ensure_provisioned", return_value={"state": "ready"})
    def test_ensure_recreates_when_policy_env_stale(
        self,
        _prov: mock.Mock,
        enrich: mock.Mock,
        _running: mock.Mock,
        _policy: mock.Mock,
        _ready: mock.Mock,
        _stable: mock.Mock,
        _pull: mock.Mock,
        run: mock.Mock,
        write_up: mock.Mock,
        _reload: mock.Mock,
    ) -> None:
        enrich.return_value = {
            "DATABASE_URL": "postgresql://x",
            "OPENAI_API_KEY": "k",
            "DEFAULT_MODEL_PARAMS": json.dumps(modules.DEFAULT_MODEL_PARAMS),
        }
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "open-webui.env"
            env_path.write_text("DATABASE_URL=postgresql://x\n", encoding="utf-8")
            with mock.patch.object(modules, "_env_file_path", return_value=env_path):
                with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
                    with mock.patch.object(modules, "_stop_container"):
                        result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "ready")
        # Blue/green onto the other slot when active is healthy but stale.
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], 18081)
        write_up.assert_called_with(18081)

    @mock.patch.object(modules, "reload_nginx", return_value=False)
    @mock.patch.object(modules, "write_upstream")
    @mock.patch.object(modules, "_run_container", return_value=(True, "lemonseed-owui-18081"))
    @mock.patch.object(modules, "_pull_image", return_value=(True, "pulled"))
    @mock.patch.object(modules, "http_ready_stable", return_value=True)
    @mock.patch.object(modules, "http_ready", return_value=True)
    @mock.patch.object(modules, "_policy_env_ok", return_value=False)
    @mock.patch.object(modules, "_container_running", return_value=True)
    @mock.patch.object(modules, "enrich_open_webui_secrets")
    @mock.patch("data_services.ensure_provisioned", return_value={"state": "ready"})
    def test_ensure_keeps_old_slot_when_nginx_reload_fails(
        self,
        _prov: mock.Mock,
        enrich: mock.Mock,
        _running: mock.Mock,
        _policy: mock.Mock,
        _ready: mock.Mock,
        _stable: mock.Mock,
        _pull: mock.Mock,
        _run: mock.Mock,
        write_up: mock.Mock,
        _reload: mock.Mock,
    ) -> None:
        enrich.return_value = {
            "DATABASE_URL": "postgresql://x",
            "OPENAI_API_KEY": "k",
        }
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "open-webui.env"
            env_path.write_text("DATABASE_URL=postgresql://x\n", encoding="utf-8")
            with mock.patch.object(modules, "_env_file_path", return_value=env_path):
                with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
                    with mock.patch.object(modules, "_stop_container") as stop:
                        result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "error")
        self.assertIn("nginx reload", result["error"])
        write_up.assert_called_with(18081)
        stop.assert_not_called()

    @mock.patch.object(modules, "http_ready", return_value=True)
    @mock.patch.object(modules, "_container_running", return_value=True)
    def test_modules_snapshot_hides_http_ready_while_updating(
        self,
        _running: mock.Mock,
        _ready: mock.Mock,
    ) -> None:
        modules._state = {
            "state": "starting",
            "reason": "Updating chat interface settings",
            "port": 18080,
        }
        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
            snap = modules.modules_snapshot()["open_webui"]
        self.assertEqual(snap["state"], "starting")
        self.assertFalse(snap["http_ready"])

    @mock.patch("modules.subprocess.run")
    def test_reload_nginx_prefers_systemctl(self, run: mock.Mock) -> None:
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        self.assertTrue(modules.reload_nginx())
        self.assertEqual(run.call_args.args[0][:2], ["systemctl", "reload"])

    @mock.patch.object(modules, "ensure_open_webui", return_value={"state": "ready"})
    @mock.patch.object(modules, "http_ready", return_value=True)
    @mock.patch.object(modules, "_container_running", return_value=True)
    def test_supervise_healthy_resets_fail_count(
        self,
        _running: mock.Mock,
        _ready: mock.Mock,
        ensure: mock.Mock,
    ) -> None:
        modules._health_fail_count = 3
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(modules, "OWUI_READY_FLAG", Path(tmp) / "owui-ready"), \
                    mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
                result = modules.supervise_modules_once()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(modules._health_fail_count, 0)
        ensure.assert_called_once_with(force_update=False)

    @mock.patch.object(modules, "ensure_open_webui", return_value={"state": "ready"})
    @mock.patch.object(modules, "_container_running", return_value=False)
    def test_supervise_starts_when_not_running(
        self,
        _running: mock.Mock,
        ensure: mock.Mock,
    ) -> None:
        modules._health_fail_count = 2
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(modules, "OWUI_READY_FLAG", Path(tmp) / "owui-ready"), \
                    mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
                modules.supervise_modules_once()
        self.assertEqual(modules._health_fail_count, 0)
        ensure.assert_called_once_with(force_update=False)

    @mock.patch.object(modules, "ensure_open_webui")
    @mock.patch.object(modules, "http_ready", return_value=False)
    @mock.patch.object(modules, "_container_running", return_value=True)
    def test_supervise_recreates_after_contiguous_failures(
        self,
        _running: mock.Mock,
        _ready: mock.Mock,
        ensure: mock.Mock,
    ) -> None:
        ensure.return_value = {"state": "ready"}
        modules._health_fail_count = 0
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(modules, "OWUI_READY_FLAG", Path(tmp) / "owui-ready"), \
                    mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
                for i in range(1, modules.HEALTH_FAIL_LIMIT):
                    result = modules.supervise_modules_once()
                    self.assertEqual(result["state"], "starting")
                    self.assertEqual(modules._health_fail_count, i)
                    ensure.assert_not_called()
                result = modules.supervise_modules_once()
        ensure.assert_called_once_with(force_update=True)
        self.assertEqual(modules._health_fail_count, 0)

    def test_ensure_module_worker_starts_supervisor(self) -> None:
        modules._worker = None
        with mock.patch("modules.threading.Thread") as thread_cls:
            fake = mock.Mock()
            fake.is_alive.return_value = True
            thread_cls.return_value = fake
            modules.ensure_module_worker()
            thread_cls.assert_called_once()
            fake.start.assert_called_once()
            self.assertIs(modules._worker, fake)

    def test_worker_skips_when_already_running(self) -> None:
        alive = mock.Mock()
        alive.is_alive.return_value = True
        modules._worker = alive
        modules.ensure_module_worker()
        self.assertIs(modules._worker, alive)

    @mock.patch("data_services.ensure_provisioned", return_value={"state": "waiting"})
    def test_ensure_waits_for_provision(self, _prov: mock.Mock) -> None:
        result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "waiting")

    def test_pull_image_waits_for_local_preload(self) -> None:
        with mock.patch.dict(
            os.environ, {"LEMONSEED_OWUI_WAIT_LOCAL_SECONDS": "7200"}, clear=False
        ):
            with mock.patch.object(modules, "_docker") as docker:
                docker.return_value = mock.Mock(returncode=1)
                ok, detail = modules._pull_image()
        self.assertFalse(ok)
        self.assertIn("waiting", detail)
        docker.assert_called_once()
        self.assertEqual(docker.call_args.args[0][:2], ["image", "inspect"])


if __name__ == "__main__":
    raise SystemExit(unittest.main())
