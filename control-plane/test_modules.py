#!/usr/bin/env python3
"""Unit tests for Open WebUI module lifecycle (mocked docker/nginx)."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import auth  # noqa: E402
import image_pins  # noqa: E402
import modules  # noqa: E402


class StaleContainerReapTests(unittest.TestCase):
    """M6.6 S5.6: stopped containers from an earlier boot must not pile up."""

    @staticmethod
    def _fake_docker(table: dict[str, str], rm_ok: bool = True) -> mock.Mock:
        def fake(args: list[str], **_kw: object) -> mock.Mock:
            if args[:2] == ["ps", "-a"]:
                out = "\n".join(f"{name}\t{state}" for name, state in table.items())
                return mock.Mock(returncode=0, stdout=out + "\n", stderr="")
            if args[:2] == ["rm", "-f"]:
                return mock.Mock(returncode=0 if rm_ok else 1, stdout="", stderr="")
            raise AssertionError(f"unexpected docker call: {args}")

        return mock.Mock(side_effect=fake)

    def test_removes_stopped_and_keeps_running(self) -> None:
        table = {
            "lemonseed-owui-18080": "exited",
            "lemonseed-owui-18081": "running",
        }
        fake = self._fake_docker(table)
        with mock.patch.object(modules, "_docker", fake):
            removed = modules.reap_stale_containers()
        self.assertEqual(removed, ["lemonseed-owui-18080"])

    def test_ignores_foreign_names(self) -> None:
        """The docker name filter is a substring match; a neighbour is not ours."""
        table = {"open-webui": "exited", "my-lemonseed-owui-old": "exited"}
        fake = self._fake_docker(table)
        with mock.patch.object(modules, "_docker", fake):
            removed = modules.reap_stale_containers()
        self.assertEqual(removed, [])

    def test_docker_failure_is_not_fatal(self) -> None:
        with mock.patch.object(
            modules, "_docker", return_value=mock.Mock(returncode=1, stdout="", stderr="boom")
        ):
            self.assertEqual(modules.reap_stale_containers(), [])

    def test_failed_rm_is_not_reported_as_removed(self) -> None:
        fake = self._fake_docker({"lemonseed-owui-18080": "created"}, rm_ok=False)
        with mock.patch.object(modules, "_docker", fake):
            self.assertEqual(modules.reap_stale_containers(), [])

    def test_worker_reaps_before_the_first_tick(self) -> None:
        """The reap must land once, ahead of the first tick, and never race it."""

        class _StopLoop(RuntimeError):
            pass

        order: list[str] = []

        def reap() -> list[str]:
            order.append("reap")
            return []

        def tick() -> None:
            order.append("tick")

        with (
            mock.patch.object(modules, "reap_stale_containers", reap),
            mock.patch.object(modules, "supervise_modules_once", tick),
            mock.patch.object(modules.time, "sleep", side_effect=_StopLoop("stop")),
        ):
            with self.assertRaises(_StopLoop):
                modules._supervise_forever()
        self.assertEqual(order, ["reap", "tick"])

    def test_reap_failure_does_not_stop_the_worker(self) -> None:
        """A broken `docker ps` must not cost us the supervisor."""

        class _StopLoop(RuntimeError):
            pass

        ticks: list[str] = []
        with (
            mock.patch.object(
                modules, "reap_stale_containers", side_effect=OSError("docker is angry")
            ),
            mock.patch.object(modules, "supervise_modules_once", side_effect=lambda: ticks.append("t")),
            mock.patch.object(modules.time, "sleep", side_effect=_StopLoop("stop")),
        ):
            with self.assertRaises(_StopLoop):
                modules._supervise_forever()
        self.assertEqual(ticks, ["t"])


class ProgressCopyTests(unittest.TestCase):
    """M6.6 S5.7: the interstitial must not blame the chat app for the data plane."""

    def test_pull_gets_its_own_words(self) -> None:
        workflow, task = modules._owui_progress_copy("pulling", "v0.11.0", {})
        self.assertEqual(workflow, "Configuring chat")
        self.assertIn("Pulling", task)

    def test_data_plane_wait_says_storage(self) -> None:
        workflow, task = modules._owui_progress_copy(
            "waiting", "v0.11.0", {"reason": "data plane not provisioned", "provisioning": "starting"}
        )
        self.assertEqual(workflow, "Preparing storage")
        self.assertNotIn("chat", task.lower())

    def test_missing_database_url_says_storage(self) -> None:
        workflow, _task = modules._owui_progress_copy(
            "waiting", "v0.11.0", {"reason": "open-webui DATABASE_URL missing"}
        )
        self.assertEqual(workflow, "Preparing storage")

    def test_unrelated_wait_stays_chat(self) -> None:
        workflow, task = modules._owui_progress_copy(
            "waiting", "v0.11.0", {"reason": "Rebuilding the index"}
        )
        self.assertEqual(workflow, "Configuring chat")
        self.assertEqual(task, "Rebuilding the index")


class ModulesTests(unittest.TestCase):
    def setUp(self) -> None:
        modules._state = None
        modules._health_fail_count = 0
        modules._worker = None
        modules._pull_fail_at = 0.0
        modules._net_check_at = 0.0
        modules._net_ok = False
        modules._service_identity_ready = False
        modules._service_account_id = None
        if modules._lock.locked():
            modules._lock.release()

    def tearDown(self) -> None:
        modules._worker = None
        modules._health_fail_count = 0
        modules._pull_fail_at = 0.0
        modules._net_check_at = 0.0
        modules._net_ok = False
        modules._service_identity_ready = False
        modules._service_account_id = None
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

    def test_owui_run_ref_is_the_pinned_digest(self) -> None:
        """Run/pull uses the digest; the status `image` field stays the tag.

        T5-H: a sha256 must never reach user-facing copy, and modules.py derives
        the version label by splitting `image` on ':'. Both invariants are
        enforced here so neither can silently regress.
        """
        digest = image_pins.digest_ref(modules.OWUI_IMAGE_KEY)
        with mock.patch.object(modules, "_image_present", return_value=True):
            self.assertEqual(modules._owui_run_ref(), digest)
        with mock.patch.object(modules, "_image_present", return_value=False):
            self.assertEqual(modules._owui_run_ref(), modules.OWUI_IMAGE)
        self.assertNotIn("@", modules.OWUI_IMAGE)
        self.assertEqual(
            modules.OWUI_IMAGE, image_pins.tag_ref(modules.OWUI_IMAGE_KEY)
        )

    def test_network_reachable_caches_outcome(self) -> None:
        with (
            mock.patch.object(modules.socket, "gethostbyname", return_value="203.0.113.9"),
            mock.patch.object(modules.socket, "create_connection") as m_conn,
        ):
            self.assertTrue(modules._network_reachable())
            self.assertTrue(modules._network_reachable())  # cached, no 2nd probe
            m_conn.assert_called_once()

    def test_network_reachable_unreachable_on_dns_failure(self) -> None:
        with mock.patch.object(
            modules.socket, "gethostbyname", side_effect=socket.gaierror("no such host")
        ):
            self.assertFalse(modules._network_reachable())

    def test_pull_skips_docker_when_registry_unreachable(self) -> None:
        probe = mock.Mock(returncode=1, stdout="", stderr="Error: no such host")
        with (
            mock.patch.object(modules, "_image_present", return_value=False),
            mock.patch.object(modules, "_network_reachable", return_value=False),
            mock.patch.object(modules, "_docker", return_value=probe) as m_docker,
        ):
            ok, detail = modules._pull_image()
        self.assertFalse(ok)
        self.assertIn("container registry", detail)
        self.assertIn("internet connection", detail)
        # Exactly one diagnostic probe (journal), not a pull attempt per tick.
        m_docker.assert_called_once()

    def test_pull_probes_docker_once_per_cooldown(self) -> None:
        with (
            mock.patch.object(modules, "_image_present", return_value=False),
            mock.patch.object(modules, "_network_reachable", return_value=False),
            mock.patch.object(modules, "_docker") as m_docker,
        ):
            modules._pull_image()
            modules._pull_image()  # within cooldown: no 2nd probe
            m_docker.assert_called_once()
            modules._pull_fail_at = 0.0  # force cooldown expiry
            modules._pull_image()
            self.assertEqual(m_docker.call_count, 2)

    def test_pull_pulls_when_network_reachable(self) -> None:
        result = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(modules, "_image_present", return_value=False),
            mock.patch.object(modules, "_network_reachable", return_value=True),
            mock.patch.object(modules, "_docker", return_value=result) as m_docker,
        ):
            ok, detail = modules._pull_image()
        self.assertTrue(ok)
        self.assertEqual(detail, "pulled")
        m_docker.assert_called_once()

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
    @mock.patch.object(modules, "_write_container_env")
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
        write_env: mock.Mock,
        _key: mock.Mock,
    ) -> None:
        env = modules.enrich_open_webui_secrets()
        # M6.2: OWUI trusted-header auth (edge gate is the login; identity keyed on email).
        self.assertEqual(env["WEBUI_AUTH"], "True")
        self.assertEqual(env["WEBUI_AUTH_TRUSTED_EMAIL_HEADER"], "X-Forwarded-Email")
        self.assertEqual(env["WEBUI_AUTH_TRUSTED_NAME_HEADER"], "X-Forwarded-Name")
        self.assertEqual(env["WEBUI_AUTH_TRUSTED_GROUPS_HEADER"], "X-Forwarded-Groups")
        self.assertEqual(env["WEBUI_AUTH_TRUSTED_ROLE_HEADER"], "X-Forwarded-Role")
        # Invented provider vars must not leak into the container env.
        self.assertNotIn("WEBUI_AUTH_PROVIDER", env)
        self.assertNotIn("AUTH_CUSTOM_HEADER", env)
        self.assertIn(f"@{modules.POSTGRES_SERVICE}:", env["DATABASE_URL"])
        self.assertNotIn("@127.0.0.1:", env["DATABASE_URL"])
        self.assertNotIn("@host.docker.internal:", env["DATABASE_URL"])
        # M6.3b Phase 3 env shrink: the payload is bootstrap + secret storage
        # only; every policy knob now reconciles through owui-policy.json.
        self.assertEqual(env["WEBUI_NAME"], "Lemonseed")
        self.assertEqual(env["OFFLINE_MODE"], "True")
        self.assertEqual(env["ENABLE_VERSION_UPDATE_CHECK"], "False")
        self.assertIn("WEBUI_SECRET_KEY", env)
        self.assertEqual(env["OPENAI_API_KEY"], "test-key")  # storage, not env
        self.assertNotIn("ENABLE_PERSISTENT_CONFIG", env)
        for retired in (
            "ENABLE_OLLAMA_API", "OLLAMA_BASE_URL", "OLLAMA_BASE_URLS",
            "ENABLE_OPENAI_API", "OPENAI_API_BASE_URL", "OPENAI_API_BASE_URLS",
            "OPENAI_API_KEYS", "ENABLE_AUTOCOMPLETE_GENERATION",
            "ENABLE_AUTOMATIONS", "ENABLE_NOTES", "ENABLE_MEMORIES",
            "ENABLE_CHANNELS", "ENABLE_WEB_SEARCH", "ENABLE_IMAGE_GENERATION",
            "ENABLE_COMMUNITY_SHARING", "USER_PERMISSIONS_WORKSPACE_MODELS_ACCESS",
            "USER_PERMISSIONS_FEATURES_AUTOMATIONS", "DEFAULT_MODEL_PARAMS",
            "DEFAULT_MODEL_METADATA",
        ):
            self.assertNotIn(retired, env, retired)
        # The docker --env-file gets the bootstrap subset only.
        write_env.assert_called_once_with(env)
        subset = modules._container_env_subset(env)
        self.assertIn("DATABASE_URL", subset)
        self.assertNotIn("OPENAI_API_KEY", subset)
        self.assertNotIn("DB_NAME", subset)
        self.assertEqual(set(subset), set(modules._CONTAINER_ENV_KEYS))
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

    @mock.patch.object(modules, "ensure_owui_service_identity", return_value=True)
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
        _identity: mock.Mock,
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
                            with mock.patch.object(modules, "_bootstrap_env_ok", return_value=False):
                                with mock.patch.object(modules, "_container_running", return_value=False):
                                    with mock.patch.object(modules, "_stop_container"):
                                        result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["port"], 18080)
        self.assertEqual(result["path"], modules.OWUI_EMBED_PATH)
        write_up.assert_called_with(18080)

    @mock.patch.object(modules, "_bootstrap_env_ok", return_value=True)
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

    def _floor(self, **bootstrap: str) -> dict[str, str]:
        """A desired map shaped like _container_env_subset output."""
        return {modules._ENV_GEN_KEY: modules._ENV_GEN_VALUE, **bootstrap}

    def test_bootstrap_env_ok_detects_stale_bootstrap_value(self) -> None:
        desired = self._floor(
            WEBUI_NAME="Lemonseed",
            ENABLE_VERSION_UPDATE_CHECK="False",
            OFFLINE_MODE="True",
        )
        stale = dict(desired, WEBUI_NAME="OldName")
        with mock.patch.object(modules, "_container_env_map", return_value=stale):
            self.assertFalse(modules._bootstrap_env_ok("c", desired))
        with mock.patch.object(modules, "_container_env_map", return_value=desired):
            self.assertTrue(modules._bootstrap_env_ok("c", desired))

    def test_bootstrap_env_gen_marker_force_recreate(self) -> None:
        # QEMU regression: the OWUI v0.11.0 image bakes OLLAMA_BASE_URL,
        # OPENAI_API_KEY, OPENAI_API_BASE_URL and WEBUI_SECRET_KEY into its
        # Config.Env, so "retired key absent" checks can never pass and sent
        # the CP into an infinite blue/green recreate storm. The CP-owned
        # generation marker detects pre-shrink containers instead; image-baked
        # keys must not cause a recreate.
        desired = self._floor(WEBUI_NAME="Lemonseed")
        pre_shrink = {
            "WEBUI_NAME": "Lemonseed",
            "ENABLE_PERSISTENT_CONFIG": "False",  # pre-Phase-2 (M6.3a box)
            "ENABLE_WEB_SEARCH": "False",         # pre-Phase-3 policy env
            "OPENAI_API_KEY": "sk-leak",          # storage-only key
        }
        with mock.patch.object(modules, "_container_env_map", return_value=pre_shrink):
            self.assertFalse(modules._bootstrap_env_ok("c", desired))
        # Image-baked values on an otherwise-current container: no drift.
        with_baked = dict(desired, OLLAMA_BASE_URL="/ollama",
                          OPENAI_API_BASE_URL="", OPENAI_API_KEY="",
                          WEBUI_SECRET_KEY="", PATH="/usr/local/bin")
        with mock.patch.object(modules, "_container_env_map", return_value=with_baked):
            self.assertTrue(modules._bootstrap_env_ok("c", desired))
        # A container matching every bootstrap value but predating the marker
        # is still recreated (that is how M6.3a boxes get cleared).
        no_marker = dict(desired)
        no_marker.pop(modules._ENV_GEN_KEY)
        with mock.patch.object(modules, "_container_env_map", return_value=no_marker):
            self.assertFalse(modules._bootstrap_env_ok("c", desired))

    def test_bootstrap_env_ok_detects_missing_trusted_header_auth(self) -> None:
        # Desired env enables OWUI trusted-header auth; a healthy container that
        # predates it (no auth keys) must be flagged stale so it gets recreated.
        desired = self._floor(
            WEBUI_AUTH="True",
            WEBUI_AUTH_TRUSTED_EMAIL_HEADER="X-Forwarded-Email",
            WEBUI_AUTH_TRUSTED_NAME_HEADER="X-Forwarded-Name",
            WEBUI_AUTH_TRUSTED_GROUPS_HEADER="X-Forwarded-Groups",
            WEBUI_AUTH_TRUSTED_ROLE_HEADER="X-Forwarded-Role",
        )
        # Container running with none of the auth env (pre-M6.2 trusted-header).
        with mock.patch.object(modules, "_container_env_map", return_value={}):
            self.assertFalse(modules._bootstrap_env_ok("c", desired))
        with mock.patch.object(modules, "_container_env_map", return_value=dict(desired)):
            self.assertTrue(modules._bootstrap_env_ok("c", desired))

    @mock.patch.object(modules, "ensure_owui_service_identity", return_value=True)
    @mock.patch.object(modules, "reload_nginx", return_value=True)
    @mock.patch.object(modules, "write_upstream")
    @mock.patch.object(modules, "_run_container", return_value=(True, "lemonseed-owui-18081"))
    @mock.patch.object(modules, "_pull_image", return_value=(True, "pulled"))
    @mock.patch.object(modules, "http_ready_stable", return_value=True)
    @mock.patch.object(modules, "http_ready", return_value=True)
    @mock.patch.object(modules, "_bootstrap_env_ok", return_value=False)
    @mock.patch.object(modules, "_container_running", return_value=True)
    @mock.patch.object(modules, "enrich_open_webui_secrets")
    @mock.patch("data_services.ensure_provisioned", return_value={"state": "ready"})
    def test_ensure_recreates_when_bootstrap_env_stale(
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
        _identity: mock.Mock,
    ) -> None:
        enrich.return_value = {
            "DATABASE_URL": "postgresql://x",
            "OPENAI_API_KEY": "k",
            "WEBUI_NAME": "Lemonseed",
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

    @mock.patch.object(modules, "ensure_owui_service_identity", return_value=True)
    @mock.patch.object(modules, "reload_nginx", return_value=False)
    @mock.patch.object(modules, "write_upstream")
    @mock.patch.object(modules, "_run_container", return_value=(True, "lemonseed-owui-18081"))
    @mock.patch.object(modules, "_pull_image", return_value=(True, "pulled"))
    @mock.patch.object(modules, "http_ready_stable", return_value=True)
    @mock.patch.object(modules, "http_ready", return_value=True)
    @mock.patch.object(modules, "_bootstrap_env_ok", return_value=False)
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
        _identity: mock.Mock,
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

    @mock.patch.object(modules, "sync_owui_guards", return_value={})
    @mock.patch.object(modules, "ensure_open_webui", return_value={"state": "ready"})
    @mock.patch.object(modules, "http_ready", return_value=True)
    @mock.patch.object(modules, "_container_running", return_value=True)
    def test_supervise_healthy_resets_fail_count(
        self,
        _running: mock.Mock,
        _ready: mock.Mock,
        ensure: mock.Mock,
        _guards: mock.Mock,
    ) -> None:
        modules._health_fail_count = 3
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(modules, "OWUI_READY_FLAG", Path(tmp) / "owui-ready"), \
                    mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
                result = modules.supervise_modules_once()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(modules._health_fail_count, 0)
        ensure.assert_called_once_with(force_update=False)

    @mock.patch.object(modules, "sync_owui_guards", return_value={})
    @mock.patch.object(modules, "ensure_open_webui", return_value={"state": "ready"})
    @mock.patch.object(modules, "_container_running", return_value=False)
    def test_supervise_starts_when_not_running(
        self,
        _running: mock.Mock,
        ensure: mock.Mock,
        _guards: mock.Mock,
    ) -> None:
        modules._health_fail_count = 2
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(modules, "OWUI_READY_FLAG", Path(tmp) / "owui-ready"), \
                    mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
                modules.supervise_modules_once()
        self.assertEqual(modules._health_fail_count, 0)
        ensure.assert_called_once_with(force_update=False)

    @mock.patch.object(modules, "sync_owui_guards", return_value={})
    @mock.patch.object(modules, "ensure_open_webui")
    @mock.patch.object(modules, "http_ready", return_value=False)
    @mock.patch.object(modules, "_container_running", return_value=True)
    def test_supervise_recreates_after_contiguous_failures(
        self,
        _running: mock.Mock,
        _ready: mock.Mock,
        ensure: mock.Mock,
        _guards: mock.Mock,
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
        # digest + tag presence probes only — no pull attempt while waiting.
        self.assertEqual(docker.call_count, 2)
        for call in docker.call_args_list:
            self.assertEqual(call.args[0][:2], ["image", "inspect"])

    def test_pull_reports_tag_only_image_as_present(self) -> None:
        """A tag-only local image (QEMU tarball) must not trigger a pull."""
        seen: list[str] = []

        def present(ref: str) -> bool:
            seen.append(ref)
            return not ref.startswith("sha256:") and "@" not in ref

        with (
            mock.patch.object(modules, "_image_present", side_effect=present),
            mock.patch.object(modules, "_docker") as docker,
        ):
            ok, detail = modules._pull_image()
        self.assertTrue(ok)
        self.assertEqual(detail, "present:tag")
        docker.assert_not_called()
        self.assertIn(image_pins.digest_ref(modules.OWUI_IMAGE_KEY), seen)

    def test_pull_falls_back_to_tag_when_digest_unavailable(self) -> None:
        """A GC'd pin must degrade loudly, not refuse to start the chat app."""
        calls: list[list[str]] = []

        def fake_docker(args: list[str], **_kw: object) -> mock.Mock:
            calls.append(args)
            # digest pull fails, tag pull succeeds
            ok = "@" not in args[-1]
            return mock.Mock(returncode=0 if ok else 1, stdout="", stderr="no such manifest")

        with (
            mock.patch.object(modules, "_image_present", return_value=False),
            mock.patch.object(modules, "_network_reachable", return_value=True),
            mock.patch.object(modules, "_docker", side_effect=fake_docker),
        ):
            ok, detail = modules._pull_image()
        self.assertTrue(ok)
        self.assertEqual(detail, "pulled:tag_fallback")
        self.assertEqual(len(calls), 2)
        self.assertIn("@sha256:", calls[0][-1])
        self.assertNotIn("@", calls[1][-1])

    def test_pull_timeout_covers_owui_image_size(self) -> None:
        """Open WebUI is ~1.7 GB compressed; a 600s cap fails slow links."""
        self.assertGreaterEqual(modules.OWUI_PULL_TIMEOUT_SEC, 1800)
        result = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(modules, "_image_present", return_value=False),
            mock.patch.object(modules, "_network_reachable", return_value=True),
            mock.patch.object(modules, "_docker", return_value=result) as m_docker,
        ):
            modules._pull_image()
        self.assertEqual(m_docker.call_args.kwargs["timeout"], modules.OWUI_PULL_TIMEOUT_SEC)

    # -- M6.3b: CP service identity in Open WebUI --------------------------

    @staticmethod
    def _signin_ok(role: str = "admin", user_id: str = "cp-1") -> dict:
        return {
            "ok": True,
            "status": 200,
            "data": {
                "id": user_id,
                "email": modules.OWUI_SERVICE_EMAIL,
                "name": modules.OWUI_SERVICE_NAME,
                "role": role,
            },
            "error": None,
        }

    def test_service_identity_provisions_once(self) -> None:
        client = mock.Mock()
        client.sign_in_trusted.return_value = self._signin_ok()
        with mock.patch.object(
            modules.owui_client, "OwuiClient", return_value=client
        ) as factory, mock.patch.object(modules, "sync_owui_guards") as guards:
            self.assertTrue(modules.ensure_owui_service_identity(18080))
            self.assertTrue(modules.ensure_owui_service_identity(18080))  # cached no-op
        factory.assert_called_once()
        # Direct-to-slot URL + the appliance's trusted-header names.
        self.assertEqual(factory.call_args.args[0], "http://127.0.0.1:18080")
        self.assertEqual(factory.call_args.kwargs["email_header"], modules.OWUI_TRUSTED_EMAIL_HEADER)
        client.sign_in_trusted.assert_called_once_with(
            modules.OWUI_SERVICE_EMAIL,
            name=modules.OWUI_SERVICE_NAME,
            role=modules.OWUI_SERVICE_ROLE,
        )
        self.assertEqual(modules._service_account_id, "cp-1")
        # Phase 4B: the %CP_OWUI_ID% guards could not exist before this moment,
        # so rendering waits on the id — and must not wait for the next tick.
        guards.assert_called_once()

    def test_identity_failure_does_not_render_guards(self) -> None:
        client = mock.Mock()
        client.sign_in_trusted.return_value = self._signin_ok(role="user")
        with mock.patch.object(modules.owui_client, "OwuiClient", return_value=client), \
             mock.patch.object(modules, "sync_owui_guards") as guards:
            self.assertFalse(modules.ensure_owui_service_identity(18080))
        guards.assert_not_called()

    def test_service_identity_failure_retries_next_tick(self) -> None:
        client = mock.Mock()
        client.sign_in_trusted.return_value = {
            "ok": False,
            "status": None,
            "data": None,
            "error": "URLError: connection refused",
        }
        with mock.patch.object(modules.owui_client, "OwuiClient", return_value=client):
            self.assertFalse(modules.ensure_owui_service_identity(18080))
            self.assertFalse(modules.ensure_owui_service_identity(18080))
        self.assertEqual(client.sign_in_trusted.call_count, 2)
        self.assertFalse(modules._service_identity_ready)
        self.assertIsNone(modules._service_account_id)

    def test_service_identity_requires_admin_role(self) -> None:
        # A sign-in that succeeds without admin role must not count as
        # provisioned (OWUI syncs the trusted role after authenticating, so
        # the next tick's re-sign-in sees the healed value and flips ready).
        client = mock.Mock()
        client.sign_in_trusted.return_value = self._signin_ok(role="user")
        with mock.patch.object(modules.owui_client, "OwuiClient", return_value=client):
            self.assertFalse(modules.ensure_owui_service_identity(18080))
        self.assertFalse(modules._service_identity_ready)

    def test_service_identity_client_crash_is_not_fatal(self) -> None:
        with mock.patch.object(
            modules.owui_client, "OwuiClient", side_effect=RuntimeError("boom")
        ):
            self.assertFalse(modules.ensure_owui_service_identity(18080))
        self.assertFalse(modules._service_identity_ready)

    @mock.patch.object(modules, "reload_nginx", return_value=True)
    @mock.patch.object(modules, "_run_container", return_value=(True, "lemonseed-owui-18080"))
    @mock.patch.object(modules, "_pull_image", return_value=(True, "pulled"))
    @mock.patch.object(modules, "enrich_open_webui_secrets")
    @mock.patch("data_services.ensure_provisioned", return_value={"state": "ready"})
    def test_fresh_boot_provisions_identity_before_cutover(
        self,
        _prov: mock.Mock,
        enrich: mock.Mock,
        _pull: mock.Mock,
        _run: mock.Mock,
        _reload: mock.Mock,
    ) -> None:
        """The CP must take the primary-admin seat before nginx serves OWUI."""
        enrich.return_value = {"DATABASE_URL": "postgresql://x", "OPENAI_API_KEY": "k"}
        calls: list[str] = []
        ready_calls = {"n": 0}

        def fake_ready(_port: int, timeout: float = 3.0) -> bool:
            ready_calls["n"] += 1
            return ready_calls["n"] >= 2

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "open-webui.env"
            env_path.write_text("DATABASE_URL=postgresql://x\n", encoding="utf-8")
            with mock.patch.object(modules, "http_ready", side_effect=fake_ready):
                with mock.patch.object(modules, "http_ready_stable", return_value=True):
                    with mock.patch.object(modules, "_env_file_path", return_value=env_path):
                        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
                            with mock.patch.object(modules, "_bootstrap_env_ok", return_value=False):
                                with mock.patch.object(modules, "_container_running", return_value=False):
                                    with mock.patch.object(modules, "_stop_container"):
                                        with mock.patch.object(
                                            modules,
                                            "ensure_owui_service_identity",
                                            side_effect=lambda _port: calls.append("identity") or True,
                                        ):
                                            with mock.patch.object(
                                                modules,
                                                "write_upstream",
                                                side_effect=lambda _port: calls.append("upstream"),
                                            ) as write_up:
                                                result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(calls, ["identity", "upstream"])
        write_up.assert_called_with(18080)

    def test_snapshot_reports_service_identity(self) -> None:
        modules._state = {"state": "ready", "port": 18080}
        modules._service_identity_ready = True
        modules._service_account_id = "cp-1"
        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
            with mock.patch.object(modules, "_container_running", return_value=True):
                with mock.patch.object(modules, "http_ready", return_value=True):
                    snap = modules.modules_snapshot()["open_webui"]
        self.assertTrue(snap["service_ready"])
        self.assertEqual(snap["service_account_id"], "cp-1")

    def test_snapshot_service_not_ready_by_default(self) -> None:
        modules._state = {"state": "ready", "port": 18080}
        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
            with mock.patch.object(modules, "_container_running", return_value=True):
                with mock.patch.object(modules, "http_ready", return_value=True):
                    snap = modules.modules_snapshot()["open_webui"]
        self.assertFalse(snap["service_ready"])
        self.assertNotIn("service_account_id", snap)


