#!/usr/bin/env python3
"""Unit tests for module_manager.py (M9 P2): the lifecycle state machine, the
run fingerprint, hook execution, platform contributions and supervision.

No real Docker, Postgres or hook scripts: Docker's argv surface is faked
(the module spec is the contract), the db.py module-row functions are faked,
and the catalog is handed in directly. The real catalog, spec validation and
digest chain are covered by test_modules_catalog.py.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import module_manager as mm  # noqa: E402
import modules_catalog as mc  # noqa: E402

Result = namedtuple("Result", ["returncode", "stdout", "stderr"])

PENDING = mm.PENDING
WAITING = mm.WAITING
INSTALLED = mm.INSTALLED
ENABLED = mm.ENABLED
BROKEN = mm.BROKEN


def spec_for(module_id: str, **overrides: object) -> dict:
    """A minimal-but-valid v1 spec (mirrors test_modules_catalog.valid_spec)."""
    spec = {
        "spec_version": 1,
        "id": module_id,
        "name": module_id.title(),
        "type": "application",
        "version": "1.0.0",
        "maturity": "beta",
        "license": "Apache-2.0",
        "owners": ["wago"],
        "docker": {
            "image": "hello_world",
            "container": f"lemonseed-{module_id}",
        },
    }
    spec.update(overrides)
    return spec


class FakeDocker:
    """Records argv and answers the inspect/rm/run/start contract."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.running = False
        self.fail_runs = 0

    def __call__(self, args: list[str], timeout: int | None = None) -> Result:
        self.calls.append(list(args))
        if args[0] == "run":
            if self.fail_runs:
                self.fail_runs -= 1
                return Result(1, "", "run failed (fake)")
            self.running = True
            return Result(0, "", "")
        if args[0] == "rm":
            self.running = False
            return Result(0, "", "")
        if args[0] == "start":
            self.running = True
            return Result(0, "", "")
        if args[0] == "inspect":
            return Result(0, "true" if self.running else "false", "")
        return Result(0, "", "")

    def runs(self) -> list[list[str]]:
        return [call for call in self.calls if call[0] == "run"]


