#!/usr/bin/env python3
"""Unit tests for auth.py (mocked; no Postgres/Valkey/Authelia required)."""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import auth  # noqa: E402
import db  # noqa: E402
import data_services  # noqa: E402


def _conn_with_cur():
    conn = mock.MagicMock()
    cur = mock.MagicMock()
    cur_ctx = mock.MagicMock()
    cur_ctx.__enter__ = mock.Mock(return_value=cur)
    cur_ctx.__exit__ = mock.Mock(return_value=False)
    conn.cursor.return_value = cur_ctx
    return conn, cur


class AuthFileRenderTests(unittest.TestCase):
    def test_render_users_yaml_empty(self) -> None:
        self.assertEqual(auth.render_users_yaml([]), "users: {}\n")

    def test_render_users_yaml_map_format(self) -> None:
        yaml_text = auth.render_users_yaml(
            [
                {
                    "username": "matt",
                    "display_name": "Matt O\"Brien\\Jr",
                    "email": "matt@example.com",
                    "password_hash": "$2y$12$abcdefgh",
                    "disabled": False,
                    "groups": ["admin", "family"],
                },
                {
                    "username": "jill",
                    "display_name": "",
                    "email": "",
                    "password_hash": "$2y$12$ijklmnop",
                    "disabled": True,
                    "groups": ["family"],
                },
            ]
        )
        # 4.x format: a `users` map keyed by username (many:many groups).
        self.assertTrue(yaml_text.startswith("users:\n"))
        self.assertIn('  "matt":', yaml_text)
        self.assertIn('    displayname: "Matt O\\"Brien\\\\Jr"', yaml_text)
        self.assertIn('    email: "matt@example.com"', yaml_text)
        self.assertIn('    password: "$2y$12$abcdefgh"', yaml_text)
        self.assertIn("    disabled: false", yaml_text)
        self.assertIn("    groups:", yaml_text)
        self.assertIn('      - "admin"', yaml_text)
        self.assertIn('      - "family"', yaml_text)
        # displayname falls back to the username; empty email omitted.
        self.assertIn('  "jill":', yaml_text)
        self.assertIn('    displayname: "jill"', yaml_text)
        jill_block = yaml_text.split('"jill":', 1)[1]
        self.assertNotIn("email:", jill_block)
        self.assertIn("    disabled: true", jill_block)
        # No legacy 2.x list style.
        self.assertNotIn("- username:", yaml_text)

    def test_render_config_keys(self) -> None:
        config = auth.render_config("session-secret-1", "storage-key-2", "test.local")
        self.assertIn(f"address: 'tcp://{auth.AUTH_HOST}:{auth.AUTH_PORT}{auth.AUTH_PATH}'", config)
        self.assertIn("    watch: true", config)
        self.assertIn("  encryption_key: storage-key-2", config)
        self.assertIn("  secret: session-secret-1", config)
        self.assertIn(f"    database_index: {auth.VALKEY_SESSION_DB}", config)
        self.assertIn("  default_policy: one_factor", config)
        self.assertIn("    disable: true", config)
        self.assertIn("  disable_startup_check: true", config)
        self.assertIn("    domain: test.local", config)
        self.assertIn("    authelia_url: https://test.local/auth/", config)
        # Default redirect target so Authelia does not reject rd=%2F as unsafe.
        self.assertIn("    default_redirection_url: https://test.local/", config)
        # 4.x auth-request endpoint implementation (official nginx guide).
        self.assertIn('        implementation: "AuthRequest"', config)


class AuthGateSnippetTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(auth, "GATE_CONF", root / "auth-gate.conf"),
            mock.patch.object(auth, "ADMIN_CONF", root / "auth-admin.conf"),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def test_write_gate_on_and_off(self) -> None:
        auth.write_gate(True)
        self.assertIn("auth_request /_lemonseed/auth-verify;", auth.GATE_CONF.read_text())
        self.assertIn("error_page 401 = @auth_unauthorized;", auth.GATE_CONF.read_text())
        self.assertIn("$upstream_http_remote_user", auth.GATE_CONF.read_text())
        self.assertIn("$upstream_http_remote_groups", auth.GATE_CONF.read_text())
        # Admin group check is enforced by the CP (X-Forwarded-Groups), not
        # nginx `if` (which runs in the rewrite phase, before auth_request).
        self.assertEqual(auth.ADMIN_CONF.read_text(), "")

        auth.write_gate(False)
        self.assertEqual(auth.GATE_CONF.read_text(), "")
        self.assertEqual(auth.ADMIN_CONF.read_text(), "")


class AuthAdminStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.marker = root / "reset-admin"
        self._patches = [
            mock.patch.object(auth, "RESET_MARKER_FILE", self.marker),
            mock.patch.object(db, "postgres_admin_dsn", return_value="postgresql://u:p@127.0.0.1/postgres"),
            mock.patch.object(db, "pg_isready", return_value=True),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def test_admin_exists_pg_down_is_none(self) -> None:
        with mock.patch.object(db, "pg_isready", return_value=False):
            self.assertIsNone(auth.admin_exists())

    def test_admin_exists_true_false(self) -> None:
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = (1,)
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertIs(auth.admin_exists(), True)
        cur.fetchone.return_value = None
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertIs(auth.admin_exists(), False)

    def test_admin_exists_query_error_is_none(self) -> None:
        with mock.patch.object(db, "cp_connect", side_effect=RuntimeError("down")):
            self.assertIsNone(auth.admin_exists())

    def test_bootstrap_required_matrix(self) -> None:
        with mock.patch.object(auth, "admin_exists", return_value=True):
            self.assertFalse(auth.admin_bootstrap_required())
        with mock.patch.object(auth, "admin_exists", return_value=False):
            self.assertTrue(auth.admin_bootstrap_required())
        with mock.patch.object(auth, "admin_exists", return_value=None):
            self.assertTrue(auth.admin_bootstrap_required())
        # Pending reset marker always requires the setup form.
        self.marker.write_text("1\n")
        with mock.patch.object(auth, "admin_exists", return_value=True):
            self.assertTrue(auth.admin_bootstrap_required())


class AuthConfigFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(auth, "AUTH_STATE_DIR", root),
            mock.patch.object(auth, "USERS_YAML", root / "users.yaml"),
            mock.patch.object(auth, "CONFIG_YAML", root / "config.yml"),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def test_ensure_config_writes_once(self) -> None:
        self.assertTrue(auth.ensure_authelia_config("test.local"))
        first = auth.CONFIG_YAML.read_text()
        self.assertIn("encryption_key:", first)
        self.assertIn("secret:", first)
        # A second call must not rewrite (the secret must survive).
        self.assertTrue(auth.ensure_authelia_config("test.local"))
        self.assertEqual(auth.CONFIG_YAML.read_text(), first)

    def test_ensure_config_empty_file_rewrites(self) -> None:
        auth.CONFIG_YAML.write_text("")
        self.assertTrue(auth.ensure_authelia_config("test.local"))
        self.assertGreater(auth.CONFIG_YAML.stat().st_size, 0)


class AuthSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(auth, "AUTH_STATE_DIR", root),
            mock.patch.object(auth, "USERS_YAML", root / "users.yaml"),
            mock.patch.object(auth, "CONFIG_YAML", root / "config.yml"),
            mock.patch.object(auth, "RESET_MARKER_FILE", root / "reset-admin"),
            mock.patch.object(db, "postgres_admin_dsn", return_value="postgresql://u:p@127.0.0.1/postgres"),
            mock.patch.object(db, "pg_isready", return_value=True),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def test_sync_users_yaml_not_ready(self) -> None:
        with mock.patch.object(db, "pg_isready", return_value=False):
            self.assertFalse(auth.sync_users_yaml())

    def test_sync_users_yaml_derives(self) -> None:
        conn, cur = _conn_with_cur()
        cur.fetchall.side_effect = [
            [("matt", "Matt", "", "$2y$12$hash", False)],
            [("matt", "admin"), ("matt", "users")],
        ]
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertTrue(auth.sync_users_yaml())
        text = auth.USERS_YAML.read_text()
        self.assertIn('  "matt":', text)
        self.assertIn('      - "admin"', text)
        self.assertIn('      - "users"', text)


class AuthStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.gate = root / "auth-gate.conf"
        self.marker = root / "reset-admin"
        self._patches = [
            mock.patch.object(auth, "GATE_CONF", self.gate),
            mock.patch.object(auth, "RESET_MARKER_FILE", self.marker),
            mock.patch.object(db, "postgres_admin_dsn", return_value=None),
            mock.patch.object(auth, "_authelia_health", return_value="down"),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def test_status_disabled_and_pg_down(self) -> None:
        status = auth.auth_status()
        self.assertEqual(status["state"], "disabled")
        self.assertEqual(status["service"], "down")
        self.assertIsNone(status["users"])
        self.assertIsNone(status["admins"])
        self.assertFalse(status["reset_pending"])

    def test_status_enabled_with_counts(self) -> None:
        self.gate.write_text(auth.GATE_SNIPPET)
        self.marker.write_text("1\n")
        with mock.patch.object(auth, "account_counts", return_value=(3, 1)):
            status = auth.auth_status()
        self.assertEqual(status["state"], "enabled")
        self.assertEqual(status["users"], 3)
        self.assertEqual(status["admins"], 1)
        self.assertTrue(status["reset_pending"])


class AuthCreateAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.gate = root / "auth-gate.conf"
        self.admin_conf = root / "auth-admin.conf"
        self.marker = root / "reset-admin"
        self._patches = [
            mock.patch.object(auth, "AUTH_STATE_DIR", root),
            mock.patch.object(auth, "USERS_YAML", root / "users.yaml"),
            mock.patch.object(auth, "CONFIG_YAML", root / "config.yml"),
            mock.patch.object(auth, "GATE_CONF", self.gate),
            mock.patch.object(auth, "ADMIN_CONF", self.admin_conf),
            mock.patch.object(auth, "RESET_MARKER_FILE", self.marker),
            mock.patch.object(auth, "hash_password", return_value="$2y$12$testhash"),
            mock.patch.object(auth, "sync_users_yaml", return_value=True),
            mock.patch.object(auth, "ensure_authelia_config", return_value=True),
            mock.patch.object(auth, "reload_nginx"),
            mock.patch.object(auth, "PG_WAIT_SECONDS", 0),
            mock.patch.object(db, "postgres_admin_dsn", return_value="postgresql://u:p@127.0.0.1/postgres"),
            mock.patch.object(db, "pg_isready", return_value=True),
            mock.patch.object(data_services, "ensure_provisioned", return_value={"state": "ready"}),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()

    def test_rejects_invalid_username(self) -> None:
        for bad in ("", "Matt", "1bad", "a" * 65, "has space"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                auth.create_admin(bad, "longenough")

    def test_rejects_short_password(self) -> None:
        with self.assertRaises(ValueError):
            auth.create_admin("admin", "short")

    def test_pg_not_ready(self) -> None:
        with mock.patch.object(db, "pg_isready", return_value=False):
            with self.assertRaises(ValueError):
                auth.create_admin("admin", "longenough")

    def test_data_services_not_ready(self) -> None:
        with mock.patch.object(
            data_services, "ensure_provisioned", return_value={"state": "starting"}
        ):
            with self.assertRaises(ValueError):
                auth.create_admin("admin", "longenough")

    def test_db_error_wrapped(self) -> None:
        with mock.patch.object(db, "cp_connect", side_effect=RuntimeError("boom")):
            with self.assertRaises(ValueError):
                auth.create_admin("admin", "longenough")

    def test_happy_path_first_admin(self) -> None:
        conn, cur = _conn_with_cur()
        with mock.patch.object(db, "cp_connect", return_value=conn):
            result = auth.create_admin("admin", "longenough")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["reset"], False)
        # User upsert + both default groups + both mappings (5 statements).
        sqls = [call.args[0] for call in cur.execute.call_args_list]
        self.assertEqual(len(sqls), 5)
        self.assertFalse(any("DELETE FROM user_groups" in sql for sql in sqls))
        self.assertIn("INSERT INTO users", sqls[0])
        self.assertIn("INSERT INTO groups", sqls[1])
        self.assertIn("INSERT INTO groups", sqls[2])
        self.assertIn("INSERT INTO user_groups", sqls[3])
        self.assertIn("INSERT INTO user_groups", sqls[4])
        self.assertIn("auth_request", self.gate.read_text())
        # Admin group check moved to CP (X-Forwarded-Groups); admin conf is empty.
        self.assertEqual(self.admin_conf.read_text(), "")
        conn.close.assert_called_once()

    def test_reset_path_demotes_admins_and_consumes_marker(self) -> None:
        self.marker.write_text("1\n")
        conn, cur = _conn_with_cur()
        with mock.patch.object(db, "cp_connect", return_value=conn):
            result = auth.create_admin("admin", "longenough")
        self.assertEqual(result["reset"], True)
        sqls = [call.args[0] for call in cur.execute.call_args_list]
        self.assertIn("DELETE FROM user_groups", sqls[0])
        self.assertFalse(self.marker.exists())


class AuthGateWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(auth, "AUTH_STATE_DIR", root),
            mock.patch.object(auth, "USERS_YAML", root / "users.yaml"),
            mock.patch.object(auth, "CONFIG_YAML", root / "config.yml"),
            mock.patch.object(auth, "GATE_CONF", root / "auth-gate.conf"),
            mock.patch.object(auth, "ADMIN_CONF", root / "auth-admin.conf"),
            mock.patch.object(auth, "RESET_MARKER_FILE", root / "reset-admin"),
            mock.patch.object(auth, "sync_users_yaml", return_value=True),
            mock.patch.object(auth, "ensure_authelia_config", return_value=True),
            mock.patch.object(auth, "reload_nginx"),
        ]
        for patch in self._patches:
            patch.start()
        auth._gate_synced = False

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()
        self._tmp.cleanup()
        auth._gate_synced = False

    def _wait_gate(self, expect_enabled: bool) -> bool:
        for _ in range(100):
            text = auth.GATE_CONF.read_text() if auth.GATE_CONF.is_file() else None
            if text is not None and bool(text.strip()) == expect_enabled:
                return True
            time.sleep(0.05)
        return False

    def test_worker_enables_gate_when_admin_exists(self) -> None:
        with mock.patch.object(auth, "admin_exists", return_value=True):
            auth.ensure_auth_gate_worker()
            self.assertTrue(self._wait_gate(True))

    def test_worker_keeps_gate_off_when_no_admin(self) -> None:
        auth.GATE_CONF.write_text(auth.GATE_SNIPPET)  # stale gate from before reboot
        with mock.patch.object(auth, "admin_exists", return_value=False):
            auth.ensure_auth_gate_worker()
            self.assertTrue(self._wait_gate(False))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