class PolicyWiringTests(unittest.TestCase):
    """M6.3b Phase 3: supervisor glue around owui_policy.reconcile."""

    def setUp(self) -> None:
        self._saved = (
            modules._policy_slot_done, modules._policy_audit_at,
            modules._service_identity_ready, modules._service_account_id,
        )
        modules._policy_slot_done = None
        modules._policy_audit_at = 0.0
        modules._service_identity_ready = True
        modules._service_account_id = "svc-1"
        self.addCleanup(setattr, modules, "_policy_slot_done", self._saved[0])
        self.addCleanup(setattr, modules, "_policy_audit_at", self._saved[1])
        self.addCleanup(setattr, modules, "_service_identity_ready", self._saved[2])
        self.addCleanup(setattr, modules, "_service_account_id", self._saved[3])

    def test_no_write_before_identity_ready(self) -> None:
        modules._service_identity_ready = False
        with mock.patch.object(modules.owui_policy, "reconcile") as rec:
            modules._policy_maybe_write(18080, "lemonseed-owui-18080")
        rec.assert_not_called()

    def test_write_marks_slot_on_ok_and_stops_firing(self) -> None:
        with mock.patch.object(modules.owui_policy, "reconcile",
                               return_value={"ok": True}) as rec:
            modules._policy_maybe_write(18080, "lemonseed-owui-18080")
            self.assertEqual(modules._policy_slot_done, (18080, "lemonseed-owui-18080"))
            modules._policy_maybe_write(18080, "lemonseed-owui-18080")
        self.assertEqual(rec.call_count, 1)
        self.assertEqual(rec.call_args[0][1], frozenset({"at_boot", "on_upgrade"}))

    def test_failed_write_retries_next_tick(self) -> None:
        results = [{"ok": False, "last_error": "boom"}, {"ok": True}]
        with mock.patch.object(modules.owui_policy, "reconcile",
                               side_effect=results) as rec:
            modules._policy_maybe_write(18080, "c")
            self.assertIsNone(modules._policy_slot_done)
            modules._policy_maybe_write(18080, "c")
            self.assertEqual(modules._policy_slot_done, (18080, "c"))
        self.assertEqual(rec.call_count, 2)

    def test_locked_run_does_not_consume_the_slot(self) -> None:
        with mock.patch.object(modules.owui_policy, "reconcile",
                               return_value={"ok": False, "skipped": "reconcile already running"}):
            modules._policy_maybe_write(18080, "c")
        self.assertIsNone(modules._policy_slot_done)

    def test_new_slot_key_fires_again(self) -> None:
        with mock.patch.object(modules.owui_policy, "reconcile",
                               return_value={"ok": True}) as rec:
            modules._policy_maybe_write(18080, "c-18080")
            modules._policy_maybe_write(18081, "c-18081")
        self.assertEqual(rec.call_count, 2)

    def test_audit_only_after_interval(self) -> None:
        modules._policy_slot_done = (18080, "c")
        modules._policy_audit_at = time.monotonic()  # just reconciled
        with mock.patch.object(modules.owui_policy, "reconcile") as rec:
            modules._policy_maybe_audit(18080, "c")
        rec.assert_not_called()
        modules._policy_audit_at = time.monotonic() - modules.POLICY_AUDIT_INTERVAL_SEC - 1
        with mock.patch.object(modules.owui_policy, "reconcile") as rec:
            modules._policy_maybe_audit(18080, "c")
        rec.assert_called_once()
        self.assertEqual(rec.call_args[0][1], frozenset())

    def test_audit_requires_completed_slot(self) -> None:
        modules._policy_slot_done = None
        with mock.patch.object(modules.owui_policy, "reconcile") as rec:
            modules._policy_maybe_audit(18080, "c")
        rec.assert_not_called()

    def test_pending_triggers_hash_compare(self) -> None:
        fake_manifest = mock.Mock(policy_hash="sha256:new")
        with mock.patch.object(modules.owui_policy, "load_manifest", return_value=fake_manifest), \
             mock.patch.object(modules.owui_policy, "load_state", return_value={"policy_hash": "sha256:new"}):
            self.assertEqual(modules._policy_pending_triggers(), frozenset({"at_boot"}))
        with mock.patch.object(modules.owui_policy, "load_manifest", return_value=fake_manifest), \
             mock.patch.object(modules.owui_policy, "load_state", return_value={"policy_hash": "sha256:old"}):
            self.assertEqual(modules._policy_pending_triggers(),
                             frozenset({"at_boot", "on_upgrade"}))
        with mock.patch.object(modules.owui_policy, "load_manifest",
                               side_effect=modules.owui_policy.ManifestError("bad")), \
             mock.patch.object(modules.owui_policy, "load_state", return_value={}):
            self.assertIn("on_upgrade", modules._policy_pending_triggers())

    def _sticky_env(self) -> dict[str, str]:
        return {"DATABASE_URL": "postgresql://x", "OPENAI_API_KEY": "k"}

    def test_sticky_ready_path_fires_policy_write(self) -> None:
        order: list[str] = []
        with mock.patch("data_services.ensure_provisioned", return_value={"state": "ready"}), \
             mock.patch.object(modules, "enrich_open_webui_secrets",
                               return_value=self._sticky_env()), \
             mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "_container_running", return_value=True), \
             mock.patch.object(modules, "http_ready", return_value=True), \
             mock.patch.object(modules, "_bootstrap_env_ok", return_value=True), \
             mock.patch.object(modules, "ensure_owui_service_identity",
                               side_effect=lambda *_a: order.append("identity") or True), \
             mock.patch.object(modules, "_policy_maybe_write",
                               side_effect=lambda *_a: order.append("policy")):
            result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(order, ["identity", "policy"])

    def test_cutover_path_reconciles_candidate_before_upstream(self) -> None:
        order: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / "open-webui.env"
            env_path.write_text("DATABASE_URL=postgresql://x\n", encoding="utf-8")
            with mock.patch("data_services.ensure_provisioned", return_value={"state": "ready"}), \
                 mock.patch.object(modules, "enrich_open_webui_secrets",
                                   return_value=self._sticky_env()), \
                 mock.patch.object(modules, "_env_file_path", return_value=env_path), \
                 mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
                 mock.patch.object(modules, "_container_running", return_value=False), \
                 mock.patch.object(modules, "http_ready", return_value=False), \
                 mock.patch.object(modules, "http_ready_stable", return_value=True), \
                 mock.patch.object(modules, "_pull_image", return_value=(True, "pulled")), \
                 mock.patch.object(modules, "_run_container", return_value=(True, "c")), \
                 mock.patch.object(modules, "_stop_container"), \
                 mock.patch.object(modules, "ensure_owui_service_identity",
                                   side_effect=lambda *_a: order.append("identity") or True), \
                 mock.patch.object(modules, "_policy_maybe_write",
                                   side_effect=lambda *_a: order.append("policy")), \
                 mock.patch.object(modules, "write_upstream",
                                   side_effect=lambda *_a: order.append("upstream")), \
                 mock.patch.object(modules, "reload_nginx", return_value=True):
                result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(order, ["identity", "policy", "upstream"])

    def test_reconcile_failure_leaves_module_state_machine_alone(self) -> None:
        # Sticky path returns ready even when the reconciler is unhappy; the
        # failure surfaces only via the owui_policy snapshot field.
        with mock.patch("data_services.ensure_provisioned", return_value={"state": "ready"}), \
             mock.patch.object(modules, "enrich_open_webui_secrets",
                               return_value=self._sticky_env()), \
             mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "_container_running", return_value=True), \
             mock.patch.object(modules, "http_ready", return_value=True), \
             mock.patch.object(modules, "_bootstrap_env_ok", return_value=True), \
             mock.patch.object(modules, "ensure_owui_service_identity", return_value=True), \
             mock.patch.object(modules.owui_policy, "reconcile",
                               return_value={"ok": False, "last_error": "gate"}):
            result = modules.ensure_open_webui()
        self.assertEqual(result["state"], "ready")
        self.assertIsNone(modules._policy_slot_done)

    def test_snapshot_exposes_policy_state(self) -> None:
        fake = {"ok": False, "state": "error", "last_error": "boom"}
        with mock.patch.object(modules.owui_policy, "state_summary", return_value=fake), \
             mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "_container_running", return_value=True), \
             mock.patch.object(modules, "http_ready", return_value=True):
            snap = modules.modules_snapshot()["open_webui"]
        policy = snap["owui_policy"]
        self.assertEqual(policy["state"], "error")
        self.assertEqual(policy["last_error"], "boom")
        self.assertEqual(policy["cp_pid"], os.getpid())