class FakeDb:
    """A stand-in for db.py's module-row functions (tests run unprivileged,
    without Postgres). Records every write so the tests can assert order."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.calls: list[tuple[str, tuple]] = []
        self.passwords: dict[str, str] = {}
        self.envs: dict[str, dict[str, str]] = {}
        self.dropped_dbs: list[str] = []

    def module_rows(self) -> list[dict]:
        out = []
        for module_id in sorted(self.rows):
            row = dict(self.rows[module_id])
            row["id"] = module_id
            out.append(row)
        return out

    def module_row(self, module_id: str) -> dict | None:
        row = self.rows.get(module_id)
        if row is None:
            return None
        row = dict(row)
        row["id"] = module_id
        return row

    def set_module_state(
        self,
        module_id: str,
        state: str,
        *,
        version: str = "",
        settings: dict | None = None,
        error: str | None = None,
        run_hash: str = "",
    ) -> None:
        self.calls.append(("set_module_state", (module_id, state)))
        row = self.rows.setdefault(
            module_id,
            {"state": "", "version": "", "settings": {}, "error": None, "run_hash": ""},
        )
        row["state"] = state
        if version:
            row["version"] = version
        if settings is not None:
            row["settings"] = settings
        row["error"] = error
        if run_hash:
            row["run_hash"] = run_hash

    def provision_module_db(self, module_id: str, db_name: str) -> None:
        self.calls.append(("provision_module_db", (module_id, db_name)))
        self.passwords[module_id] = f"pw-{module_id}"

    def write_module_env(self, module_id: str, updates: dict[str, str]) -> None:
        self.calls.append(("write_module_env", (module_id, dict(updates))))
        self.envs.setdefault(module_id, {}).update(updates)

    def drop_module_row(self, module_id: str) -> None:
        self.calls.append(("drop_module_row", (module_id,)))
        self.rows.pop(module_id, None)

    def drop_module_db(self, module_id: str) -> None:
        self.calls.append(("drop_module_db", (module_id,)))
        self.dropped_dbs.append(module_id)

    def _module_credentials(self, module_id: str) -> dict[str, str]:
        env = dict(self.envs.get(module_id) or {})
        if module_id in self.passwords:
            env["POSTGRES_DB"] = f"mod_{module_id.replace('-', '_')}"
            env["POSTGRES_PASSWORD"] = self.passwords[module_id]
            env["DATABASE_URL"] = (
                f"postgresql://mod_{module_id.replace('-', '_')}:"
                f"{self.passwords[module_id]}@127.0.0.1:5432/"
                f"mod_{module_id.replace('-', '_')}"
            )
        return env

    def _module_row_dict(self, module_id: str) -> dict | None:
        return self.module_row(module_id)

    def enabled_order(self) -> list[str]:
        return [
            call[1][0]
            for call in self.calls
            if call[0] == "set_module_state" and call[1][1] == ENABLED
        ]


class ModuleCase(unittest.TestCase):
    """Common harness: faked docker, db, data-services and hooks."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.secrets = self.root / "secrets"
        self.secrets.mkdir()
        self.data_root = self.root / "modules-data"
        self.db = FakeDb()
        self.docker = FakeDocker()
        self.hooks: list[tuple[str, str]] = []
        self.catalog = {"modules": {}, "problems": [], "catalog_version": 1}

        def fake_run_hook(module_id, event, spec=None, payload=None):
            self.hooks.append((module_id, event, bool(payload)))
            return True, ""

        patches = [
            mock.patch.object(mm, "_catalog", lambda: self.catalog),
            mock.patch.object(mm, "_docker", self.docker),
            mock.patch.object(mm, "_pull_image", lambda image, name: (True, "")),
            mock.patch.object(mm, "_resolve_ref", lambda image: "ref:local"),
            mock.patch.object(mm, "_ensure_lemon_network", lambda: True),
            mock.patch.object(mm, "_wait_data_plane", lambda: None),
            mock.patch.object(mm, "DATA_ROOT", self.data_root),
            mock.patch.object(mm, "run_hook", fake_run_hook),
            mock.patch.object(audit_mod(), "record", lambda *args, **kwargs: True),
            mock.patch.object(mc, "module_root", lambda: self.root / "modulesrc"),
            mock.patch.object(mc, "shipped_catalog_path", lambda: self.root / "catalog-shipped.json"),
            mock.patch.object(mc, "durable_catalog_path", lambda: self.root / "catalog-durable.json"),
            mock.patch.object(mm, "VERSION_FILE", self.root / "appliance-version"),
            mock.patch.object(db_mod(), "SECRETS_MODULES", self.secrets),
            mock.patch.object(db_mod(), "_module_credentials", self.db._module_credentials),
            mock.patch.object(db_mod(), "module_rows", self.db.module_rows),
            mock.patch.object(db_mod(), "module_row", self.db.module_row),
            mock.patch.object(db_mod(), "set_module_state", self.db.set_module_state),
            mock.patch.object(db_mod(), "provision_module_db", self.db.provision_module_db),
            mock.patch.object(db_mod(), "write_module_env", self.db.write_module_env),
            mock.patch.object(db_mod(), "drop_module_row", self.db.drop_module_row),
            mock.patch.object(db_mod(), "drop_module_db", self.db.drop_module_db),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _wait(self, predicate, timeout: float = 5.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("operation did not finish in time")

    def _wait_op_done(self) -> None:
        self._wait(lambda: mm._op_progress is None)

    def add_module(self, module_id: str, **overrides: object) -> dict:
        spec = spec_for(module_id, **overrides)
        self.catalog["modules"][module_id] = spec
        return spec

    def assert_state(self, module_id: str, state: str) -> None:
        self.assertEqual(self.row(module_id)["state"], state)

    def row(self, module_id: str) -> dict:
        """The module row, asserted present so Pyright sees a dict."""
        row = self.db.module_row(module_id)
        self.assertIsNotNone(row)
        return row


def db_mod():
    import db

    return db


def audit_mod():
    import audit

    return audit


class InstallTests(ModuleCase):
    def test_install_marks_pending_then_enabled(self) -> None:
        spec = self.add_module("demo")
        result = mm.request_install("demo")
        self.assertTrue(result["accepted"], result)
        self.assert_state("demo", PENDING)
        self._wait_op_done()
        self.assert_state("demo", ENABLED)
        self.assertEqual(self.db.enabled_order(), ["demo"])
        # The run fingerprint was recorded at install time, so the first
        # supervision tick sees no drift.
        row = self.db.module_row("demo")
        self.assertTrue(row["run_hash"])
        env_path = mm._container_env_path("demo")
        self.assertEqual(row["run_hash"], mm.run_fingerprint(spec, env_path))

    def test_install_not_in_catalog_refused(self) -> None:
        result = mm.request_install("ghost")
        self.assertFalse(result["accepted"])
        self.assertIn("catalog", result["reason"])

    def test_install_refuses_when_already_installed(self) -> None:
        self.add_module("demo")
        self.db.rows["demo"] = {"state": ENABLED, "version": "", "settings": {}, "error": None, "run_hash": "x"}
        result = mm.request_install("demo")
        self.assertFalse(result["accepted"])
        self.assertIn("already", result["reason"])

    def test_install_provisions_postgres_and_valkey(self) -> None:
        self.add_module(
            "demo",
            data={"postgres": {"db_name": "demo_db"}, "valkey": {"db": 4}},
        )
        mm.request_install("demo")
        self._wait_op_done()
        self.assert_state("demo", ENABLED)
        calls = [c[0] for c in self.db.calls if c[0] in ("provision_module_db", "write_module_env")]
        self.assertEqual(calls[0], "provision_module_db")
        env = self.db.envs["demo"]
        self.assertEqual(env["VALKEY_URL"], "redis://127.0.0.1:6379/4")
        self.assertEqual(env["VALKEY_DB"], "4")
        # The container env file rewrites the host-scoped URL to the service name.
        container_env = mm._container_env_path("demo")
        text = container_env.read_text(encoding="utf-8")
        self.assertIn("DATABASE_URL=postgresql://mod_demo:", text)
        self.assertIn("VALKEY_URL=redis://lemonseed-valkey:6379/4", text)

    def test_settings_defaults_reach_the_env(self) -> None:
        self.add_module(
            "demo",
            settings=[{"name": "GREETING", "type": "string", "default": "hi"}],
        )
        mm.request_install("demo")
        self._wait_op_done()
        self.assertEqual(self.db.envs["demo"].get("GREETING"), "hi")

    def test_hooks_run_in_order(self) -> None:
        self.add_module("demo")
        mm.request_install("demo")
        self._wait_op_done()
        self.assertEqual(
            [event for module_id, event, _ in self.hooks if module_id == "demo"],
            ["install", "start"],
        )

    def test_dependency_auto_install_topological(self) -> None:
        self.add_module("aa")
        self.add_module("bb", dependencies={"modules": ["aa"]})
        mm.request_install("bb")
        self._wait_op_done()
        self.assertEqual(self.db.enabled_order(), ["aa", "bb"])

    def test_dependency_cycle_refused(self) -> None:
        self.add_module("aa", dependencies={"modules": ["bb"]})
        self.add_module("bb", dependencies={"modules": ["aa"]})
        result = mm.request_install("aa")
        self.assertFalse(result["accepted"])
        self.assertIn("cycle", result["reason"])

    def test_pull_failure_waits_and_the_tick_retries(self) -> None:
        self.add_module("demo")
        with mock.patch.object(
            mm, "_pull_image", lambda image, name: (False, "Cannot reach the container registry")
        ):
            result = mm.request_install("demo")
            self.assertTrue(result["accepted"], result)
            self._wait_op_done()
        # A pull failure is a wait, not a verdict (§7 step 6): the row records
        # WAITING with the reason, and the container was never started.
        self.assert_state("demo", WAITING)
        self.assertIn("registry", self.row("demo")["error"])
        self.assertEqual(self.docker.runs(), [])
        # The supervision tick re-runs the install flow once the registry is
        # reachable again.
        mm.supervise_once()
        self.assert_state("demo", ENABLED)

    def test_install_hook_failure_marks_broken(self) -> None:
        self.add_module("demo")

        def failing_run_hook(module_id, event, spec=None, payload=None):
            if event == "install":
                return False, "boom"
            return True, ""

        with mock.patch.object(mm, "run_hook", failing_run_hook):
            result = mm.request_install("demo")
            self.assertTrue(result["accepted"], result)
            self._wait_op_done()
        self.assert_state("demo", BROKEN)
        self.assertIn("hooks/install.sh failed", self.row("demo")["error"])


class EnableDisableTests(ModuleCase):
    def _installed(self, module_id: str = "demo") -> dict:
        spec = self.add_module(module_id)
        self.db.rows[module_id] = {
            "state": INSTALLED,
            "version": "1.0.0",
            "settings": {},
            "error": None,
            "run_hash": "",
        }
        return spec

    def test_enable_recreates_and_runs_start_hook(self) -> None:
        self._installed()
        result = mm.request_enable("demo")
        self.assertTrue(result["accepted"], result)
        self._wait_op_done()
        self.assert_state("demo", ENABLED)
        self.assertEqual([e for module_id, e, _ in self.hooks if module_id == "demo"], ["start"])
        self.assertTrue(self.docker.runs())

    def test_enable_refused_when_gone_from_catalog(self) -> None:
        self._installed()
        self.catalog["modules"].pop("demo")
        result = mm.request_enable("demo")
        self.assertFalse(result["accepted"])
        self.assertIn("no longer in the catalog", result["reason"])

    def test_disable_stops_container_and_runs_stop_hook(self) -> None:
        self._installed()
        self.db.rows["demo"]["state"] = ENABLED
        mm.request_disable("demo")
        self._wait_op_done()
        self.assert_state("demo", INSTALLED)
        self.assertEqual([e for module_id, e, _ in self.hooks if module_id == "demo"], ["stop"])
        self.assertFalse(self.docker.running)

    def test_disable_refused_when_not_enabled(self) -> None:
        self._installed()
        result = mm.request_disable("demo")
        self.assertFalse(result["accepted"])
        self.assertIn("not enabled", result["reason"])

    def test_disable_records_hook_failure_without_lying(self) -> None:
        self._installed()
        self.db.rows["demo"]["state"] = ENABLED

        def failing_run_hook(module_id, event, spec=None, payload=None):
            return (False, "boom") if event == "stop" else (True, "")

        with mock.patch.object(mm, "run_hook", failing_run_hook):
            result = mm.request_disable("demo")
            self.assertTrue(result["accepted"], result)
            self._wait_op_done()
        # The container is down either way; the state records the hook error.
        self.assertFalse(self.docker.running)
        row = self.db.module_row("demo")
        self.assertEqual(row["state"], INSTALLED)
        self.assertIn("hooks/stop.sh failed", row["error"])


class RemoveTests(ModuleCase):
    def _enabled(self, module_id: str = "demo") -> dict:
        spec = self.add_module(module_id)
        self.db.rows[module_id] = {
            "state": ENABLED,
            "version": "1.0.0",
            "settings": {},
            "error": None,
            "run_hash": "x",
        }
        (mm._module_volume(module_id) / "greeting.txt").parent.mkdir(parents=True)
        (mm._module_volume(module_id) / "greeting.txt").write_text("hi", encoding="utf-8")
        return spec

    def test_remove_purges_data_with_confirm(self) -> None:
        self._enabled()
        result = mm.request_remove("demo", preserve=False, confirm="purge")
        self.assertTrue(result["accepted"], result)
        self._wait_op_done()
        self.assertNotIn("demo", self.db.rows)
        self.assertEqual(self.db.dropped_dbs, ["demo"])
        self.assertFalse(mm._module_volume("demo").exists())

    def test_remove_purge_requires_typed_confirm(self) -> None:
        self._enabled()
        result = mm.request_remove("demo", preserve=False, confirm="remove")
        self.assertFalse(result["accepted"])
        self.assertIn("purge", result["reason"])
        self.assertIn("demo", self.db.rows)

    def test_remove_preserves_data(self) -> None:
        self._enabled()
        result = mm.request_remove("demo", preserve=True)
        self.assertTrue(result["accepted"], result)
        self._wait_op_done()
        self.assertNotIn("demo", self.db.rows)
        self.assertEqual(self.db.dropped_dbs, [])
        self.assertTrue((mm._module_volume("demo") / "greeting.txt").exists())

    def test_remove_refused_with_dependents(self) -> None:
        self._enabled()
        self.add_module("bb", dependencies={"modules": ["demo"]})
        self.db.rows["bb"] = {"state": ENABLED, "version": "", "settings": {}, "error": None, "run_hash": ""}
        result = mm.request_remove("demo", preserve=True)
        self.assertFalse(result["accepted"])
        self.assertIn("depend", result["reason"])
        self.assertIn("demo", self.db.rows)

    def test_remove_refused_when_not_installed(self) -> None:
        result = mm.request_remove("demo", preserve=True)
        self.assertFalse(result["accepted"])
        self.assertIn("not installed", result["reason"])

    def test_remove_hook_failure_keeps_the_row_for_retry(self) -> None:
        self._enabled()

        def failing_run_hook(module_id, event, spec=None, payload=None):
            return (False, "boom") if event == "remove" else (True, "")

        with mock.patch.object(mm, "run_hook", failing_run_hook):
            result = mm.request_remove("demo", preserve=True)
            self.assertTrue(result["accepted"], result)
            self._wait_op_done()
        row = self.db.module_row("demo")
        self.assertEqual(row["state"], INSTALLED)
        self.assertIn("hooks/remove.sh failed", row["error"])


class SettingsTests(ModuleCase):
    def test_save_settings_merges_into_the_env(self) -> None:
        self.add_module(
            "demo", settings=[{"name": "GREETING", "type": "string", "default": "x"}]
        )
        self.db.rows["demo"] = {"state": ENABLED, "version": "", "settings": {}, "error": None, "run_hash": ""}
        result = mm.save_settings("demo", {"GREETING": "Hi"})
        self.assertEqual(result["state"], "ready")
        self.assertEqual(self.db.rows["demo"]["settings"]["GREETING"], "Hi")
        self.assertEqual(self.db.envs["demo"]["GREETING"], "Hi")

    def test_save_settings_validates(self) -> None:
        self.add_module(
            "demo",
            settings=[{"name": "GREETING", "type": "string"}, {"name": "COUNT", "type": "int"}],
        )
        self.db.rows["demo"] = {"state": ENABLED, "version": "", "settings": {}, "error": None, "run_hash": ""}
        self.assertEqual(
            mm.save_settings("demo", {"NOPE": "x"})["state"], "error"
        )
        self.assertEqual(mm.save_settings("demo", {"COUNT": 3})["state"], "ready")
        self.assertEqual(self.db.envs["demo"]["COUNT"], "3")

    def test_save_settings_rejected_when_module_gone(self) -> None:
        result = mm.save_settings("demo", {})
        self.assertEqual(result["state"], "error")


class ContributionTests(ModuleCase):
    def test_plugin_contribution_when_enabled(self) -> None:
        self.add_module(
            "demo",
            type="plugin",
            platform={"litellm": {"mcp_server": {"name": "demo", "url": "http://lemonseed-demo:80/mcp"}}},
        )
        self.db.rows["demo"] = {"state": INSTALLED, "version": "", "settings": {}, "error": None, "run_hash": ""}
        self.assertEqual(mm.litellm_contributions(), [])
        self.db.rows["demo"]["state"] = ENABLED
        self.assertEqual(
            mm.litellm_contributions(),
            [{"name": "demo", "url": "http://lemonseed-demo:80/mcp"}],
        )
        # Disabling really does remove the endpoint from the chat path.
        self.db.rows["demo"]["state"] = INSTALLED
        self.assertEqual(mm.litellm_contributions(), [])

    def test_application_type_never_contributes(self) -> None:
        self.add_module("demo")
        self.db.rows["demo"] = {"state": ENABLED, "version": "", "settings": {}, "error": None, "run_hash": ""}
        self.assertEqual(mm.litellm_contributions(), [])

    def test_broken_catalog_contributes_nothing(self) -> None:
        with mock.patch.object(mm, "_catalog", lambda: (_ for _ in ()).throw(RuntimeError("x"))):
            self.assertEqual(mm.litellm_contributions(), [])


class FingerprintTests(ModuleCase):
    def test_env_change_changes_the_fingerprint(self) -> None:
        spec = self.add_module("demo")
        env_path = mm._container_env_path("demo")
        base = mm.run_fingerprint(spec, env_path)
        env_path.write_text("EXTRA=1\n", encoding="utf-8")
        self.assertNotEqual(base, mm.run_fingerprint(spec, env_path))

    def test_fingerprint_is_stable_across_calls(self) -> None:
        spec = self.add_module("demo")
        env_path = mm._container_env_path("demo")
        self.assertEqual(
            mm.run_fingerprint(spec, env_path), mm.run_fingerprint(spec, env_path)
        )

    def test_container_args_shape(self) -> None:
        spec = self.add_module(
            "demo",
            docker={
                "image": "hello_world",
                "container": "lemonseed-demo",
                "ports": ["127.0.0.1:18100:80"],
                "volumes": ["/var/lib/lemonseed/modules/demo:/data"],
                "run_args": ["--read-only"],
            },
        )
        args = mm.container_args(spec, Path("/tmp/env"), "img:ref")
        self.assertEqual(
            args,
            [
                "run",
                "-d",
                "--name",
                "lemonseed-demo",
                "--restart",
                "no",
                "--network",
                "lemon",
                "--add-host",
                "host.docker.internal:host-gateway",
                "-p",
                "127.0.0.1:18100:80",
                "-v",
                "/var/lib/lemonseed/modules/demo:/data",
                "--env-file",
                "/tmp/env",
                "--read-only",
                "img:ref",
            ],
        )


class SupervisionTests(ModuleCase):
    def _enabled(self, module_id: str = "demo", **overrides: object) -> dict:
        spec = self.add_module(module_id, **overrides)
        self.db.rows[module_id] = {
            "state": ENABLED,
            "version": "1.0.0",
            "settings": {},
            "error": None,
            "run_hash": "",
        }
        return spec

    def test_recreates_on_env_drift(self) -> None:
        spec = self._enabled()
        env_path = mm._container_env_path("demo")
        self.db.rows["demo"]["run_hash"] = mm.run_fingerprint(spec, env_path)
        self.docker.running = True
        # A settings change lands in the module env: the next tick re-derives
        # the container env file, sees a new fingerprint, and recreates.
        self.db.envs.setdefault("demo", {})["EXTRA"] = "1"
        mm.supervise_once()
        self.assertEqual(len(self.docker.runs()), 1)
        row = self.row("demo")
        self.assertEqual(row["state"], ENABLED)
        self.assertEqual(row["run_hash"], mm.run_fingerprint(spec, mm._container_env_path("demo")))

    def test_restarts_stopped_container(self) -> None:
        spec = self._enabled()
        env_path = mm._container_env_path("demo")
        self.db.rows["demo"]["run_hash"] = mm.run_fingerprint(spec, env_path)
        self.docker.running = False  # crashed
        mm.supervise_once()
        self.assertTrue(self.docker.running)
        # A heal is idempotent: the row stays enabled under the same
        # fingerprint (no drift was invented by the revive).
        row = self.row("demo")
        self.assertEqual(row["state"], ENABLED)
        self.assertEqual(row["run_hash"], mm.run_fingerprint(spec, mm._container_env_path("demo")))
        # The start hook ran, because a revive is a bring-up.
        self.assertEqual(
            [e for module_id, e, _ in self.hooks if module_id == "demo"], ["start"]
        )

    def test_marks_broken_after_repeated_probe_failures(self) -> None:
        spec = self._enabled(
            docker={
                "image": "hello_world",
                "container": "lemonseed-demo",
                "ports": ["127.0.0.1:9:80"],
                "probe": {"port": 9},
            }
        )
        env_path = mm._container_env_path("demo")
        self.db.rows["demo"]["run_hash"] = mm.run_fingerprint(spec, env_path)
        self.docker.running = True
        with mock.patch.object(mm, "_probe_ok", lambda spec, timeout=2.0: False):
            for _ in range(mm.HEALTH_FAIL_LIMIT):
                mm.supervise_once()
        self.assert_state("demo", BROKEN)
        self.assertIn("health checks", self.row("demo")["error"])
        # Broken rows are not auto-healed.
        before = len(self.docker.calls)
        mm.supervise_once()
        self.assertEqual(len(self.docker.calls), before)

    def test_skips_while_an_operation_holds_the_lock(self) -> None:
        self._enabled()
        with mm._op_lock:
            before = len(self.docker.calls)
            mm.supervise_once()
            self.assertEqual(len(self.docker.calls), before)

    def test_marks_broken_when_module_left_the_catalog(self) -> None:
        self._enabled()
        self.catalog["modules"].pop("demo")
        mm.supervise_once()
        self.assert_state("demo", BROKEN)
        self.assertIn("no longer in the catalog", self.row("demo")["error"])


class HookTests(unittest.TestCase):
    """The real hook runner: root by default, env + stdin payload, timeout."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.modroot = self.root / "modulesrc"
        (self.modroot / "demo" / "hooks").mkdir(parents=True)
        (self.modroot / "demo" / "data").mkdir()
        self.env = {"POSTGRES_PASSWORD": "pw", "DATABASE_URL": "postgresql://x"}
        self.patches = [
            mock.patch.object(mc, "module_root", lambda: self.modroot),
            mock.patch.object(db_mod(), "_module_credentials", lambda mid: dict(self.env)),
            mock.patch.object(db_mod(), "SECRETS_MODULES", self.root / "secrets"),
            mock.patch.object(mm, "DATA_ROOT", self.root / "modules-data"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _hook(self, body: str) -> Path:
        path = self.modroot / "demo" / "hooks" / "install.sh"
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_missing_hook_is_not_a_failure(self) -> None:
        ok, _detail = mm.run_hook("demo", "install")
        self.assertTrue(ok)

    def test_hook_gets_env_and_stdin_payload(self) -> None:
        self._hook("echo \"$LEMONSEED_MODULE_ID|$LEMONSEED_MODULE_DIR|$POSTGRES_PASSWORD|$(cat)\"")
        ok, detail = mm.run_hook(
            "demo", "install", spec_for("demo"), payload={"event": "install"}
        )
        self.assertTrue(ok, detail)
        self.assertIn(
            f"demo|{mm._module_volume('demo')}|pw|", detail
        )
        self.assertIn('{"event": "install"}', detail)

    def test_hook_nonzero_exit_fails(self) -> None:
        self._hook("echo out; echo err >&2; exit 3")
        ok, detail = mm.run_hook("demo", "install")
        self.assertFalse(ok)
        self.assertIn("exit 3", detail)
        self.assertIn("err", detail)

    def test_hook_timeout_from_spec(self) -> None:
        # The clamp floor is 5s; a script that sleeps past it must be killed.
        spec = spec_for("demo", hooks={"script_timeout": 5})
        self._hook("sleep 20")
        start = time.monotonic()
        ok, detail = mm.run_hook("demo", "install", spec)
        self.assertFalse(ok)
        self.assertIn("timed out", detail)
        self.assertLess(time.monotonic() - start, 15)

    def test_hook_default_timeout_when_undeclared(self) -> None:
        self.assertEqual(mm.hook_timeout(spec_for("demo")), mc.HOOK_TIMEOUT_DEFAULT_SEC)
        self.assertEqual(mm.hook_timeout(None), mc.HOOK_TIMEOUT_DEFAULT_SEC)
        spec = spec_for("demo", hooks={"script_timeout": 900})
        self.assertEqual(mm.hook_timeout(spec), mc.HOOK_TIMEOUT_MAX_SEC)
        spec = spec_for("demo", hooks={"script_timeout": 2})
        self.assertEqual(mm.hook_timeout(spec), mc.HOOK_TIMEOUT_MIN_SEC)

    def test_user_observer_runs_for_every_enabled_module(self) -> None:
        hooks: list[tuple[str, str]] = []

        def recorder(module_id, event, spec=None, payload=None):
            hooks.append((module_id, event))
            return True, ""

        with mock.patch.object(mm, "run_hook", recorder), \
             mock.patch.object(db_mod(), "module_rows", lambda: [
                 {"id": "aa", "state": ENABLED},
                 {"id": "bb", "state": INSTALLED},
                 {"id": "cc", "state": BROKEN},
             ]):
            mm._user_event_observer("user.create", {"event": "user.create", "username": "ada"})
        self.assertEqual(hooks, [("aa", "user")])


class VersionTests(unittest.TestCase):
    def test_version_file_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            version_file = Path(tmp) / "version"
            version_file.write_text("20261112.07\n", encoding="utf-8")
            with mock.patch.object(mm, "VERSION_FILE", version_file):
                self.assertEqual(mm.appliance_version(), "20261112.07")
            version_file.unlink()
            with mock.patch.object(mm, "VERSION_FILE", version_file):
                self.assertEqual(mm.appliance_version(), "unknown")


class OpLockTests(ModuleCase):
    def test_second_operation_refused_while_one_runs(self) -> None:
        self.add_module("demo")
        with mm._op_lock:
            result = mm.request_install("demo")
        self.assertFalse(result["accepted"])
        self.assertIn("in progress", result["reason"])
        # The row was not marked pending by the refused request.
        self.assertNotIn("demo", self.db.rows)


class CatalogViewTests(ModuleCase):
    def test_view_groups_catalog_and_installed(self) -> None:
        self.add_module("demo")
        self.db.rows["demo"] = {"state": ENABLED, "version": "", "settings": {}, "error": None, "run_hash": ""}
        view = mm.catalog_view()
        self.assertEqual([c["id"] for c in view["catalog"]], ["demo"])
        self.assertEqual([c["id"] for c in view["installed"]], ["demo"])
        self.assertEqual(view["catalog"][0]["state"], ENABLED)
        self.assertEqual(view["catalog_version"], 1)

    def test_incompatible_entries_are_filtered_with_a_reason(self) -> None:
        (self.root / "appliance-version").write_text("20261111.00\n", encoding="utf-8")
        self.add_module("demo", compat={"min_lemonseed": "20261112.01"})
        view = mm.catalog_view()
        self.assertEqual(view["catalog"], [])
        self.assertEqual(
            view["incompatible"],
            [{"id": "demo", "reason": mock.ANY}],
        )
        self.assertIn("20261112.01", view["incompatible"][0]["reason"])

    def test_installed_card_without_a_spec_is_honest(self) -> None:
        self.db.rows["demo"] = {"state": ENABLED, "version": "1.0.0", "settings": {}, "error": None, "run_hash": ""}
        view = mm.catalog_view()
        self.assertTrue(view["installed"][0]["spec_missing"])


class SnapshotTests(ModuleCase):
    def test_snapshot_lists_rows_without_docker_calls(self) -> None:
        self.add_module("demo")
        self.db.rows["demo"] = {"state": ENABLED, "version": "1.0.0", "settings": {}, "error": None, "run_hash": ""}
        before = len(self.docker.calls)
        snap = mm.snapshot()
        self.assertEqual(len(self.docker.calls), before)
        self.assertEqual(snap["state"], "ready")
        self.assertEqual(snap["installed"][0]["id"], "demo")
        self.assertEqual(snap["installed"][0]["state"], ENABLED)


if __name__ == "__main__":
    unittest.main()
