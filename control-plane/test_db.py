#!/usr/bin/env python3
"""Unit tests for db.py and data_services.py (mocked; no Postgres required)."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import data_services  # noqa: E402
import db  # noqa: E402
from image_pins import digest_ref, tag_ref  # noqa: E402


class DbProvisioningTests(unittest.TestCase):
    def test_read_env_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write("POSTGRES_USER=postgres\nPOSTGRES_PASSWORD=secret\n")
            path = Path(tmp.name)
        try:
            env = db._read_env_file(path)
            self.assertEqual(env["POSTGRES_USER"], "postgres")
            self.assertEqual(env["POSTGRES_PASSWORD"], "secret")
        finally:
            path.unlink()

    @mock.patch.object(db, "pg_isready", return_value=False)
    @mock.patch.object(db, "postgres_admin_dsn", return_value="postgresql://u:p@127.0.0.1/postgres")
    def test_provision_waiting_for_postgres(self, _dsn: mock.Mock, _ready: mock.Mock) -> None:
        result = db.provision_data_plane()
        self.assertEqual(result["state"], "starting")

    @mock.patch.object(db, "_write_module_secret")
    @mock.patch.object(db, "_connect")
    @mock.patch.object(db, "pg_isready", return_value=True)
    @mock.patch.object(db, "postgres_admin_dsn", return_value="postgresql://u:p@127.0.0.1/postgres")
    def test_provision_ready(
        self,
        _dsn: mock.Mock,
        _ready: mock.Mock,
        connect: mock.Mock,
        _write: mock.Mock,
    ) -> None:
        admin = mock.MagicMock()
        cp = mock.MagicMock()
        connect.side_effect = [admin, cp]

        def cursor_ctx(*_a, **_k):
            ctx = mock.MagicMock()
            ctx.__enter__ = mock.Mock(return_value=ctx)
            ctx.__exit__ = mock.Mock(return_value=False)
            ctx.fetchone.return_value = None
            return ctx

        admin.cursor.side_effect = cursor_ctx
        cp.cursor.side_effect = cursor_ctx

        with mock.patch.object(db, "POSTGRES_ENV", Path("/nonexistent")):
            with mock.patch.object(db, "_module_credentials", return_value={}):
                result = db.provision_data_plane()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["cp_database"], "lemonseed_cp")
        self.assertEqual(len(result["modules"]), 2)


class _FakeCursor:
    """Stand-in for a psycopg2 cursor over the ``cp_meta`` version row.

    Stateful where it matters: the ``schema_version`` SELECT reads ``version``
    and the UPSERT writes it back, so the migration runner can be driven twice
    against one instance and behave like a real database. Doubles as its own
    context manager so ``with conn.cursor() as cur`` works unchanged.
    """

    def __init__(self, version: Optional[int] = None) -> None:
        self.version = version
        self.statements: list[str] = []
        self._probe_pending = False

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql)
        self._probe_pending = "FROM cp_meta WHERE key = 'schema_version'" in sql
        if "INSERT INTO cp_meta" in sql and "schema_version" in sql:
            self.version = json.loads(params[0])  # type: ignore[index]

    def fetchone(self):
        if self._probe_pending:
            return None if self.version is None else (self.version,)
        return None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def ddl(self) -> list[str]:
        return [
            s.strip()
            for s in self.statements
            if s.strip().upper().startswith(("ALTER ", "CREATE "))
        ]


class SchemaMigrationTests(unittest.TestCase):
    """``SCHEMA_VERSION`` must mean something on an already-deployed box.

    M6.3b shipped ``schema_version`` as a metadata row only, with every statement
    a ``CREATE TABLE IF NOT EXISTS`` and no ``ALTER TABLE`` anywhere: a new column
    would have been silently absent in the field while tests passed. These assert
    the runner actually executes DDL, exactly once, in order.
    """

    def test_fresh_database_reaches_current_version(self) -> None:
        cur = _FakeCursor(None)
        self.assertEqual(db.migrate_cp_schema(cur), db.SCHEMA_VERSION)
        self.assertEqual(cur.version, db.SCHEMA_VERSION)
        joined = " ".join(cur.ddl())
        self.assertIn("token_generation", joined)
        self.assertIn("last_login", joined)

    def test_database_at_current_version_runs_no_ddl(self) -> None:
        cur = _FakeCursor(db.SCHEMA_VERSION)
        self.assertEqual(db.migrate_cp_schema(cur), db.SCHEMA_VERSION)
        self.assertEqual(cur.ddl(), [])

    def test_upgrade_from_baseline_adds_only_the_new_columns(self) -> None:
        cur = _FakeCursor(db.BASELINE_SCHEMA_VERSION)
        self.assertEqual(db.migrate_cp_schema(cur), db.SCHEMA_VERSION)
        self.assertEqual(
            cur.ddl(),
            [
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS token_generation"
                " INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_login TIMESTAMPTZ",
            ],
        )

    def test_each_migration_runs_once_even_if_applied_twice(self) -> None:
        cur = _FakeCursor(None)
        db.migrate_cp_schema(cur)
        db.migrate_cp_schema(cur)
        alters = [s for s in cur.ddl() if s.startswith("ALTER TABLE")]
        self.assertEqual(len(alters), 2)  # both columns, not each twice

    def test_a_newer_database_than_the_control_plane_is_tolerated(self) -> None:
        """A/B rollback must not brick the appliance."""
        ahead = db.SCHEMA_VERSION + 1
        cur = _FakeCursor(ahead)
        self.assertEqual(db.migrate_cp_schema(cur), ahead)
        self.assertEqual(cur.ddl(), [])
        self.assertEqual(cur.version, ahead)

    def test_migrations_cover_the_declared_version(self) -> None:
        versions = [m.version for m in db.MIGRATIONS]
        self.assertEqual(versions, sorted(set(versions)))
        self.assertEqual(
            versions,
            list(range(db.BASELINE_SCHEMA_VERSION + 1, db.SCHEMA_VERSION + 1)),
        )

    def test_only_additive_migration_sql(self) -> None:
        """Additive-only DDL, so an older slot can still read a newer database."""
        banned = ("DROP", "RENAME", "ALTER COLUMN", "TRUNCATE", "DELETE", "UPDATE ")
        for migration in db.MIGRATIONS:
            cur = _FakeCursor(None)
            migration.apply(cur)
            statements = cur.ddl()
            self.assertTrue(statements, f"v{migration.version} produced no DDL")
            for sql in statements:
                head = sql.upper()
                # Every step must be re-runnable, because the version stamp is
                # written after the DDL and a crash in between is expected.
                if head.startswith("ALTER TABLE"):
                    self.assertIn("ADD COLUMN IF NOT EXISTS", head, sql)
                else:
                    self.assertIn("IF NOT EXISTS", head, sql)
                for word in banned:
                    self.assertNotIn(word, head, sql)

    @mock.patch.object(db, "_create_cp_tables")
    def test_apply_cp_schema_reports_migrated_version(self, create: mock.Mock) -> None:
        cur = _FakeCursor(db.BASELINE_SCHEMA_VERSION)
        conn = mock.MagicMock()
        conn.cursor.return_value = cur
        self.assertEqual(db._apply_cp_schema(conn), db.SCHEMA_VERSION)
        create.assert_called_once_with(conn)


class OwuiConfigResetTests(unittest.TestCase):
    """``truncate_owui_config`` is the only place the destructive SQL lives."""

    @staticmethod
    def _connection(ctx: mock.MagicMock) -> mock.MagicMock:
        conn = mock.MagicMock()
        # One cursor object, so the recorded execute() calls are inspectable.
        conn.cursor.side_effect = lambda *_a, **_k: ctx
        return conn

    @mock.patch.object(db, "pg_isready", return_value=True)
    @mock.patch.object(db, "postgres_admin_dsn",
                       return_value="postgresql://u:p@127.0.0.1/postgres")
    @mock.patch.object(db, "_connect")
    def test_truncates_only_the_config_table_in_the_module_db(
        self, connect: mock.Mock, _dsn: mock.Mock, _ready: mock.Mock
    ) -> None:
        ctx = mock.MagicMock()
        ctx.__enter__ = mock.Mock(return_value=ctx)
        ctx.__exit__ = mock.Mock(return_value=False)
        conn = self._connection(ctx)
        connect.return_value = conn

        result = db.truncate_owui_config()

        self.assertEqual(result["state"], "ready")
        self.assertIsNone(result["error"])
        connect.assert_called_once_with("open_webui")
        statements = [c.args[0] for c in ctx.execute.call_args_list]
        self.assertEqual(statements, ["TRUNCATE TABLE config"])
        conn.close.assert_called_once()

    @mock.patch.object(db, "pg_isready", return_value=True)
    @mock.patch.object(db, "postgres_admin_dsn",
                       return_value="postgresql://u:p@127.0.0.1/postgres")
    @mock.patch.object(db, "_connect", side_effect=RuntimeError("auth failed"))
    def test_failure_is_reported_not_raised(
        self, _connect: mock.Mock, _dsn: mock.Mock, _ready: mock.Mock
    ) -> None:
        result = db.truncate_owui_config()
        self.assertEqual(result["state"], "error")
        self.assertIn("auth failed", result["error"])

    @mock.patch.object(db, "pg_isready", return_value=False)
    @mock.patch.object(db, "postgres_admin_dsn", return_value="postgresql://x")
    @mock.patch.object(db, "_connect")
    def test_waits_for_postgres(
        self, connect: mock.Mock, _dsn: mock.Mock, _ready: mock.Mock
    ) -> None:
        result = db.truncate_owui_config()
        self.assertEqual(result["state"], "error")
        connect.assert_not_called()


class DataServicesTests(unittest.TestCase):
    @mock.patch.object(data_services, "valkey_ready", return_value=True)
    @mock.patch.object(data_services, "postgres_snapshot", return_value={"state": "ready"})
    def test_data_snapshot(self, _pg: mock.Mock, _vk: mock.Mock) -> None:
        snap = data_services.data_snapshot()
        self.assertEqual(snap["postgres"]["state"], "ready")
        self.assertEqual(snap["valkey"]["state"], "ready")

    @mock.patch.object(data_services, "_sync_json_state_to_db")
    @mock.patch.object(data_services, "provision_data_plane", return_value={"state": "ready"})
    @mock.patch.object(data_services, "pg_isready", return_value=True)
    def test_ensure_provisioned_syncs_state(
        self,
        _ready: mock.Mock,
        _prov: mock.Mock,
        sync: mock.Mock,
    ) -> None:
        data_services._provision_state = None
        result = data_services.ensure_provisioned()
        self.assertEqual(result["state"], "ready")
        sync.assert_called_once()


class DataSuperviseTests(unittest.TestCase):
    def setUp(self) -> None:
        data_services._containers = {
            data_services.POSTGRES_CONTAINER: {"state": "starting"},
            data_services.VALKEY_CONTAINER: {"state": "starting"},
        }
        data_services._net_check_at = 0.0
        data_services._net_ok = False

    def tearDown(self) -> None:
        data_services._net_check_at = 0.0
        data_services._net_ok = False

    @mock.patch.object(data_services, "_port_open", return_value=True)
    @mock.patch.object(data_services, "_container_running", return_value=True)
    def test_ready_container_untouched(self, _running: mock.Mock, _port: mock.Mock) -> None:
        with mock.patch.object(data_services, "_docker") as m_docker:
            data_services._supervise_one(
                data_services.POSTGRES_CONTAINER,
                data_services.POSTGRES_IMAGE_KEY,
                "postgres",
                5432,
                lambda _ref: ["run"],
            )
        m_docker.assert_not_called()
        self.assertEqual(
            data_services._containers[data_services.POSTGRES_CONTAINER]["state"], "ready"
        )

    @mock.patch.object(data_services, "_port_open", return_value=False)
    @mock.patch.object(data_services, "_container_running", return_value=True)
    def test_running_within_grace_not_recreated(self, _running: mock.Mock, _port: mock.Mock) -> None:
        data_services._containers[data_services.POSTGRES_CONTAINER]["since"] = (
            time.monotonic()
        )
        with mock.patch.object(data_services, "_docker") as m_docker:
            data_services._supervise_one(
                data_services.POSTGRES_CONTAINER,
                data_services.POSTGRES_IMAGE_KEY,
                "postgres",
                5432,
                lambda _ref: ["run"],
            )
        m_docker.assert_not_called()
        self.assertEqual(
            data_services._containers[data_services.POSTGRES_CONTAINER]["state"], "starting"
        )

    @mock.patch.object(data_services, "_port_open", return_value=False)
    @mock.patch.object(data_services, "_container_running", return_value=True)
    def test_running_past_grace_recreated(self, _running: mock.Mock, _port: mock.Mock) -> None:
        data_services._containers[data_services.POSTGRES_CONTAINER]["since"] = (
            time.monotonic() - data_services.HEALTH_GRACE_SEC - 1
        )
        with mock.patch.object(data_services, "_start_container") as m_start:
            with mock.patch.object(data_services, "_docker") as m_docker:
                data_services._supervise_one(
                    data_services.POSTGRES_CONTAINER,
                    data_services.POSTGRES_IMAGE_KEY,
                    "postgres",
                    5432,
                    lambda _ref: ["run"],
                )
        # rm -f for the recreate, then the start path re-runs it.
        self.assertTrue(
            any(
                call.args and call.args[0] == ["rm", "-f", data_services.POSTGRES_CONTAINER]
                for call in m_docker.call_args_list
            )
        )
        m_start.assert_called_once()

    @mock.patch.object(data_services, "_port_open", return_value=False)
    @mock.patch.object(data_services, "_container_running", return_value=False)
    @mock.patch.object(data_services, "_container_exists", return_value=False)
    def test_missing_container_starts(self, _exists: mock.Mock, _running: mock.Mock, _port: mock.Mock) -> None:
        with mock.patch.object(data_services, "_start_container") as m_start:
            data_services._supervise_one(
                data_services.POSTGRES_CONTAINER,
                data_services.POSTGRES_IMAGE_KEY,
                "postgres",
                5432,
                lambda _ref: ["run"],
            )
        m_start.assert_called_once()
        self.assertEqual(
            data_services._containers[data_services.POSTGRES_CONTAINER]["state"], "starting"
        )

    @mock.patch.object(data_services, "_start_container")
    @mock.patch.object(data_services, "_container_exists", return_value=True)
    @mock.patch.object(data_services, "_container_running", return_value=False)
    def test_stopped_container_restarted(self, _running: mock.Mock, _exists: mock.Mock, _start: mock.Mock) -> None:
        with mock.patch.object(data_services, "_docker") as m_docker:
            m_docker.return_value = mock.Mock(returncode=0)
            data_services._supervise_one(
                data_services.POSTGRES_CONTAINER,
                data_services.POSTGRES_IMAGE_KEY,
                "postgres",
                5432,
                lambda _ref: ["run"],
            )
        self.assertTrue(
            any(
                call.args and call.args[0] == ["start", data_services.POSTGRES_CONTAINER]
                for call in m_docker.call_args_list
            )
        )

    @mock.patch.object(data_services, "_pull_image", return_value=(False, "Cannot reach the container registry"))
    @mock.patch.object(data_services, "_ensure_lemon_network", return_value=True)
    @mock.patch.object(data_services, "_container_exists", return_value=False)
    @mock.patch.object(data_services, "_container_running", return_value=False)
    def test_start_records_waiting_when_pull_fails(
        self,
        _running: mock.Mock,
        _exists: mock.Mock,
        _net: mock.Mock,
        _pull: mock.Mock,
    ) -> None:
        with mock.patch.object(data_services, "_docker") as m_docker:
            m_docker.return_value = mock.Mock(returncode=0)
            data_services._start_container(
                data_services.POSTGRES_CONTAINER,
                data_services.POSTGRES_IMAGE_KEY,
                "postgres",
                lambda _ref: ["run"],
            )
        state = data_services._containers[data_services.POSTGRES_CONTAINER]
        self.assertEqual(state["state"], "waiting")
        self.assertIn("registry", state["error"])

    @mock.patch.object(data_services, "_image_present", return_value=False)
    @mock.patch.object(data_services, "_network_reachable", return_value=False)
    def test_pull_reports_registry_unreachable(self, _net: mock.Mock, _present: mock.Mock) -> None:
        probe = mock.Mock(returncode=1, stdout="", stderr="Error: no such host")
        with mock.patch.object(data_services, "_docker", return_value=probe) as m_docker:
            ok, detail = data_services._pull_image(data_services.POSTGRES_IMAGE_KEY, "postgres")
        self.assertFalse(ok)
        self.assertIn("registry", detail)
        m_docker.assert_called_once()  # one diagnostic probe, no real pull

    @mock.patch.object(data_services, "_image_present", return_value=True)
    def test_pull_skips_docker_when_image_present(self, _present: mock.Mock) -> None:
        with mock.patch.object(data_services, "_docker") as m_docker:
            ok, detail = data_services._pull_image(data_services.POSTGRES_IMAGE_KEY, "postgres")
        self.assertTrue(ok)
        self.assertEqual(detail, "present")
        m_docker.assert_not_called()

    # -- M6.6 S5.1: digest is what we run; the tag is display + fallback -----

    def test_run_ref_prefers_the_pinned_digest(self) -> None:
        with mock.patch.object(data_services, "_image_present", return_value=True):
            self.assertEqual(
                data_services._resolve_ref(data_services.POSTGRES_IMAGE_KEY),
                digest_ref(data_services.POSTGRES_IMAGE_KEY),
            )

    def test_run_ref_falls_back_to_tag_when_digest_absent(self) -> None:
        """QEMU's pullImage tarballs carry RepoTags but no RepoDigests."""

        def present(ref: str) -> bool:
            return "@" not in ref

        with mock.patch.object(data_services, "_image_present", side_effect=present):
            self.assertEqual(
                data_services._resolve_ref(data_services.POSTGRES_IMAGE_KEY),
                tag_ref(data_services.POSTGRES_IMAGE_KEY),
            )

    def test_run_ref_is_digest_when_nothing_is_local(self) -> None:
        with mock.patch.object(data_services, "_image_present", return_value=False):
            self.assertEqual(
                data_services._resolve_ref(data_services.VALKEY_IMAGE_KEY),
                digest_ref(data_services.VALKEY_IMAGE_KEY),
            )

    @mock.patch.object(data_services, "_image_present", return_value=True)
    def test_run_args_end_with_the_resolved_ref(self, _present: mock.Mock) -> None:
        ref = digest_ref(data_services.POSTGRES_IMAGE_KEY)
        self.assertEqual(data_services._postgres_run_args(ref)[-1], ref)
        ref = digest_ref(data_services.VALKEY_IMAGE_KEY)
        self.assertIn(ref, data_services._valkey_run_args(ref))

    def test_tag_only_local_image_is_not_repulled(self) -> None:
        def present(ref: str) -> bool:
            return "@" not in ref

        with (
            mock.patch.object(data_services, "_image_present", side_effect=present),
            mock.patch.object(data_services, "_docker") as m_docker,
        ):
            ok, detail = data_services._pull_image(data_services.POSTGRES_IMAGE_KEY, "postgres")
        self.assertTrue(ok)
        self.assertEqual(detail, "present:tag")
        m_docker.assert_not_called()

    def test_pull_falls_back_to_tag_when_digest_unavailable(self) -> None:
        calls: list[list[str]] = []

        def fake_docker(args: list[str], **_kw: object) -> mock.Mock:
            calls.append(args)
            ok = "@" not in args[-1]
            return mock.Mock(returncode=0 if ok else 1, stdout="", stderr="no such manifest")

        with (
            mock.patch.object(data_services, "_image_present", return_value=False),
            mock.patch.object(data_services, "_network_reachable", return_value=True),
            mock.patch.object(data_services, "_docker", side_effect=fake_docker),
        ):
            ok, detail = data_services._pull_image(data_services.POSTGRES_IMAGE_KEY, "postgres")
        self.assertTrue(ok)
        self.assertEqual(detail, "pulled:tag_fallback")
        self.assertIn("@sha256:", calls[0][-1])
        self.assertNotIn("@", calls[1][-1])

    def test_start_container_flags_image_drift_on_tag_fallback(self) -> None:
        data_services._containers = {data_services.VALKEY_CONTAINER: {"state": "starting"}}
        with (
            mock.patch.object(data_services, "_container_exists", return_value=False),
            mock.patch.object(data_services, "_ensure_lemon_network", return_value=True),
            mock.patch.object(data_services, "_pull_image", return_value=(True, "pulled:tag_fallback")),
            mock.patch.object(data_services, "_resolve_ref", return_value="valkey/valkey:8-alpine"),
            mock.patch.object(data_services, "_docker") as m_docker,
        ):
            m_docker.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            data_services._start_container(
                data_services.VALKEY_CONTAINER,
                data_services.VALKEY_IMAGE_KEY,
                "valkey",
                lambda ref: ["run", ref],
            )
        state = data_services._containers[data_services.VALKEY_CONTAINER]
        self.assertTrue(state["image_drift"])
        self.assertEqual(state["image"], "valkey/valkey:8-alpine")

    def test_pull_budget_is_per_image(self) -> None:
        """A blanket 600s cap was the slow-link hazard; budget by transfer size."""
        self.assertEqual(data_services.PULL_TIMEOUT_SEC["valkey"], 300)
        self.assertEqual(data_services.PULL_TIMEOUT_SEC["postgres"], 900)
        result = mock.Mock(returncode=0, stdout="", stderr="")
        for key, budget in data_services.PULL_TIMEOUT_SEC.items():
            with (
                mock.patch.object(data_services, "_image_present", return_value=False),
                mock.patch.object(data_services, "_network_reachable", return_value=True),
                mock.patch.object(data_services, "_docker", return_value=result) as m_docker,
            ):
                data_services._pull_image(key, key)
            self.assertEqual(m_docker.call_args.kwargs["timeout"], budget)

    @mock.patch.object(data_services, "_supervise_one")
    def test_supervise_once_covers_both_containers(self, m_one: mock.Mock) -> None:
        data_services.supervise_data_once()
        names = [c.args[0] for c in m_one.call_args_list]
        self.assertEqual(
            names,
            [data_services.POSTGRES_CONTAINER, data_services.VALKEY_CONTAINER],
        )

    @mock.patch.object(data_services, "_port_open", return_value=True)
    @mock.patch.object(data_services, "pg_isready", return_value=True)
    def test_snapshot_ready_state(self, _ready: mock.Mock, _port: mock.Mock) -> None:
        data_services._containers[data_services.POSTGRES_CONTAINER] = {
            "state": "ready",
        }
        snap = data_services.postgres_snapshot()
        self.assertEqual(snap["state"], "ready")
        self.assertEqual(snap["container"], data_services.POSTGRES_CONTAINER)

    @mock.patch.object(data_services, "_port_open", return_value=False)
    @mock.patch.object(data_services, "pg_isready", return_value=False)
    def test_snapshot_keeps_error_while_not_ready(self, _ready: mock.Mock, _port: mock.Mock) -> None:
        data_services._containers[data_services.POSTGRES_CONTAINER] = {
            "state": "waiting",
            "error": "Cannot reach the container registry",
        }
        snap = data_services.postgres_snapshot()
        self.assertEqual(snap["state"], "waiting")
        self.assertEqual(snap["error"], "Cannot reach the container registry")

    @mock.patch.object(data_services, "_port_open", return_value=True)
    @mock.patch.object(data_services, "pg_isready", return_value=True)
    def test_data_snapshot_shape(self, _ready: mock.Mock, _port: mock.Mock) -> None:
        snap = data_services.data_snapshot()
        self.assertEqual(set(snap), {"postgres", "valkey", "provisioning"})
        self.assertEqual(snap["postgres"]["state"], "ready")
        self.assertEqual(snap["valkey"]["state"], "ready")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