class GuardSyncTests(unittest.TestCase):
    """M6.3b Phase 4B: rendering the manifest into the nginx include.

    The contract under test is "the file is a render, not an accumulation": what
    is on disk is always the whole current intent, and the CP only claims what
    nginx actually accepted.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conf = Path(self.tmp.name) / "owui-guards.conf"
        self._saved = (modules.GUARDS_CONF, modules._guards_nginx_sha,
                       modules._guards_error_logged, modules._service_account_id,
                       modules._service_identity_ready)
        modules.GUARDS_CONF = self.conf
        modules._guards_nginx_sha = None
        modules._guards_error_logged = None
        modules._service_account_id = "0f9a1b2c-3d4e-5f60-7182-93a4b5c6d7e8"
        modules._service_identity_ready = True
        # One reload spy for the whole test, so "reloaded exactly once" is
        # observable across several sync() calls.
        self.reload = mock.Mock(return_value=True)
        patcher = mock.patch.object(modules, "reload_nginx", self.reload)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, modules, "GUARDS_CONF", self._saved[0])
        self.addCleanup(setattr, modules, "_guards_nginx_sha", self._saved[1])
        self.addCleanup(setattr, modules, "_guards_error_logged", self._saved[2])
        self.addCleanup(setattr, modules, "_service_account_id", self._saved[3])
        self.addCleanup(setattr, modules, "_service_identity_ready", self._saved[4])
        # Counts are derived from the shipped manifest rather than hard-coded.
        # The enforced set changed twice during M7 (D-17b widened one rule and
        # flipped another), and every literal in this class had to move with it;
        # a guard set that a manifest edit cannot update on its own is a trap.
        guards = modules.owui_policy.load_manifest().guards
        self.enforced = sum(1 for g in guards if g.mode == "enforce")
        self.placeholdered = sum(
            1 for g in guards if g.mode == "enforce" and "%" in g.path
        )
        self.assertEqual(
            self.placeholdered, 1,
            "the user-update guard lost its placeholder in D-17b; if this moved,"
            " the pending-set assertion below is stale, not the manifest",
        )

    def sync(self, reload_ok: bool = True) -> dict:
        self.reload.return_value = reload_ok
        return modules.sync_owui_guards()

    def test_sync_writes_the_rendered_file_and_reloads(self) -> None:
        state = self.sync()
        self.assertTrue(self.conf.exists())
        text = self.conf.read_text(encoding="utf-8")
        self.assertEqual(text.count("if ("), self.enforced)
        self.assertIn("managed-by-lemonseed", text)
        # Placeholders resolved to real ids; the raw %TOKEN% must never reach
        # nginx. (A manifest-authored ``[^/]+`` in a path *is* legitimate now —
        # see the D-17b user-update rule — so this checks for the token, not for
        # the bracket class.)
        self.assertIn(modules._service_account_id, text)
        self.assertNotIn("%CP_OWUI_ID%", text)
        self.assertEqual(self.reload.call_count, 1)
        self.assertTrue(state["in_sync"])
        self.assertEqual(state["enforced_count"], self.enforced)
        self.assertEqual(state["declared"], self.enforced)
        self.assertEqual(state["rendered_sha"], state["declared_sha"])
        self.assertEqual(state["nginx_sha"], state["rendered_sha"])
        self.assertEqual(state["pending"], [])

    def test_unchanged_render_does_not_reload_nginx(self) -> None:
        self.sync()
        state = self.sync()
        self.assertTrue(state["in_sync"])
        self.assertEqual(self.reload.call_count, 1, "second tick must be free")

    def test_failed_reload_is_retried_next_tick(self) -> None:
        state = self.sync(reload_ok=False)
        # The file is right, but nothing claims nginx has it.
        self.assertIsNotNone(state["rendered_sha"])
        self.assertIsNone(state["nginx_sha"])
        self.assertFalse(state["in_sync"])
        self.assertEqual(state["enforced_count"], 0)
        state = self.sync(reload_ok=True)
        self.assertTrue(state["in_sync"])
        self.assertEqual(state["enforced_count"], self.enforced)

    def test_render_error_keeps_the_previous_file(self) -> None:
        self.sync()
        before = self.conf.read_text(encoding="utf-8")
        with mock.patch.object(modules.owui_policy, "load_manifest",
                               side_effect=modules.owui_policy.ManifestError("bad json")), \
             mock.patch.object(modules, "reload_nginx") as reload:
            state = modules.sync_owui_guards()
        self.assertEqual(self.conf.read_text(encoding="utf-8"), before)
        self.assertFalse(state["in_sync"])
        self.assertIn("bad json", state["pending_reason"])
        reload.assert_not_called()

    def test_dropped_guard_disappears_from_the_file(self) -> None:
        """T4-B, end to end: advertise is not "commented out", it is absent."""
        self.sync()
        manifest = modules.owui_policy.load_manifest()
        narrower = mock.Mock(policy_hash=manifest.policy_hash)
        narrower.guards = [g for g in manifest.guards
                           if not (g.mode == "enforce" and g.method == "POST"
                                   and g.path == "/ollama/config/update")]
        with mock.patch.object(modules.owui_policy, "load_manifest", return_value=narrower):
            state = self.sync()
        text = self.conf.read_text(encoding="utf-8")
        self.assertNotIn("/ollama/config/update", text)
        self.assertEqual(text.count("if ("), self.enforced - 1)
        self.assertEqual(state["declared"], self.enforced - 1)
        self.assertTrue(state["in_sync"])

    def test_pending_guards_are_reported_not_guessed(self) -> None:
        modules._service_account_id = None
        state = self.sync()
        self.assertEqual(state["enforced_count"], self.enforced - self.placeholdered)
        # Exactly one rule still waits on a placeholder. D-17b widened the
        # user-update guard to every account, which is what took the last
        # placeholder out of it; the delete rule for the CP's own account keeps
        # its own because its denial copy names that account.
        self.assertEqual(sorted(state["pending"]), [
            "DELETE /api/v1/users/%CP_OWUI_ID%",
        ])
        self.assertIn("unresolved", state["pending_reason"])
        text = self.conf.read_text(encoding="utf-8")
        # The two unresolved rules are absent; the ones that need no placeholder
        # still render, which is why the count above is a subtraction and not 0.
        self.assertNotIn("%CP_OWUI_ID%", text)
        self.assertIn("/api/v1/users/[^/]+/update", text)
        self.assertTrue(state["in_sync"])

    def test_write_is_atomic_and_world_readable(self) -> None:
        self.sync()
        mode = self.conf.stat().st_mode & 0o777
        self.assertEqual(mode, 0o644, "the nginx worker user must be able to read it")
        self.assertFalse(list(Path(self.tmp.name).glob("*.tmp")))

    def test_snapshot_exposes_guard_state(self) -> None:
        self.sync()
        with mock.patch.object(modules.owui_policy, "state_summary", return_value={}), \
             mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "_container_running", return_value=True), \
             mock.patch.object(modules, "http_ready", return_value=True):
            snap = modules.modules_snapshot()["open_webui"]
        self.assertEqual(snap["owui_guards"]["enforced_count"], self.enforced)
        self.assertTrue(snap["owui_guards"]["in_sync"])

    @mock.patch.object(modules, "sync_owui_guards", return_value={})
    @mock.patch.object(modules, "ensure_open_webui", return_value={"state": "ready"})
    @mock.patch.object(modules, "http_ready", return_value=True)
    @mock.patch.object(modules, "_container_running", return_value=True)
    def test_supervisor_syncs_every_tick(
        self, _running: mock.Mock, _ready: mock.Mock, _ensure: mock.Mock, sync: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(modules, "OWUI_READY_FLAG", Path(tmp) / "ready"), \
                 mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
                modules.supervise_modules_once()
        sync.assert_called_once()

    @mock.patch.object(modules, "sync_owui_guards", side_effect=RuntimeError("render blew up"))
    @mock.patch.object(modules, "ensure_open_webui", return_value={"state": "ready"})
    @mock.patch.object(modules, "http_ready", return_value=True)
    @mock.patch.object(modules, "_container_running", return_value=True)
    def test_guard_failure_does_not_kill_the_tick(
        self, _running: mock.Mock, _ready: mock.Mock, ensure: mock.Mock, _sync: mock.Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(modules, "OWUI_READY_FLAG", Path(tmp) / "ready"), \
                 mock.patch.object(modules, "_active_port_from_upstream", return_value=18080):
                result = modules.supervise_modules_once()
        self.assertEqual(result["state"], "ready")
        ensure.assert_called_once()


class ManualReconcileTests(unittest.TestCase):
    """T4-E: the admin "apply Lemonseed settings now" seam."""

    def setUp(self) -> None:
        self._saved = (modules._service_identity_ready, modules._policy_slot_done,
                       modules._policy_audit_at)
        modules._service_identity_ready = True
        modules._policy_slot_done = (18080, "stale")
        modules._policy_audit_at = 0.0
        self.addCleanup(setattr, modules, "_service_identity_ready", self._saved[0])
        self.addCleanup(setattr, modules, "_policy_slot_done", self._saved[1])
        self.addCleanup(setattr, modules, "_policy_audit_at", self._saved[2])

    def test_refuses_before_the_identity_exists(self) -> None:
        modules._service_identity_ready = False
        with mock.patch.object(modules.owui_policy, "reconcile") as rec:
            result = modules.request_policy_reconcile()
        self.assertEqual(result["reason"], "not ready")
        rec.assert_not_called()

    def test_refuses_when_no_slot_is_serving(self) -> None:
        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "http_ready", return_value=False), \
             mock.patch.object(modules.owui_policy, "reconcile") as rec:
            result = modules.request_policy_reconcile()
        self.assertEqual(result["reason"], "not ready")
        rec.assert_not_called()

    def test_forces_a_real_run_and_rearms_the_audit_anchor(self) -> None:
        seen: list[object] = []

        def fake_reconcile(*_args):
            seen.append(modules._policy_slot_done)
            return {"ok": True, "policy_hash": "sha256:x",
                    "drift": {"ok": 4, "fixed": 1, "pending": 0, "error": 0,
                              "entries": [{"key": "openai.api_keys", "value": "sk-SECRET"}]},
                    "last_error": None}

        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "http_ready", return_value=True), \
             mock.patch.object(modules, "sync_owui_guards") as guards, \
             mock.patch.object(modules.owui_policy, "reconcile", side_effect=fake_reconcile) as rec:
            result = modules.request_policy_reconcile()
        # The stale slot marker was cleared *before* the run, so the engine could
        # not skip it as already-done, and re-armed afterwards.
        self.assertEqual(seen, [None])
        self.assertEqual(modules._policy_slot_done, (18080, "lemonseed-owui-18080"))
        self.assertEqual(rec.call_args[0][1], frozenset({"at_boot", "manual"}))
        guards.assert_called_once()
        self.assertTrue(result["ok"])
        self.assertEqual(result["drift"], {"ok": 4, "fixed": 1, "pending": 0, "error": 0})
        # Counts only: the entries (the only place values live) are dropped, so
        # this response cannot carry a credential.
        self.assertNotIn("sk-", json.dumps(result))
        self.assertNotIn("SECRET", json.dumps(result))

    def test_contention_reports_in_flight(self) -> None:
        self.assertTrue(modules._reconcile_request_lock.acquire(blocking=False))
        self.addCleanup(modules._reconcile_request_lock.release)
        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "http_ready", return_value=True), \
             mock.patch.object(modules.owui_policy, "reconcile") as rec:
            result = modules.request_policy_reconcile()
        self.assertEqual(result["reason"], "in flight")
        rec.assert_not_called()

    def test_engine_lock_reports_in_flight_too(self) -> None:
        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "http_ready", return_value=True), \
             mock.patch.object(modules.owui_policy, "reconcile",
                               return_value={"ok": False,
                                             "skipped": "reconcile already running"}):
            result = modules.request_policy_reconcile()
        self.assertEqual(result["reason"], "in flight")
        self.assertIsNone(modules._policy_slot_done)

    def test_failed_run_leaves_the_slot_unclaimed(self) -> None:
        modules._policy_slot_done = None
        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "http_ready", return_value=True), \
             mock.patch.object(modules.owui_policy, "reconcile",
                               return_value={"ok": False, "last_error": "version gate",
                                             "drift": {"ok": 1}}):
            result = modules.request_policy_reconcile()
        self.assertFalse(result["ok"])
        self.assertEqual(result["last_error"], "version gate")
        self.assertIsNone(modules._policy_slot_done)


class ConfigResetTests(unittest.TestCase):
    """T4-C/T4-D: the admin reset window.

    The window's safety rests on ordering (both slots down before the truncate,
    reconcile before any cutover) and on the failure ladder refusing to hand
    users a blank-slate Open WebUI. Both are asserted here as *ordering*, not as
    mocks-called, because a reorder is exactly the regression to catch.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.key_path = Path(self.tmp.name) / "open-webui.cp-key"
        self.key_path.write_text("sk-STALE\n", encoding="utf-8")
        self._saved = (modules._service_identity_ready, modules._service_account_id,
                       modules._policy_slot_done, modules._state, modules._owui_reset)
        modules._service_identity_ready = True
        modules._service_account_id = "svc-1"
        modules._policy_slot_done = (18080, "old")
        modules._state = {"state": "ready"}
        modules._owui_reset = {"state": "idle", "error": None, "at": None}
        for name, value in (("_service_identity_ready", self._saved[0]),
                            ("_service_account_id", self._saved[1]),
                            ("_policy_slot_done", self._saved[2]),
                            ("_state", self._saved[3]),
                            ("_owui_reset", self._saved[4])):
            self.addCleanup(setattr, modules, name, value)
        if modules._lock.locked():
            modules._lock.release()
        self.addCleanup(self._release_lock)

    def _release_lock(self) -> None:
        if modules._lock.locked():
            modules._lock.release()

    def _harness(self, order: list[str], reconcile_results: list[dict],
                 lkg_ok: bool = True) -> list[mock.Mock]:
        """Patch every side-effecting step to record ordering. Returns [fakes]."""
        env_path = Path(self.tmp.name) / "open-webui.env"
        env_path.write_text("DATABASE_URL=x\n", encoding="utf-8")

        def rec(step):
            def _inner(*_args, **_kwargs):
                order.append(step)
            return _inner

        fakes = {
            "_stop_container": mock.Mock(side_effect=lambda name: order.append(f"stop:{name}")),
            "_run_container": mock.Mock(side_effect=lambda port, _env: order.append(f"run:{port}") or (True, "c")),
            "_wait_slot_ready": mock.Mock(side_effect=lambda _port: order.append("ready") or True),
            "ensure_owui_service_identity": mock.Mock(
                side_effect=lambda _port: order.append("identity") or True),
            "write_upstream": mock.Mock(side_effect=lambda port: order.append(f"upstream:{port}")),
            "reload_nginx": mock.Mock(return_value=True),
            "sync_owui_guards": mock.Mock(side_effect=rec("guards")),
            "_env_file_path": mock.Mock(return_value=env_path),
            "_owui_cp_key_path": mock.Mock(return_value=self.key_path),
        }
        patches = [
            mock.patch.object(modules, name, fake) for name, fake in fakes.items()
        ]
        patches.append(mock.patch.object(modules, "OWUI_READY_FLAG",
                                         Path(self.tmp.name) / "owui-ready"))
        patches.append(mock.patch.object(modules, "_active_port_from_upstream",
                                         return_value=18080))
        patches.append(mock.patch.object(modules, "http_ready", return_value=True))
        patches.append(mock.patch.object(
            modules.db, "truncate_owui_config",
            side_effect=lambda: order.append("truncate") or {"state": "ready", "error": None}))
        patches.append(mock.patch.object(
            modules.owui_policy, "reconcile",
            side_effect=lambda *_, **__: order.append("reconcile")
            or reconcile_results.pop(0)))
        patches.append(mock.patch.object(
            modules, "_restore_last_known_good",
            side_effect=lambda _port: order.append("lkg") or lkg_ok))
        # Run the "daemon" thread inline so the assertions are deterministic.
        patches.append(mock.patch("modules.threading.Thread", _SyncThread))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return [fakes[k] for k in ("_stop_container", "_run_container", "write_upstream")]

    def test_refuses_without_the_typed_confirmation(self) -> None:
        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "http_ready", return_value=True), \
             mock.patch.object(modules.db, "truncate_owui_config") as cut:
            for wrong in ("", "reset chat settings", "reset now", "rest", "re"):
                result = modules.reset_owui_config(wrong)
                self.assertFalse(result["accepted"], wrong)
        cut.assert_not_called()
        self.assertFalse(modules._lock.locked(), "refusal must not take the module lock")

    def test_confirmation_is_one_short_word_case_insensitively(self) -> None:
        """Bare-metal review: the long uppercase phrase read as a transcription
        test. One short word, matched case-insensitively, still stops an
        accidental click while staying something a person will actually type.
        """
        # Not ready short-circuits *after* the confirmation check, so a reason
        # other than "confirmation text did not match" proves the word passed.
        modules._service_identity_ready = False
        for accepted in ("reset", "Reset", "RESET", "  reset  "):
            result = modules.reset_owui_config(accepted)
            self.assertNotEqual(result["reason"], "confirmation text did not match",
                                accepted)
        self.assertEqual(modules.OWUI_RESET_CONFIRM, "reset")

    def test_refuses_when_not_ready(self) -> None:
        modules._service_identity_ready = False
        with mock.patch.object(modules, "http_ready", return_value=True):
            result = modules.reset_owui_config(modules.OWUI_RESET_CONFIRM)
        self.assertEqual(result["reason"], "not ready")
        self.assertFalse(modules._lock.locked())

    def test_refuses_while_another_change_holds_the_slots(self) -> None:
        self.assertTrue(modules._lock.acquire(blocking=False))
        with mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "http_ready", return_value=True):
            result = modules.reset_owui_config(modules.OWUI_RESET_CONFIRM)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "a change is already in progress")

    def test_window_order_and_cutover(self) -> None:
        order: list[str] = []
        self._harness(order, [{"ok": True}])
        result = modules.reset_owui_config(modules.OWUI_RESET_CONFIRM)
        self.assertTrue(result["accepted"])
        self.assertEqual(order, [
            "stop:lemonseed-owui-18080", "stop:lemonseed-owui-18081",
            "truncate", "run:18081", "ready", "identity", "reconcile",
            "upstream:18081", "guards",
        ])
        # Both slots go down before the shared database is touched, and the
        # candidate is reconciled before nginx is pointed at it.
        self.assertLess(order.index("stop:lemonseed-owui-18081"), order.index("truncate"))
        self.assertLess(order.index("reconcile"), order.index("upstream:18081"))
        # Truncate exactly once, through the one function that owns the SQL.
        self.assertEqual(order.count("truncate"), 1)
        self.assertEqual(modules.owui_reset_state()["state"], "ready")
        self.assertEqual(modules._state["state"], "ready")
        self.assertEqual(modules._state["port"], 18081)
        self.assertFalse(modules._lock.locked(), "the window must release the module lock")

    def test_stale_cp_key_is_dropped_before_the_reconcile(self) -> None:
        order: list[str] = []
        self._harness(order, [{"ok": True}])
        modules.reset_owui_config(modules.OWUI_RESET_CONFIRM)
        self.assertFalse(self.key_path.exists(),
                         "the sk- key dies with the config table it was minted against")

    def test_identity_memory_is_cleared_so_signin_repeats(self) -> None:
        order: list[str] = []
        self._harness(order, [{"ok": True}])
        modules.reset_owui_config(modules.OWUI_RESET_CONFIRM)
        self.assertEqual(order.count("identity"), 1)

    def test_reconcile_failure_uses_the_lkg_then_retries(self) -> None:
        order: list[str] = []
        self._harness(order, [{"ok": False, "last_error": "gate"}, {"ok": True}])
        modules.reset_owui_config(modules.OWUI_RESET_CONFIRM)
        self.assertEqual(order.count("lkg"), 1)
        self.assertEqual(order.count("reconcile"), 2)
        first = order.index("reconcile")
        self.assertLess(first, order.index("lkg"))
        self.assertLess(order.index("lkg"), order.index("reconcile", first + 1))
        self.assertIn("upstream:18081", order)
        self.assertEqual(modules.owui_reset_state()["state"], "ready")

    def test_unrecoverable_reset_never_cuts_over(self) -> None:
        """A wiped-but-unreconciled slot must not be handed to users (D1)."""
        order: list[str] = []
        _stop, _run, upstream = self._harness(
            order,
            [{"ok": False, "last_error": "gate"}, {"ok": False, "last_error": "gate"}],
        )
        modules.reset_owui_config(modules.OWUI_RESET_CONFIRM)
        upstream.assert_not_called()
        self.assertNotIn("upstream:18081", order)
        state = modules.owui_reset_state()
        self.assertEqual(state["state"], "error")
        self.assertIn("gate", state["error"])
        self.assertEqual(modules._state["state"], "error")
        self.assertFalse(modules._lock.locked())

    def test_truncate_failure_stops_before_starting(self) -> None:
        order: list[str] = []
        self._harness(order, [{"ok": True}])
        with mock.patch.object(modules.db, "truncate_owui_config",
                               side_effect=lambda: order.append("truncate")
                               or {"state": "error", "error": "db down"}):
            modules.reset_owui_config(modules.OWUI_RESET_CONFIRM)
        self.assertEqual(order.count("truncate"), 1)
        self.assertNotIn("ready", order)
        self.assertNotIn("reconcile", order)
        state = modules.owui_reset_state()
        self.assertEqual(state["state"], "error")
        self.assertIn("db down", state["error"])
        self.assertFalse(modules._lock.locked())

    def test_snapshot_reports_reset_progress(self) -> None:
        modules._owui_reset = {"state": "reconciling", "error": None, "at": "now"}
        with mock.patch.object(modules.owui_policy, "state_summary", return_value={}), \
             mock.patch.object(modules, "_active_port_from_upstream", return_value=18080), \
             mock.patch.object(modules, "_container_running", return_value=False), \
             mock.patch.object(modules, "http_ready", return_value=False):
            snap = modules.modules_snapshot()["open_webui"]
        # A reset window must never look "ready", even if the old slot still
        # answers HTTP — the config table is empty underneath it.
        self.assertEqual(snap["state"], "resetting")
        self.assertFalse(snap["http_ready"])
        self.assertEqual(snap["owui_reset"]["state"], "reconciling")
        self.assertIn("Rebuilding", snap["task"])


