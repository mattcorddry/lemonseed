"""Unit tests for the M9 P4 probe library (control-plane/health.py).

Every external effect is stubbed — these tests assert the wrappers' parsing
and the Tracker's state machine, not the network.
"""

from __future__ import annotations

import io
import socket
import threading
import unittest
from unittest import mock

import health


def _resp(payload: bytes = b"", status: int = 200) -> mock.Mock:
    r = mock.Mock()
    r.read = lambda *a, **k: payload
    r.__enter__ = mock.Mock(return_value=r)
    r.__exit__ = mock.Mock(return_value=False)
    r.status = status
    return r


class ProbeHttpTests(unittest.TestCase):
    def test_2xx_is_ok_and_keeps_body(self):
        with mock.patch.object(
            health.urllib.request, "urlopen",
            lambda req, timeout: _resp(b'{"status":"ok"}'),
        ):
            ok, detail = health.probe_http("http://x/health")
        self.assertTrue(ok)
        self.assertEqual(detail, '{"status":"ok"}')

    def test_503_reports_body(self):
        exc = health.urllib.error.HTTPError(  # type: ignore[arg-type]
            "http://x/health", 503, "unavailable",
            {},  # hdrs
            io.BytesIO(b"db: disconnected"),
        )
        with mock.patch.object(health.urllib.request, "urlopen",
                               lambda req, timeout: (_ for _ in ()).throw(exc)):
            ok, detail = health.probe_http("http://x/health")
        self.assertFalse(ok)
        self.assertIn("503", detail)
        self.assertIn("db: disconnected", detail)

    def test_refused_reports_reason(self):
        err = health.urllib.error.URLError(
            ConnectionRefusedError(111, "Connection refused")
        )
        with mock.patch.object(health.urllib.request, "urlopen",
                               lambda req, timeout: (_ for _ in ()).throw(err)):
            ok, detail = health.probe_http("http://x/health")
        self.assertFalse(ok)
        self.assertIn("refused", detail)

    def test_bearer_key_is_sent(self):
        seen = {}

        def capture(req, timeout):
            seen["auth"] = req.headers.get("Authorization")
            return _resp(b"")

        with mock.patch.object(health.urllib.request, "urlopen", capture):
            health.probe_http("http://x/health", key="sk-test")
        self.assertEqual(seen["auth"], "Bearer sk-test")


class ProbeValkeyTests(unittest.TestCase):
    """A real loopback RESP server — the wire format is the thing under test."""

    def setUp(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.stop.set()
        self.sock.close()

    mode = "up"

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    data = conn.recv(64)
                except OSError:
                    continue
                if data.startswith(b"PING"):
                    reply = b"-LOADING connecting-to-master\r\n" \
                        if self.mode == "loading" else b"+PONG\r\n"
                    conn.sendall(reply)

    def test_ping_pong(self):
        ok, detail = health.probe_valkey(port=self.port)
        self.assertTrue(ok, detail)

    def test_loading_state_fails(self):
        # A valkey mid-RDB-load accepts TCP but answers the PING with -LOADING:
        # exactly the wedge a bare port check cannot see.
        type(self).mode = "loading"
        try:
            ok, detail = health.probe_valkey(port=self.port)
        finally:
            type(self).mode = "up"
        self.assertFalse(ok)
        self.assertIn("LOADING", detail)


class ProbePostgresTests(unittest.TestCase):
    def test_pg_isready_refusal_fails_fast(self):
        import db
        with mock.patch.object(db, "pg_isready", return_value=False) as ready:
            ok, detail = health.probe_postgres()
        self.assertFalse(ok)
        self.assertIn("pg_isready", detail)
        ready.assert_called_once()

    def test_select1_failure_fails(self):
        # pg_isready passes but the session-level query fails: a real wedge.
        import db
        boom = mock.Mock()
        boom.side_effect = Exception("boom")
        with mock.patch.object(db, "pg_isready", return_value=True), \
             mock.patch.dict("sys.modules", {"psycopg2": boom}):
            ok, detail = health.probe_postgres()
        self.assertFalse(ok)
        self.assertIn("SELECT 1", detail)

    def test_select1_uses_the_env_file_credentials(self):
        # The deep probe must authenticate like every other consumer. A
        # passwordless connect fails against TCP auth (fe_sendauth) and the
        # supervisor then recreates a perfectly healthy server in a loop.
        import db

        seen = {}

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql):
                seen["sql"] = sql

            def fetchone(self):
                return (1,)

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def close(self):
                pass

        with mock.patch.object(db, "pg_isready", return_value=True), \
             mock.patch.object(db, "_connect",
                               side_effect=lambda database: seen.update(
                                   dbname=database) or FakeConn()) as connect:
            ok, _ = health.probe_postgres()
        self.assertTrue(ok)
        # The credentials come from db's own env-file handling; the probe may
        # not build its own connection.
        connect.assert_called_once_with("postgres")
        self.assertEqual(seen["sql"], "SELECT 1")


