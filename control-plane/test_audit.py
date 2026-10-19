#!/usr/bin/env python3
"""Unit tests for audit.py (mocked; no Postgres required)."""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import audit  # noqa: E402
import db  # noqa: E402


def _captured_cursor():
    cur = mock.MagicMock()
    cur.__enter__ = mock.Mock(return_value=cur)
    cur.__exit__ = mock.Mock(return_value=False)
    return cur


class AuditRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cur = _captured_cursor()

    def _detail(self, **kwargs) -> dict:
        audit.record("alice", "bob", "user.create", kwargs, cur=self.cur)
        return json.loads(self.cur.execute.call_args.args[1][3])

    def test_writes_the_row_on_the_callers_cursor(self) -> None:
        """The row must commit with the change it describes, or not at all."""
        audit.record("alice", "bob", "user.create", cur=self.cur)
        sql, params = self.cur.execute.call_args.args
        self.assertIn("INSERT INTO audit_log", sql)
        self.assertIn("::jsonb", sql)
        self.assertEqual(params[:3], ("alice", "bob", "user.create"))
        # An empty detail is SQL NULL, not a "{}" blob: 365 days of rows that all
        # say nothing is noise that makes the interesting rows harder to see.
        self.assertIsNone(params[3])
        audit.record("alice", "bob", "user.create", {"groups": ["users"]}, cur=self.cur)
        self.assertEqual(
            json.loads(self.cur.execute.call_args.args[1][3]), {"groups": ["users"]}
        )

    def test_no_cursor_opens_its_own_connection(self) -> None:
        conn = mock.MagicMock()
        conn.cursor.return_value = self.cur
        with mock.patch.object(db, "cp_connect", return_value=conn):
            self.assertTrue(audit.record("alice", "bob", "user.delete"))
        conn.close.assert_called_once()

    def test_detail_carries_names_not_values_and_masks_secrets(self) -> None:
        """D-37: the failure mode of an audit log is faithfully recording a secret."""
        detail = self._detail(
            api_key="sk-live-abcdef",
            password="hunter2",
            groups=["admin", "users"],
            nested={"client_secret": "shh", "role": "admin"},
        )
        self.assertEqual(detail["groups"], ["admin", "users"])
        self.assertEqual(detail["api_key"], "***")
        self.assertEqual(detail["password"], "***")
        self.assertEqual(detail["nested"]["client_secret"], "***")
        self.assertEqual(detail["nested"]["role"], "admin")

    def test_journal_line_is_written_even_when_the_database_is_not(self) -> None:
        """The journal copy exists to survive a wiped database (§11)."""
        conn = mock.MagicMock()
        conn.cursor.side_effect = RuntimeError("database is gone")
        out = io.StringIO()
        with mock.patch.object(db, "cp_connect", return_value=conn):
            with redirect_stdout(out):
                ok = audit.record("alice", "bob", "user.create", {"groups": ["users"]})
        self.assertFalse(ok)
        line = out.getvalue()
        self.assertIn("lemonseed-audit actor=alice target=bob action=user.create", line)
        self.assertIn("COULD NOT RECORD", line)

    def test_a_failing_audit_write_never_raises_to_the_caller(self) -> None:
        """The operator's change already happened; a missing row is not their fault."""
        out = io.StringIO()
        with redirect_stdout(out):
            ok = audit.record("alice", "bob", "user.create", cur="not a cursor")
        self.assertFalse(ok)
        self.assertIn("COULD NOT RECORD", out.getvalue())

    def test_json_unfriendly_detail_is_still_logged(self) -> None:
        """A detail that cannot be encoded must not lose the event entirely.

        actor/target/action are what an operator needs to spot that something
        happened; the detail is a bonus. Dropping the row altogether because one
        value would not encode is the worse trade. A self-referencing dict is the
        case no encoder can finish.
        """
        looped: dict = {}
        looped["self"] = looped
        detail = self._detail(when=looped)
        self.assertIn("unloggable_detail", detail)

    def test_missing_actor_becomes_system(self) -> None:
        audit.record("", "bob", "user.create", cur=self.cur)
        self.assertEqual(self.cur.execute.call_args.args[1][0], audit.ACTOR_SYSTEM)


class AuditSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = None

    def _conn(self) -> tuple[mock.MagicMock, mock.MagicMock]:
        conn = mock.MagicMock()
        cur = mock.MagicMock()
        cur.__enter__ = mock.Mock(return_value=cur)
        cur.__exit__ = mock.Mock(return_value=False)
        cur.rowcount = 7
        conn.cursor.return_value = cur
        return conn, cur

    def test_sweep_deletes_only_expired_rows(self) -> None:
        conn, cur = self._conn()
        with mock.patch.object(db, "cp_connect", return_value=conn), \
                mock.patch.object(db, "postgres_admin_dsn", return_value="dsn"), \
                mock.patch.object(db, "pg_isready", return_value=True):
            self.assertEqual(audit.sweep(), 7)
        sql, params = cur.execute.call_args.args
        self.assertIn("DELETE FROM audit_log", sql)
        # Parameterised, so the retention window can never become an injection
        # site and the statement stays a constant.
        self.assertIn("make_interval", sql)
        self.assertEqual(params, (audit.AUDIT_RETENTION_DAYS,))

    def test_sweep_is_a_no_op_when_postgres_is_down(self) -> None:
        with mock.patch.object(db, "postgres_admin_dsn", return_value=None):
            self.assertEqual(audit.sweep(), 0)

    def test_sweep_swallows_its_own_errors(self) -> None:
        """It runs on a background tick; a raise there would kill the tick."""
        out = io.StringIO()
        with mock.patch.object(db, "cp_connect", side_effect=RuntimeError("boom")), \
                mock.patch.object(db, "postgres_admin_dsn", return_value="dsn"), \
                mock.patch.object(db, "pg_isready", return_value=True):
            with redirect_stdout(out):
                self.assertEqual(audit.sweep(), 0)
        self.assertIn("sweep failed", out.getvalue())


class AuditThrottleTests(unittest.TestCase):
    """``maybe_sweep`` is what the data supervisor actually calls.

    Retention has to survive a box whose Postgres is not up yet at boot, so the
    throttle must not charge an attempt that never reached the database.
    """

    def setUp(self) -> None:
        self._saved = audit._sweep_at
        audit._sweep_at = -audit.SWEEP_INTERVAL_SEC

    def tearDown(self) -> None:
        audit._sweep_at = self._saved

    def _ready(self) -> mock.MagicMock:
        """Both Postgres preconditions truthy (MagicMock is truthy, so a plain
        ``mock.DEFAULT`` patch stands in for "the database is there")."""
        return mock.patch.multiple(
            db, postgres_admin_dsn=mock.DEFAULT, pg_isready=mock.DEFAULT
        )

    def test_the_first_ready_tick_sweeps_without_waiting_six_hours(self) -> None:
        with self._ready():
            with mock.patch.object(audit, "sweep", return_value=3) as sweep:
                self.assertEqual(audit.maybe_sweep(), 3)
        sweep.assert_called_once_with()

    def test_a_second_tick_inside_the_window_is_free(self) -> None:
        with self._ready():
            with mock.patch.object(audit, "sweep", return_value=3) as sweep:
                audit.maybe_sweep()
                audit.maybe_sweep()
        self.assertEqual(sweep.call_count, 1)

    def test_an_unreachable_database_does_not_consume_the_slot(self) -> None:
        """Otherwise a CP that boots before Postgres waits 6h for nothing."""
        with mock.patch.object(audit, "sweep", return_value=3) as sweep:
            with mock.patch.object(db, "postgres_admin_dsn", return_value=None), \
                    mock.patch.object(db, "pg_isready", return_value=True):
                self.assertEqual(audit.maybe_sweep(), 0)
            with self._ready():
                self.assertEqual(audit.maybe_sweep(), 3)
        sweep.assert_called_once_with()

    def test_the_supervisor_tick_calls_it(self) -> None:
        """Wiring, not maths: if the call is dropped, rows grow forever."""
        source = (Path(__file__).resolve().parent / "data_services.py").read_text(
            encoding="utf-8"
        )
        body = source.split("def supervise_data_once")[1].split("def ensure_data_worker")[0]
        self.assertIn("audit.maybe_sweep()", body)


class AuditRecentTests(unittest.TestCase):
    def test_rows_are_returned_newest_first_with_parsed_detail(self) -> None:
        conn = mock.MagicMock()
        cur = mock.MagicMock()
        cur.__enter__ = mock.Mock(return_value=cur)
        cur.__exit__ = mock.Mock(return_value=False)
        cur.fetchall.return_value = [
            (2, None, "alice", "bob", "user.create", {"groups": ["users"]}),
            (1, None, "system", "", "user.delete", None),
        ]
        conn.cursor.return_value = cur
        with mock.patch.object(db, "cp_connect", return_value=conn):
            events = audit.recent(limit=5)
        self.assertEqual([e["id"] for e in events], [2, 1])
        self.assertEqual(events[0]["detail"], {"groups": ["users"]})
        # A NULL detail reads as an empty dict, never as None: the portal would
        # otherwise have to null-check every row for a case it cannot cause.
        self.assertEqual(events[1]["detail"], {})
        sql, params = cur.execute.call_args.args
        self.assertIn("ORDER BY id DESC LIMIT", sql)
        self.assertEqual(params, ("", "", 5))

    def test_target_filters_in_sql(self) -> None:
        conn = mock.MagicMock()
        cur = mock.MagicMock()
        cur.__enter__ = mock.Mock(return_value=cur)
        cur.__exit__ = mock.Mock(return_value=False)
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        with mock.patch.object(db, "cp_connect", return_value=conn):
            audit.recent(target="bob")
        self.assertEqual(cur.execute.call_args.args[1], ("bob", "bob", 200))

    def test_unreadable_database_is_empty_not_an_error(self) -> None:
        with mock.patch.object(db, "cp_connect", side_effect=RuntimeError("down")):
            self.assertEqual(audit.recent(), [])


if __name__ == "__main__":
    unittest.main()
