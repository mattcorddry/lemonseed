#!/usr/bin/env python3
"""Unit tests for auth.py (mocked; no Postgres required)."""

from __future__ import annotations

import contextlib
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

import audit  # noqa: E402
import auth  # noqa: E402
import data_services  # noqa: E402
import db  # noqa: E402
import sessions  # noqa: E402
import srp  # noqa: E402


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
            mock.patch.object(auth, "GATE_PERSIST", root / "auth-gate-persist.conf"),
            mock.patch.object(auth, "ADMIN_PERSIST", root / "auth-admin-persist.conf"),
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
        # The persistent copies the boot seeder restores (bare-metal review:
        # an empty-seeded gate left the edge open until Postgres was up).
        self.assertEqual(auth.GATE_PERSIST.read_text(), auth.GATE_CONF.read_text())
        self.assertEqual(auth.ADMIN_PERSIST.read_text(), auth.ADMIN_CONF.read_text())

        auth.write_gate(False)
        self.assertEqual(auth.GATE_CONF.read_text(), "")
        self.assertEqual(auth.ADMIN_CONF.read_text(), "")
        self.assertEqual(auth.GATE_PERSIST.read_text(), "")
        self.assertEqual(auth.ADMIN_PERSIST.read_text(), "")


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
        # The absolute window lives in sessions.py now (one source of truth).
        with mock.patch.object(sessions, "SESSION_ABSOLUTE_SECONDS", -10):
            expired = auth.issue_token("matt", "", ["users"])
        self.assertIsNone(auth.verify_token(expired))

    def test_token_carries_a_session_reference_and_generation(self) -> None:
        """The cookie is a *reference* to revocable state, not self-sufficient."""
        token = auth.issue_token("matt", "", ["users"], sid="sid-1", generation=7)
        data = auth.verify_token(token)
        self.assertEqual(data["s"], "sid-1")
        self.assertEqual(data["gen"], 7)

    def test_no_secret_returns_none(self) -> None:
        self.secret_file.unlink()
        self.assertIsNone(auth.verify_token("abc.def"))

    def test_malformed_token_rejected(self) -> None:
        for bad in ("", "noperiod", "!!!.!!!", "a.b.c"):
            self.assertIsNone(auth.verify_token(bad))