class ProbeLemondTests(unittest.TestCase):
    def test_missing_key_fails_without_http(self):
        import hardware
        with mock.patch.object(hardware, "lemonade_api_key", return_value=""):
            ok, detail = health.probe_lemond()
        self.assertFalse(ok)
        self.assertIn("LEMONADE_API_KEY", detail)

    def test_ok_body_is_healthy(self):
        body = b'{"status":"ok","version":"2026.45.0"}'
        import hardware
        with mock.patch.object(hardware, "lemonade_api_key", return_value="sk"), \
             mock.patch.object(health.urllib.request, "urlopen",
                               lambda req, timeout: _resp(body)):
            ok, detail = health.probe_lemond()
        self.assertTrue(ok, detail)


class ProbeDockerHealthTests(unittest.TestCase):
    def _run(self, code: int, out: str):
        result = mock.Mock(returncode=code, stdout=out, stderr="")
        with mock.patch.object(health.subprocess, "run", return_value=result):
            return health.probe_docker_health("c")

    def test_healthy(self):
        self.assertIs(self._run(0, "healthy\n"), True)

    def test_unhealthy(self):
        self.assertIs(self._run(0, "unhealthy\n"), False)

    def test_no_healthcheck_is_none_never_a_guess(self):
        self.assertIsNone(self._run(0, "<no value>\n"))
        self.assertIsNone(self._run(1, ""))


class TrackerTests(unittest.TestCase):
    def test_starting_then_ready(self):
        t = health.Tracker()
        self.assertEqual(t.snapshot()["state"], "starting")
        t.record(True)
        snap = t.snapshot()
        self.assertEqual(snap["state"], "ready")
        self.assertIsNone(snap["last_error"])
        self.assertEqual(snap["failed_ticks"], 0)

    def test_four_consecutive_failures_go_unhealthy_with_error(self):
        t = health.Tracker()
        for _ in range(3):
            t.record(False, "http 503: db down")
            self.assertEqual(t.snapshot()["state"], "starting")
        t.record(False, "http 503: db down")
        snap = t.snapshot()
        self.assertEqual(snap["state"], "unhealthy")
        self.assertIn("db down", snap["last_error"] or "")
        self.assertTrue(snap["checked_at"])
        self.assertEqual(snap["failed_ticks"], 4)

    def test_one_success_recovers(self):
        t = health.Tracker(fail_limit=2)
        t.record(False, "x")
        t.record(True)
        self.assertEqual(t.snapshot()["state"], "ready")

    def test_stays_unhealthy_until_a_probe_succeeds(self):
        # No probe ran since going unhealthy (e.g. the container is stopped):
        # note_unhealthy keeps the verdict without inventing recovery.
        t = health.Tracker(fail_limit=1)
        t.record(False, "probe dead")
        self.assertEqual(t.snapshot()["state"], "unhealthy")

    def test_note_unhealthy_forces_the_verdict(self):
        t = health.Tracker()
        t.note_unhealthy("container exited (code 137)")
        snap = t.snapshot()
        self.assertEqual(snap["state"], "unhealthy")
        self.assertIn("exited", snap["last_error"] or "")

    def test_error_is_truncated(self):
        t = health.Tracker()
        t.record(False, "e" * 900)
        self.assertLessEqual(len(t.snapshot()["last_error"] or ""), 500)


if __name__ == "__main__":
    unittest.main()
