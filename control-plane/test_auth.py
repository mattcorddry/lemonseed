#!/usr/bin/env python3
"""Unit tests for auth.py (mocked; no Postgres required)."""

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


class SessionSecretTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.secret_file = Path(self._tmp.name) / "session-secret"
        self._patch = mock.patch.object(auth, "SESSION_SECRET_FILE", self.secret_file)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_ensure_generates_32_bytes_0600(self) -> None:
        secret = auth.ensure_session_secret()
        self.assertEqual(len(secret), 32)
        self.assertTrue(self.secret_file.is_file())
        self.assertEqual(self.secret_file.stat().st_mode & 0o777, 0o600)

    def test_ensure_returns_existing(self) -> None:
        first = auth.ensure_session_secret()
        second = auth.ensure_session_secret()
        self.assertEqual(first, second)

    def test_rotate_changes_secret(self) -> None:
        first = auth.ensure_session_secret()
        auth.rotate_session_secret()
        second = auth.ensure_session_secret()
        self.assertNotEqual(first, second)


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

    def test_role_maps_admin_group(self) -> None:
        self.assertEqual(auth.role(["admin", "users"]), "admin")
        self.assertEqual(auth.role(["users"]), "user")
        self.assertEqual(auth.role([]), "user")
        self.assertEqual(auth.role([" admin "]), "admin")

    def test_write_gate_on_and_off(self) -> None:
        auth.write_gate(True)
        self.assertIn("auth_request /_lemonseed/session-verify;", auth.GATE_CONF.read_text())
        self.assertIn("error_page 401 = @auth_unauthorized;", auth.GATE_CONF.read_text())
        self.assertIn("$upstream_http_x_forwarded_user", auth.GATE_CONF.read_text())
        self.assertIn("$upstream_http_x_forwarded_groups", auth.GATE_CONF.read_text())
        self.assertIn("$upstream_http_x_forwarded_name", auth.GATE_CONF.read_text())
        self.assertIn("$upstream_http_x_forwarded_role", auth.GATE_CONF.read_text())
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


class TokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.secret_file = Path(self._tmp.name) / "session-secret"
        self._patch = mock.patch.object(auth, "SESSION_SECRET_FILE", self.secret_file)
        self._patch.start()
        self.secret = b"0123456789abcdef0123456789abcdef"  # 32 bytes
        self.secret_file.write_bytes(self.secret)

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def test_issue_then_verify_roundtrip(self) -> None:
        token = auth.issue_token("matt", "matt@example.com", ["admin", "users"])
        data = auth.verify_token(token)
        self.assertEqual(data["u"], "matt")
        self.assertEqual(data["e"], "matt@example.com")
        self.assertEqual(data["g"], ["admin", "users"])
        # Display name falls back to the username when not supplied.
        self.assertEqual(data["n"], "matt")
        self.assertGreater(data["exp"], data["iat"])

    def test_display_name_claim(self) -> None:
        token = auth.issue_token("matt", "", ["users"], "Matt Corddry")
        self.assertEqual(auth.verify_token(token)["n"], "Matt Corddry")

    def test_tampered_payload_rejected(self) -> None:
        token = auth.issue_token("matt", "", ["users"])
        payload_b64, sig_b64 = token.split(".", 1)
        # Flip a character in the payload (username).
        bad_payload = payload_b64[:-2] + ("A" if payload_b64[-2] != "A" else "B")
        self.assertIsNone(auth.verify_token(bad_payload + "." + sig_b64))

    def test_tampered_signature_rejected(self) -> None:
        token = auth.issue_token("matt", "", ["users"])
        payload_b64, sig_b64 = token.split(".", 1)
        bad_sig = ("A" if sig_b64[0] != "A" else "B") + sig_b64[1:]
        self.assertIsNone(auth.verify_token(payload_b64 + "." + bad_sig))

    def test_wrong_secret_rejected(self) -> None:
        token = auth.issue_token("matt", "", ["users"])
        self.secret_file.write_bytes(b"fedcba9876543210fedcba9876543210")
        self.assertIsNone(auth.verify_token(token))

    def test_expired_token_rejected(self) -> None:
        token = auth.issue_token("matt", "", ["users"])
        with mock.patch.object(auth, "SESSION_ABSOLUTE_SECONDS", -10):
            expired = auth.issue_token("matt", "", ["users"])
        self.assertIsNone(auth.verify_token(expired))

    def test_no_secret_returns_none(self) -> None:
        self.secret_file.unlink()
        self.assertIsNone(auth.verify_token("abc.def"))

    def test_malformed_token_rejected(self) -> None:
        for bad in ("", "noperiod", "!!!.!!!", "a.b.c"):
            self.assertIsNone(auth.verify_token(bad))


class VerifyCredentialsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._patches = [
            mock.patch.object(db, "postgres_admin_dsn", return_value="postgresql://u:p@127.0.0.1/postgres"),
            mock.patch.object(db, "pg_isready", return_value=True),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()

    def test_pg_down_returns_none(self) -> None:
        with mock.patch.object(db, "pg_isready", return_value=False):
            self.assertIsNone(auth.verify_credentials("matt", "password1"))

    def test_valid_credentials(self) -> None:
        conn, cur = _conn_with_cur()
        # First query (user) returns one row; second (groups) returns rows.
        cur.fetchone.return_value = ("matt", "Matt", "matt@example.com", "$2b$12$hash", False)
        cur.fetchall.return_value = [("admin",), ("users",)]
        with mock.patch.object(db, "cp_connect", return_value=conn), \
             mock.patch("bcrypt.checkpw", return_value=True):
            user = auth.verify_credentials("matt", "password1")
        self.assertEqual(user["username"], "matt")
        self.assertEqual(user["groups"], ["admin", "users"])

    def test_unknown_user_returns_none(self) -> None:
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = None
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertIsNone(auth.verify_credentials("ghost", "password1"))

    def test_disabled_user_returns_none(self) -> None:
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = ("matt", "Matt", "", "$2b$12$hash", True)
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertIsNone(auth.verify_credentials("matt", "password1"))

    def test_wrong_password_returns_none(self) -> None:
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = ("matt", "Matt", "", "$2b$12$hash", False)
        cur.fetchall.return_value = []
        with mock.patch.object(db, "cp_connect", return_value=conn), \
             mock.patch("bcrypt.checkpw", return_value=False):
            self.assertIsNone(auth.verify_credentials("matt", "wrong"))

    def test_db_error_returns_none(self) -> None:
        with mock.patch.object(db, "cp_connect", side_effect=RuntimeError("down")):
            self.assertIsNone(auth.verify_credentials("matt", "password1"))


class RateLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reset module-level rate-limit state between tests.
        auth._rate_failures.clear()
        auth._rate_lockouts.clear()

    def tearDown(self) -> None:
        auth._rate_failures.clear()
        auth._rate_lockouts.clear()

    def test_allows_below_threshold(self) -> None:
        for _ in range(auth.RATE_LIMIT_MAX_FAILURES - 1):
            auth.rate_limit_record_failure("1.2.3.4")
        self.assertTrue(auth.rate_limit_check("1.2.3.4"))

    def test_locks_out_at_threshold(self) -> None:
        for _ in range(auth.RATE_LIMIT_MAX_FAILURES):
            auth.rate_limit_record_failure("1.2.3.4")
        self.assertFalse(auth.rate_limit_check("1.2.3.4"))

    def test_reset_clears_lockout(self) -> None:
        for _ in range(auth.RATE_LIMIT_MAX_FAILURES):
            auth.rate_limit_record_failure("1.2.3.4")
        self.assertFalse(auth.rate_limit_check("1.2.3.4"))
        auth.rate_limit_reset("1.2.3.4")
        self.assertTrue(auth.rate_limit_check("1.2.3.4"))

    def test_ips_are_independent(self) -> None:
        for _ in range(auth.RATE_LIMIT_MAX_FAILURES):
            auth.rate_limit_record_failure("1.2.3.4")
        self.assertFalse(auth.rate_limit_check("1.2.3.4"))
        self.assertTrue(auth.rate_limit_check("5.6.7.8"))


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
        self.assertNotIn("service", status)
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
            mock.patch.object(auth, "SESSION_SECRET_FILE", root / "session-secret"),
            mock.patch.object(auth, "GATE_CONF", self.gate),
            mock.patch.object(auth, "ADMIN_CONF", self.admin_conf),
            mock.patch.object(auth, "RESET_MARKER_FILE", self.marker),
            mock.patch.object(auth, "hash_password", return_value="$2y$12$testhash"),
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

    def test_provisioning_retries_until_ready(self) -> None:
        # Postgres is up but provisioning is still running: create_admin must
        # poll until ready (regression: first-admin bootstrap raced the
        # provision worker on a fresh QEMU boot).
        conn, _cur = _conn_with_cur()
        with mock.patch.object(db, "cp_connect", return_value=conn), \
                mock.patch.object(auth, "PG_WAIT_SECONDS", 100), \
                mock.patch.object(
                    data_services,
                    "ensure_provisioned",
                    side_effect=[{"state": "waiting"}, {"state": "ready"}],
                ), \
                mock.patch.object(auth.time, "monotonic", side_effect=[0.0, 1.0]), \
                mock.patch.object(auth.time, "sleep"):
            result = auth.create_admin("admin", "longenough")
        self.assertEqual(result["state"], "ready")

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
        # Deterministic local-part email fallback (OWUI keys identity on email).
        user_params = cur.execute.call_args_list[0].args[1]
        self.assertEqual(user_params[2], "admin@localhost")
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


class AuthEmailIdentityTests(unittest.TestCase):
    """The synthetic email domain is a wire contract with Open WebUI."""

    def test_owui_email_shape(self) -> None:
        self.assertEqual(auth.owui_email("admin"), "admin@localhost")
        # ``localhost`` is the one no-dot domain OWUI's validate_email_format
        # exempts; adding a dot-free domain here would break its admin-side
        # user-create path. Keep it bare (no hostname/FQDN): the box renames
        # itself after the factory mDNS handoff and email is the identity key.
        self.assertEqual(auth.OWUI_EMAIL_DOMAIN, "localhost")


class AuthGateWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(auth, "SESSION_SECRET_FILE", root / "session-secret"),
            mock.patch.object(auth, "GATE_CONF", root / "auth-gate.conf"),
            mock.patch.object(auth, "ADMIN_CONF", root / "auth-admin.conf"),
            mock.patch.object(auth, "RESET_MARKER_FILE", root / "reset-admin"),
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
        """Wait for the worker's pass to *finish*, not just for the file to look right.

        ``write_gate`` writes GATE_CONF then ADMIN_CONF, so polling on GATE_CONF
        alone returns while the worker still has a write into the temp dir in
        flight — which raced with tearDown and made TemporaryDirectory.cleanup
        fail intermittently ("Directory not empty"). ``_gate_synced`` is set only
        after every write the worker performs.
        """
        for _ in range(100):
            text = auth.GATE_CONF.read_text() if auth.GATE_CONF.is_file() else None
            if auth._gate_synced and text is not None and bool(text.strip()) == expect_enabled:
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