class SessionLifecycleTests(unittest.TestCase):
    """``verify_session`` is the gate's real check (M6.6).

    Four things must hold: signature, a live store record, the account's current
    generation, and room under the absolute ceiling. Each gets its own case
    because each is a different way a user could otherwise keep access.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.secret_file = Path(self._tmp.name) / "session-secret"
        self.secret_file.write_bytes(b"0123456789abcdef0123456789abcdef")
        self.now = int(time.time())
        self.store: dict[str, dict[str, object]] = {}
        self.generation = 3

        self.record = {
            "u": "matt",
            "e": "matt@localhost",
            "g": ["admin"],
            "n": "Matt",
            "gen": self.generation,
            "iat": self.now,
        }
        patches = [
            mock.patch.object(auth, "SESSION_SECRET_FILE", self.secret_file),
            mock.patch.object(auth, "user_generation", side_effect=lambda _u: self.generation),
            mock.patch.object(sessions, "read", side_effect=lambda sid: self.store.get(sid)),
            mock.patch.object(sessions, "create", side_effect=self._fake_create),
            mock.patch.object(sessions, "refresh", return_value=True),
            mock.patch.object(sessions, "drop_quietly", side_effect=self._fake_drop),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.token = auth.issue_token(
            "matt", "matt@localhost", ["admin"], "Matt", sid="sid-1", generation=self.generation
        )
        self.store["sid-1"] = dict(self.record)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fake_create(self, sid, record, now=None):
        self.store[sid] = dict(record)
        return True

    def _fake_drop(self, sid):
        return self.store.pop(sid, None) is not None

    def test_create_session_stores_state_and_returns_a_matching_token(self) -> None:
        token = auth.create_session(
            {
                "username": "matt",
                "email": "matt@localhost",
                "groups": ["admin"],
                "display_name": "Matt",
            }
        )
        payload = auth.verify_token(token)
        self.assertTrue(payload["s"])
        stored = self.store[payload["s"]]
        self.assertEqual(stored["gen"], self.generation)
        self.assertEqual(stored["g"], ["admin"])
        self.assertEqual(stored["n"], "Matt")

    def test_create_session_refuses_an_unknown_or_disabled_account(self) -> None:
        self.generation = None
        with self.assertRaises(ValueError):
            auth.create_session({"username": "matt", "email": "", "groups": []})

    def test_create_session_fails_closed_when_the_store_is_down(self) -> None:
        with mock.patch.object(
            sessions, "create", side_effect=sessions.SessionStoreUnavailable("down")
        ):
            with self.assertRaises(sessions.SessionStoreUnavailable):
                auth.create_session({"username": "matt", "email": "", "groups": []})

    def test_verify_session_accepts_a_live_record(self) -> None:
        self.assertEqual(auth.verify_session(self.token), self.record)

    def test_verify_session_rejects_a_pre_m66_cookie(self) -> None:
        """A token with no session id has no revocable state, so it cannot sign in."""
        legacy = auth.issue_token("matt", "matt@localhost", ["admin"], "Matt")
        self.assertIsNone(auth.verify_session(legacy))

    def test_verify_session_rejects_when_the_record_is_gone(self) -> None:
        del self.store["sid-1"]
        self.assertIsNone(auth.verify_session(self.token))

    def test_verify_session_rejects_a_revoked_generation(self) -> None:
        """Disable / demote / delete / sign-out-everywhere all land here."""
        self.generation = self.generation + 1
        self.assertIsNone(auth.verify_session(self.token))

    def test_verify_session_rejects_a_missing_or_disabled_account(self) -> None:
        self.generation = None
        self.assertIsNone(auth.verify_session(self.token))

    def test_verify_session_rejects_a_record_belonging_to_someone_else(self) -> None:
        self.store["sid-1"]["u"] = "eve"
        with mock.patch.object(auth, "user_generation", side_effect=lambda _u: 3):
            self.assertIsNone(auth.verify_session(self.token))

    def test_verify_session_rejects_at_the_absolute_ceiling(self) -> None:
        with mock.patch.object(sessions, "refresh", return_value=False):
            self.assertIsNone(auth.verify_session(self.token))

    def test_verify_session_propagates_an_unreachable_store(self) -> None:
        """Must not degrade to None, or 503 (broken) becomes 401 (sign in again)."""
        with mock.patch.object(
            sessions, "read", side_effect=sessions.SessionStoreUnavailable("down")
        ):
            with self.assertRaises(sessions.SessionStoreUnavailable):
                auth.verify_session(self.token)

    def test_verify_session_fails_closed_on_a_corrupt_record(self) -> None:
        self.store["sid-1"]["gen"] = "not-a-number"
        self.assertIsNone(auth.verify_session(self.token))

    def test_drop_session_removes_only_a_verified_tokens_record(self) -> None:
        auth.drop_session("garbage-cookie")
        self.assertIn("sid-1", self.store)
        auth.drop_session(self.token)
        self.assertNotIn("sid-1", self.store)


class SessionGenerationDbTests(unittest.TestCase):
    """The generation counter against a mocked connection."""

    def setUp(self) -> None:
        self._patches = [
            mock.patch.object(db, "postgres_admin_dsn", return_value="postgresql://u:p@h/p"),
            mock.patch.object(db, "pg_isready", return_value=True),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in self._patches:
            patch.stop()

    def test_user_generation_reads_the_column(self) -> None:
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = (5,)
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertEqual(auth.user_generation("matt"), 5)
        conn.close.assert_called_once()

    def test_user_generation_is_none_for_a_missing_or_disabled_account(self) -> None:
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = None
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertIsNone(auth.user_generation("matt"))

    @mock.patch.object(db, "pg_isready", return_value=False)
    def test_user_generation_is_none_when_pg_is_not_ready(self, _ready: mock.Mock) -> None:
        with mock.patch.object(db, "cp_connect") as connect:
            self.assertIsNone(auth.user_generation("matt"))
        connect.assert_not_called()

    def test_user_generation_fails_closed_on_a_db_error(self) -> None:
        conn, cur = _conn_with_cur()
        cur.execute.side_effect = RuntimeError("boom")
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertIsNone(auth.user_generation("matt"))

    def test_revoke_sessions_bumps_the_counter(self) -> None:
        conn, cur = _conn_with_cur()
        cur.rowcount = 1
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertTrue(auth.revoke_sessions("matt"))
        sql = cur.execute.call_args_list[0].args[0]
        self.assertIn("token_generation = token_generation + 1", sql)

    def test_revoke_sessions_reports_an_unknown_account(self) -> None:
        conn, cur = _conn_with_cur()
        cur.rowcount = 0
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertFalse(auth.revoke_sessions("nobody"))


class SrpLoadUserTests(unittest.TestCase):
    """``srp_load_user`` replaced ``verify_credentials``.

    It loads state; it does not authenticate. Nothing here takes a password,
    because there is no function left in ``auth`` that does.
    """

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
            self.assertIsNone(auth.srp_load_user("matt"))

    def test_enrolled_row_exposes_the_verifier_and_groups(self) -> None:
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = (
            "matt", "Matt", "matt@example.com", "ab" * 16, "deadbeef", False, False,
        )
        cur.fetchall.return_value = [("admin",), ("users",)]
        with mock.patch.object(db, "cp_connect", return_value=conn):
            user = auth.srp_load_user("matt")
        self.assertEqual(user["username"], "matt")
        self.assertEqual(user["groups"], ["admin", "users"])
        self.assertEqual(user["srp_verifier"], "deadbeef")
        self.assertEqual(user["srp_salt"], "ab" * 16)
        self.assertFalse(user["password_reset_required"])

    def test_unknown_user_returns_none(self) -> None:
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = None
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertIsNone(auth.srp_load_user("ghost"))

    def test_disabled_user_returns_none(self) -> None:
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = (
            "matt", "Matt", "", "ab" * 16, "deadbeef", True, False,
        )
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertIsNone(auth.srp_load_user("matt"))

    def test_unenrolled_row_is_distinguishable_by_an_empty_verifier(self) -> None:
        """A row exists but cannot sign in; the caller must be able to tell."""
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = ("matt", "Matt", "", None, None, False, False)
        cur.fetchall.return_value = []
        with mock.patch.object(db, "cp_connect", return_value=conn):
            user = auth.srp_load_user("matt")
        self.assertIsNotNone(user)
        self.assertEqual(user["srp_verifier"], "")
        self.assertEqual(user["srp_salt"], "")

    def test_reset_flag_is_carried_for_the_verify_phase(self) -> None:
        """D-11: only the handshake row can say "rotate before you get a session".

        If the flag were absent, verify would mint a session for an account still
        sitting on a one-time password, which is the half-authenticated state the
        decision exists to avoid.
        """
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = (
            "matt", "Matt", "", "ab" * 16, "deadbeef", False, True,
        )
        cur.fetchall.return_value = [("users",)]
        with mock.patch.object(db, "cp_connect", return_value=conn):
            user = auth.srp_load_user("matt")
        self.assertTrue(user["password_reset_required"])

    def test_db_error_returns_none(self) -> None:
        with mock.patch.object(db, "cp_connect", side_effect=RuntimeError("down")):
            self.assertIsNone(auth.srp_load_user("matt"))

    def test_no_password_column_is_read(self) -> None:
        """The query must not select ``password_hash`` any more.

        It is vestigial, and a code path that still reads it is a code path one
        refactor away from becoming a plaintext comparison again.
        """
        conn, cur = _conn_with_cur()
        cur.fetchone.return_value = None
        with mock.patch.object(db, "cp_connect", return_value=conn):
            auth.srp_load_user("matt")
        sql = cur.execute.call_args_list[0].args[0]
        self.assertNotIn("password_hash", sql)
        self.assertIn("srp_verifier", sql)


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


class _FakeValkey:
    """Enough of the client for the rate limiter: exists/get/set/delete/pipeline."""

    def __init__(self) -> None:
        self.store: dict[str, object] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[tuple] = []
        self.fail = False

    def _check(self) -> None:
        if self.fail:
            raise RuntimeError("valkey is down")

    def exists(self, key: str) -> int:
        self._check()
        self.calls.append(("exists", key))
        return 1 if key in self.store else 0

    def get(self, key: str):
        self._check()
        return self.store.get(key)

    def set(self, key: str, value: object, ex: Optional[int] = None) -> bool:
        self._check()
        self.calls.append(("set", key, ex))
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def delete(self, *keys: str) -> int:
        self._check()
        self.calls.append(("delete",) + keys)
        removed = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                removed += 1
            self.ttls.pop(key, None)
        return removed

    def pipeline(self, transaction: bool = True):
        return _FakePipeline(self, transaction)


class _FakePipeline:
    def __init__(self, client: _FakeValkey, transaction: bool) -> None:
        self.client = client
        self.transaction = transaction
        self.ops: list[tuple] = []

    def incr(self, key: str) -> None:
        self.ops.append(("incr", key))

    def expire(self, key: str, seconds: int) -> None:
        self.ops.append(("expire", key, seconds))

    def execute(self) -> list[object]:
        results: list[object] = []
        for op in self.ops:
            if op[0] == "incr":
                self.client.store[op[1]] = int(self.client.store.get(op[1], 0)) + 1
                self.client.calls.append(("incr", op[1]))
                results.append(self.client.store[op[1]])
            elif op[0] == "expire":
                self.client.ttls[op[1]] = op[2]
                self.client.calls.append(("expire", op[1], op[2]))
                results.append(True)
        self.ops = []
        return results


class RateLimitValkeyTests(unittest.TestCase):
    """The lockout must outlive a control-plane restart (M7 P2).

    This is the reason for the move off process dicts: an A/B update restarts the
    CP, which used to wipe every lockout and hand an attacker a fresh budget.
    """

    IP = "192.0.2.7"

    def setUp(self) -> None:
        auth._rate_failures.clear()
        auth._rate_lockouts.clear()
        self.valkey = _FakeValkey()
        patch = mock.patch.object(sessions, "client", return_value=self.valkey)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._clear)

    def _clear(self) -> None:
        auth._rate_failures.clear()
        auth._rate_lockouts.clear()

    def _fail_to_lockout(self) -> None:
        for _ in range(auth.RATE_LIMIT_MAX_FAILURES):
            auth.rate_limit_record_failure(self.IP)

    def test_failures_are_counted_in_valkey(self) -> None:
        auth.rate_limit_record_failure(self.IP)
        self.assertIn(self._failures_key(), self.valkey.store)

    def _failures_key(self) -> str:
        return f"{auth.RATE_LIMIT_KEY_PREFIX}:f:{self.IP}"

    def _lockout_key(self) -> str:
        return f"{auth.RATE_LIMIT_KEY_PREFIX}:l:{self.IP}"

    def test_lockout_lands_in_valkey_with_a_ttl(self) -> None:
        self._fail_to_lockout()
        self.assertIn(self._lockout_key(), self.valkey.store)
        self.assertEqual(self.valkey.ttls[self._lockout_key()], auth.RATE_LIMIT_LOCKOUT_SECONDS)

    def test_lockout_survives_a_control_plane_restart(self) -> None:
        """The regression this whole change exists to fix.

        Simulated by clearing the process dicts, which is exactly what a restart
        does. The lockout must still be in force.
        """
        self._fail_to_lockout()
        self.assertFalse(auth.rate_limit_check(self.IP))
        auth._rate_failures.clear()
        auth._rate_lockouts.clear()
        self.assertFalse(auth.rate_limit_check(self.IP))

    def test_the_counter_key_always_gets_a_ttl(self) -> None:
        """A counter with no TTL is a permanent lockout waiting to happen."""
        auth.rate_limit_record_failure(self.IP)
        expires = [c for c in self.valkey.calls if c[0] == "expire" and c[1] == self._failures_key()]
        self.assertTrue(expires, "INCR was not paired with EXPIRE")
        self.assertEqual(expires[0][2], auth.RATE_LIMIT_WINDOW_SECONDS)

    def test_increment_and_expire_share_one_pipeline(self) -> None:
        """Split across two round trips, a crash between them leaks a key forever."""
        calls: list[str] = []
        original = self.valkey.pipeline

        def spy(*a, **kw):
            calls.append("pipeline")
            return original(*a, **kw)

        self.valkey.pipeline = spy  # type: ignore[method-assign]
        auth.rate_limit_record_failure(self.IP)
        self.assertEqual(calls, ["pipeline"])
        # INCR and EXPIRE both went through it, not as separate client calls.
        incr_at = [i for i, c in enumerate(self.valkey.calls) if c[0] == "incr"]
        expire_at = [i for i, c in enumerate(self.valkey.calls) if c[0] == "expire"]
        self.assertEqual(len(incr_at), 1)
        self.assertEqual(len(expire_at), 1)

    def test_success_clears_both_keys(self) -> None:
        self._fail_to_lockout()
        auth.rate_limit_reset(self.IP)
        self.assertNotIn(self._failures_key(), self.valkey.store)
        self.assertNotIn(self._lockout_key(), self.valkey.store)
        self.assertTrue(auth.rate_limit_check(self.IP))

    def test_allows_when_valkey_is_absent(self) -> None:
        """Failure direction is allow.

        Inverse of the session store on purpose: a session store we cannot read
        must not serve authenticated pages, but a counter we cannot read must not
        lock a household out of their own appliance.
        """
        self.valkey.fail = True
        self.assertTrue(auth.rate_limit_check(self.IP))
        # And a failed write must not raise into the sign-in path.
        auth.rate_limit_record_failure(self.IP)
        auth.rate_limit_reset(self.IP)

    def test_falls_back_to_the_local_dicts_when_valkey_is_absent(self) -> None:
        """With no Valkey the throttle still works in-process."""
        self.valkey.fail = True
        for _ in range(auth.RATE_LIMIT_MAX_FAILURES):
            auth.rate_limit_record_failure(self.IP)
        self.assertFalse(auth.rate_limit_check(self.IP))

    def test_lockout_locks_only_the_offending_ip(self) -> None:
        self._fail_to_lockout()
        self.assertFalse(auth.rate_limit_check(self.IP))
        self.assertTrue(auth.rate_limit_check("198.51.100.9"))


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
        self.conn, self.cur = _conn_with_cur()
        self._patches = [
            mock.patch.object(auth, "SESSION_SECRET_FILE", root / "session-secret"),
            mock.patch.object(auth, "GATE_CONF", self.gate),
            mock.patch.object(auth, "ADMIN_CONF", self.admin_conf),
            mock.patch.object(auth, "GATE_PERSIST", root / "auth-gate-persist.conf"),
            mock.patch.object(auth, "ADMIN_PERSIST", root / "auth-admin-persist.conf"),
            mock.patch.object(auth, "RESET_MARKER_FILE", self.marker),
            mock.patch.object(auth, "ADMINS_FILE", root / "admins.json"),
            mock.patch.object(auth, "reload_nginx"),
            # The roster is D-28's installer affordance; AuthAdminRosterTests owns it.
            mock.patch.object(auth, "publish_admin_roster"),
            # Audit rows are asserted as calls here; test_audit.py covers their SQL.
            mock.patch.object(audit, "record"),
            # create_admin writes in one transaction (D-6), so it asks for
            # cp_transaction, not cp_connect. The fake conn is the same shape.
            mock.patch.object(
                db, "cp_transaction", lambda: contextlib.nullcontext(self.conn)
            ),
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

    @staticmethod
    def _enrollment(username: str = "admin", password: str = "longenough") -> tuple[str, str]:
        """A real browser-produced pair, so the validation path is exercised."""
        salt, verifier = srp.make_enrollment(srp.SERVER_GROUP, username, password)
        return salt.hex(), f"{verifier:x}"

    def test_rejects_invalid_username(self) -> None:
        salt, verifier = self._enrollment()
        bad = (
            "", "1bad", "a" * 17, "a" * 65, "has space", "-lead", "_lead",
            "dot.", "dash-", "..", ".", "a",
            # Reserved rather than malformed; the message must say so (D-33).
            "root", "nobody", "nginx", "postgres", "lemonseed-cp", "lemonseed-lemond",
            "systemd-network", "_systemd-disk", "dhcp4",
        )
        for value in bad:
            with self.subTest(username=value or "<empty>"), self.assertRaises(ValueError, msg=repr(value)):
                auth.create_admin(value, salt, verifier)

    def _statements(self) -> list[str]:
        return [call.args[0] for call in self.cur.execute.call_args_list]

    def _upsert_statement(self) -> str:
        for call in self.cur.execute.call_args_list:
            if "INSERT INTO users" in call.args[0]:
                return call.args[0]
        self.fail("create_admin wrote no user row")
        return ""

    def _upsert_params(self) -> tuple:
        """Params of the ``INSERT INTO users`` statement, wherever it sits.

        Located by content rather than by index on purpose: the statement count
        is asserted once in test_happy_path_first_admin, and every other test
        here should keep passing when an unrelated statement is added.
        """
        for call in self.cur.execute.call_args_list:
            if "INSERT INTO users" in call.args[0]:
                return call.args[1]
        self.fail("create_admin wrote no user row")

    def test_username_is_normalised_not_rejected(self) -> None:
        """``Matt`` is ``matt`` (D-33), and the row stored is the lower-cased one.

        Refusing mixed case would be the friendlier-looking bug: the same person
        typing their name the way it is written would be told their own account
        does not exist. Normalising is only safe because the *stored* form is the
        normalised one on every path, including the SRP identity string.
        """
        salt, verifier = self._enrollment("matt")
        result = auth.create_admin("  Matt  ", salt, verifier)
        self.assertEqual(result["username"], "matt")
        self.assertEqual(self._upsert_params()[0], "matt")

    def test_display_name_is_stored_not_the_username(self) -> None:
        """D-25: the setup form's name field reaches ``display_name``."""
        salt, verifier = self._enrollment()
        auth.create_admin("admin", salt, verifier, display_name="   Matt D.   ")
        self.assertEqual(self._upsert_params()[1], "Matt D.")

    def test_display_name_defaults_to_the_username(self) -> None:
        """No name field on a pre-P3 setup post → the username, never empty."""
        salt, verifier = self._enrollment()
        auth.create_admin("admin", salt, verifier)
        self.assertEqual(self._upsert_params()[1], "admin")

    def test_rejects_a_control_character_in_the_display_name(self) -> None:
        """The name goes out as an HTTP header value to Open WebUI (D-25)."""
        salt, verifier = self._enrollment()
        with self.assertRaises(ValueError):
            auth.create_admin("admin", salt, verifier, display_name="Matt\nX-Role: admin")

    def test_email_is_not_written_from_the_setup_form(self) -> None:
        """D-16': the OWUI identity is derived, so nothing here stores an address.

        Writing ``admin@localhost`` into ``users.email`` was the F-1 bug waiting
        to happen: a household address in that column, and ``X-Forwarded-Email``
        reading it, is exactly how the duplicate admin account appeared.
        """
        salt, verifier = self._enrollment()
        auth.create_admin("admin", salt, verifier)
        sqls = self._statements()
        params = [call.args[1] for call in self.cur.execute.call_args_list]
        self.assertNotIn("admin@localhost", " ".join(sqls))
        self.assertFalse(any("@localhost" in str(p) for p in params))

    def test_rejects_malformed_srp_pair(self) -> None:
        """Enrollment arrives from a browser, so it is untrusted input."""
        good_salt, good_verifier = self._enrollment()
        for bad in (
            ("", good_verifier),
            ("abcd", good_verifier),
            (good_salt, ""),
            (good_salt, "0"),
            (good_salt, "not hex"),
            (good_salt, f"{srp.SERVER_GROUP.n:x}"),
        ):
            with self.subTest(pair=str(bad)[:24]):
                with self.assertRaises(ValueError):
                    auth.create_admin("admin", bad[0], bad[1])

    def test_no_password_length_policy_is_enforced_server_side(self) -> None:
        """Documents the deliberate consequence of never seeing the password.

        The server cannot check a password it never receives. This is not an
        oversight to fix here but the trade the design accepted, so it is pinned
        where someone looking for the policy will find it.
        """
        salt, verifier = self._enrollment(password="x")
        self.assertEqual(auth.create_admin("admin", salt, verifier)["state"], "ready")

    def test_pg_not_ready(self) -> None:
        salt, verifier = self._enrollment()
        with mock.patch.object(db, "pg_isready", return_value=False):
            with self.assertRaises(ValueError):
                auth.create_admin("admin", salt, verifier)

    def test_data_services_not_ready(self) -> None:
        salt, verifier = self._enrollment()
        with mock.patch.object(
            data_services, "ensure_provisioned", return_value={"state": "starting"}
        ):
            with self.assertRaises(ValueError):
                auth.create_admin("admin", salt, verifier)

    def test_provisioning_retries_until_ready(self) -> None:
        # Postgres is up but provisioning is still running: create_admin must
        # poll until ready (regression: first-admin bootstrap raced the
        # provision worker on a fresh QEMU boot).
        salt, verifier = self._enrollment()
        with mock.patch.object(auth, "PG_WAIT_SECONDS", 100), \
                mock.patch.object(
                    data_services,
                    "ensure_provisioned",
                    side_effect=[{"state": "waiting"}, {"state": "ready"}],
                ), \
                mock.patch.object(auth.time, "monotonic", side_effect=[0.0, 1.0]), \
                mock.patch.object(auth.time, "sleep"):
            result = auth.create_admin("admin", salt, verifier)
        self.assertEqual(result["state"], "ready")

    def test_db_error_wrapped(self) -> None:
        salt, verifier = self._enrollment()

        def _boom():
            raise RuntimeError("boom")

        with mock.patch.object(db, "cp_transaction", _boom):
            with self.assertRaises(ValueError):
                auth.create_admin("admin", salt, verifier)

    def test_happy_path_first_admin(self) -> None:
        salt, verifier = self._enrollment()
        result = auth.create_admin("admin", salt, verifier)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["reset"], False)
        self.assertEqual(result["owui_cleanup"], [])
        sqls = self._statements()
        # Two system groups + user upsert + both mappings + the first-admin
        # marker (6 statements).
        self.assertEqual(len(sqls), 6)
        self.assertFalse(any("DELETE" in sql.upper() for sql in sqls))
        # Groups are seeded with their system flag asserted (D-3), which is why
        # seeding lives here and not in migration SQL.
        self.assertIn("INSERT INTO groups", sqls[0])
        self.assertIn("system", sqls[0])
        self.assertIn("INSERT INTO groups", sqls[1])
        upsert = self._upsert_statement()
        self.assertIn("ON CONFLICT (username) DO UPDATE", upsert)
        self.assertIn("srp_salt = EXCLUDED.srp_salt", upsert)
        self.assertIn("srp_verifier = EXCLUDED.srp_verifier", upsert)
        self.assertIn("password_reset_required = FALSE", upsert)
        # A reset must invalidate sessions issued to whoever held the row before.
        self.assertIn("token_generation = users.token_generation + 1", upsert)
        self.assertIn("srp_verifier", upsert)
        user_params = self._upsert_params()
        self.assertEqual(user_params[0], "admin")
        self.assertEqual(user_params[1], "admin")
        # The verifier and its salt are what actually get stored.
        self.assertEqual(user_params[2], salt)
        self.assertEqual(user_params[3], verifier)
        self.assertEqual(
            len([sql for sql in sqls if "INSERT INTO user_groups" in sql]), 2
        )
        self.assertEqual(len([sql for sql in sqls if "INSERT INTO cp_meta" in sql]), 1)
        self.assertIn("auth_request", self.gate.read_text())
        # Admin group check moved to CP (X-Forwarded-Groups); admin conf is empty.
        self.assertEqual(self.admin_conf.read_text(), "")
        actors = [call.args[:3] for call in audit.record.call_args_list]
        self.assertEqual(actors, [(audit.ACTOR_SYSTEM, "admin", "user.create")])

    def test_first_admin_row_is_recorded_by_identity_not_by_name(self) -> None:
        """D-22: the reserved first-admin row has to be findable after a rename.

        The username is whatever the operator typed at setup, so anything that
        hard-codes ``admin`` is a latent bug. The marker is the row, and the only
        thing that may rely on it is a check for *this* row.
        """
        salt, verifier = self._enrollment("matt")
        auth.create_admin("matt", salt, verifier)
        meta = [
            call for call in self.cur.execute.call_args_list
            if "INSERT INTO cp_meta" in call.args[0]
        ]
        self.assertEqual(len(meta), 1)
        self.assertEqual(meta[0].args[0].count("%s"), 2)
        self.assertEqual(meta[0].args[1][0], auth.FIRST_ADMIN_KEY)
        self.assertEqual(json.loads(meta[0].args[1][1]), {"username": "matt"})

    def test_reset_overwrites_the_old_verifier(self) -> None:
        """An admin reset must not leave the previous password working.

        The upsert has to assign both SRP columns. If it only inserted, a reset
        would report success while the row kept authenticating with the old
        verifier — a silent failure in the one flow people reach when locked out.
        """
        salt, verifier = self._enrollment()
        self.marker.write_text("1\n")
        self.cur.fetchall.return_value = []
        auth.create_admin("admin", salt, verifier)
        upsert = self._upsert_statement()
        self.assertIn("ON CONFLICT (username) DO UPDATE", upsert)
        self.assertIn("srp_salt = EXCLUDED.srp_salt", upsert)
        self.assertIn("srp_verifier = EXCLUDED.srp_verifier", upsert)
        # The pair stored is the freshly derived one, not the stored old pair.
        self.assertEqual(self._upsert_params()[2], salt)
        self.assertEqual(self._upsert_params()[3], verifier)

    def test_reset_deletes_differently_named_admins_and_consumes_marker(self) -> None:
        """D-20a: a straggler admin row may not keep a working verifier.

        Demoting instead of deleting left the former admin signed-in-able as an
        ordinary user with their OWUI history intact, which is the outcome the
        checkbox explicitly warns about.
        """
        salt, verifier = self._enrollment("newadmin")
        self.marker.write_text("1\n")
        self.cur.fetchall.return_value = [("oldadmin", "owui-uuid-1")]
        result = auth.create_admin("newadmin", salt, verifier)
        self.assertEqual(result["reset"], True)
        self.assertEqual(
            result["owui_cleanup"],
            [{"username": "oldadmin", "owui_id": "owui-uuid-1",
              "email": "oldadmin@localhost"}],
        )
        sqls = self._statements()
        delete_users = "DELETE FROM users WHERE username = %s"
        # The former admin's own row goes; the admin *group* is not emptied.
        self.assertIn(delete_users, sqls)
        self.assertFalse(any("DELETE FROM user_groups" in sql for sql in sqls))
        deletes = [
            call.args[1] for call in self.cur.execute.call_args_list
            if delete_users in call.args[0]
        ]
        self.assertEqual(deletes, [("oldadmin",)])
        # The straggler is removed before the new admin's joins are written, so a
        # crash between the two can never leave both accounts in the group.
        self.assertLess(
            sqls.index(delete_users),
            next(i for i, sql in enumerate(sqls) if "INSERT INTO user_groups" in sql),
        )
        actors = [call.args[:3] for call in audit.record.call_args_list]
        self.assertIn((audit.ACTOR_SYSTEM, "oldadmin", "user.delete"), actors)
        self.assertIn((audit.ACTOR_SYSTEM, "newadmin", "user.password_reset"), actors)
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
            mock.patch.object(auth, "GATE_PERSIST", root / "auth-gate-persist.conf"),
            mock.patch.object(auth, "ADMIN_PERSIST", root / "auth-admin-persist.conf"),
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


