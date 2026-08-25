#!/usr/bin/env python3
"""Unit tests for Open WebUI module lifecycle (mocked docker/nginx)."""

from __future__ import annotations

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

    def test_other_port(self) -> None:
        self.assertEqual(modules._other_port(18080), 18081)
        self.assertEqual(modules._other_port(18081), 18080)

    def test_http_ready_requires_version_json(self) -> None:
        class _Resp:
            status = 200

            def read(self, _n: int = -1) -> bytes:
                return b'{{"version":"0.6.32"}}'

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
            with mock.patch.object(modules, "UPSTREAM_FILE", path):
                modules.write_upstream(18081)
            text = path.read_text(encoding="utf-8")
            self.assertIn("127.0.0.1:18081", text)
            self.assertIn("upstream lemonseed_owui", text)

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
                            with mock.patch.object(modules, "_container_env_contains", return_value=False):
                                with mock.patch.object(modules, "_container_running", return_value=False):
                                    with mock.patch.object(modules, "_stop_container"):
                                        result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["port"], 18080)
        self.assertEqual(result["path"], modules.OWUI_EMBED_PATH)
        write_up.assert_called_with(18080)

    @mock.patch.object(modules, "_container_env_contains", return_value=True)
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

    @mock.patch.object(modules, "open_webui_is_ready", return_value=True)
    def test_worker_skips_when_already_ready(self, _ready: mock.Mock) -> None:
        modules._worker = None
        modules.ensure_module_worker()
        self.assertIsNone(modules._worker)

    @mock.patch("data_services.ensure_provisioned", return_value={"state": "waiting"})
    def test_ensure_waits_for_provision(self, _prov: mock.Mock) -> None:
        result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "waiting")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