class ServiceIdentityContractTests(unittest.TestCase):
    """The CP service account is shaped like a portal identity, not a one-off."""

    def test_service_email_shares_the_portal_domain(self) -> None:
        # OWUI keys identity on email and shows the service account beside human
        # accounts in one admin list, so a different domain reads as an
        # unrelated system (and had to be explained away on every box).
        self.assertEqual(
            modules.OWUI_SERVICE_EMAIL.rsplit("@", 1)[-1], auth.OWUI_EMAIL_DOMAIN
        )
        self.assertEqual(modules.OWUI_SERVICE_EMAIL, "lemonseed-cp@localhost")


class ContainerStatusTests(unittest.TestCase):
    """The Chat panel quotes docker's own status wording, so it must stay honest."""

    # 2026-10-08T12:59:00Z — 3540 s (59 min) after the started stamps below.
    NOW = 1791464340.0

    def test_duration_uses_whole_units_like_docker(self) -> None:
        self.assertEqual(modules._docker_duration(0), "0 seconds")
        self.assertEqual(modules._docker_duration(9.4), "9 seconds")
        self.assertEqual(modules._docker_duration(60), "1 minute")
        self.assertEqual(modules._docker_duration(3599), "59 minutes")
        self.assertEqual(modules._docker_duration(3600), "1 hour")
        self.assertEqual(modules._docker_duration(7200), "2 hours")
        self.assertEqual(modules._docker_duration(86400), "1 day")
        self.assertEqual(modules._docker_duration(90000), "1 day")

    def test_docker_timestamps_are_read_as_utc(self) -> None:
        """Parsing them as local time would skew uptime by the tz offset."""
        self.assertEqual(modules._docker_epoch("2026-01-01T00:00:00.123456789Z"), 1767225600.0)
        self.assertIsNone(modules._docker_epoch("0001-01-01T00:00:00Z"))
        self.assertIsNone(modules._docker_epoch(""))
        self.assertIsNone(modules._docker_epoch("nonsense"))

    def test_running_container_reports_uptime_and_health(self) -> None:
        fields = [
            "running",
            "2026-10-08T12:00:00.123456789Z",
            "0001-01-01T00:00:00Z",
            "0",
            "healthy",
            "ghcr.io/open-webui/open-webui:v0.11.0",
        ]
        status, image = modules.format_container_status(fields, self.NOW)
        self.assertEqual(status, "Up 59 minutes (healthy)")
        self.assertEqual(image, "ghcr.io/open-webui/open-webui:v0.11.0")

    def test_container_without_a_healthcheck_omits_the_parenthesis(self) -> None:
        fields = ["running", "2026-10-08T12:58:00Z", "0001-01-01T00:00:00Z", "0", "", "img"]
        self.assertEqual(modules.format_container_status(fields, self.NOW)[0], "Up 1 minute")

    def test_exited_container_reports_code_and_how_long_ago(self) -> None:
        fields = ["exited", "2026-10-08T11:00:00Z", "2026-10-08T12:47:00Z", "137", "", "img"]
        self.assertEqual(
            modules.format_container_status(fields, self.NOW)[0],
            "Exited (137) 12 minutes ago",
        )

    def test_other_states_are_simply_named(self) -> None:
        for state, expected in (("paused", "Paused"), ("restarting", "Restarting")):
            fields = [state, "2026-10-08T12:58:00Z", "0001-01-01T00:00:00Z", "0", "", "img"]
            self.assertEqual(modules.format_container_status(fields, self.NOW)[0], expected)

    def test_unusable_inspect_output_is_none(self) -> None:
        self.assertIsNone(modules.format_container_status(["<no value>"], self.NOW))
        self.assertIsNone(modules.format_container_status([""], self.NOW))

    def test_missing_docker_yields_no_status(self) -> None:
        """No docker (dev machine) must not look like a stopped container."""
        with mock.patch.object(
            modules, "_docker", return_value=mock.Mock(returncode=127, stdout="", stderr="FileNotFoundError")
        ):
            self.assertIsNone(modules.owui_container_status("lemonseed-owui-18080"))

    def test_inspect_targets_the_active_slot_container(self) -> None:
        seen: dict[str, list[str]] = {}

        def fake_docker(args: list[str], timeout: float = 120) -> mock.Mock:
            seen["args"] = list(args)
            return mock.Mock(
                returncode=0,
                stdout="running|2026-10-08T12:00:00Z|0001-01-01T00:00:00Z|0|starting|ghcr.io/x:v1\n",
                stderr="",
            )

        with mock.patch.object(modules, "_docker", side_effect=fake_docker), mock.patch.object(
            modules, "_active_port_from_upstream", return_value=18081
        ):
            status, image = modules.owui_container_status()
        # Uptime arithmetic is covered against a fixed clock in
        # format_container_status; here we only wire the inspect call.
        self.assertTrue(status.startswith("Up "), status)
        self.assertTrue(status.endswith("(starting)"), status)
        self.assertEqual(image, "ghcr.io/x:v1")
        self.assertEqual(seen["args"][-1], "lemonseed-owui-18081")
        self.assertIn("{{.State.Status}}", seen["args"][2])


class _SyncThread:
    """Stand-in for threading.Thread that runs the target inline."""

    instances: list["_SyncThread"] = []

    def __init__(self, target=None, args=(), kwargs=None, name=None, daemon=None) -> None:
        self._target = target
        self._args = args or ()
        self._kwargs = kwargs or {}
        _SyncThread.instances.append(self)

    def start(self) -> None:
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self) -> bool:
        return False


if __name__ == "__main__":
    raise SystemExit(unittest.main())