class RedirectTargetTests(unittest.TestCase):
    """``rd`` is attacker-controlled text, so each rejected shape is its own case.

    The gate hands us ``/login?rd=$request_uri`` verbatim and the login form
    echoes the value back, so this function is the only thing standing between
    a sign-in page and an off-appliance redirect. The ``/\\host`` case is the
    one that used to be missing: browsers resolve it exactly like ``//host``.
    """

    def test_plain_path_is_kept(self) -> None:
        self.assertEqual(auth.safe_redirect_target("/settings/"), "/settings/")

    def test_query_string_survives(self) -> None:
        # The truncation bug: nginx does not encode $request_uri, so a target
        # with its own query string must still arrive whole.
        self.assertEqual(
            auth.safe_redirect_target("/settings/?tab=models"),
            "/settings/?tab=models",
        )

    def test_empty_is_root(self) -> None:
        self.assertEqual(auth.safe_redirect_target(""), "/")

    def test_absolute_url_is_rejected(self) -> None:
        self.assertEqual(auth.safe_redirect_target("http://evil.example/x"), "/")

    def test_protocol_relative_is_rejected(self) -> None:
        self.assertEqual(auth.safe_redirect_target("//evil.example"), "/")

    def test_backslash_origin_is_rejected(self) -> None:
        self.assertEqual(auth.safe_redirect_target(r"/\evil.example"), "/")

    def test_encoded_double_slash_is_rejected(self) -> None:
        # Decoding happens before checking, so the encoded form cannot sneak a
        # host past a check that only looked at the raw string.
        self.assertEqual(auth.safe_redirect_target("%2f%2fevil.example"), "/")

    def test_scheme_hidden_in_the_path_is_rejected(self) -> None:
        self.assertEqual(auth.safe_redirect_target("/http://evil.example"), "/")

    def test_double_slash_after_the_path_is_rejected(self) -> None:
        self.assertEqual(auth.safe_redirect_target("/a//evil.example"), "/")

    def test_double_slash_inside_a_query_value_is_kept(self) -> None:
        # The path decides the origin; a query value is just data for our page.
        self.assertEqual(auth.safe_redirect_target("/a?next=//x"), "/a?next=//x")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
