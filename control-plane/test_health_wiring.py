"""M9 P4 wiring tests: deep probes drive status and restart decisions.

The supervisors are driven with stubbed docker/sockets; what is under test
is the state machine around health.py, not Docker or Postgres.
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import data_services as ds
import health
import litellm
import module_manager as mm


def _docker_recorder():
    calls: list[list[str]] = []

    def rec(args, timeout=120):  # type: ignore[no-untyped-def]
        calls.append(list(args))
        return mock.Mock(returncode=0, stdout="", stderr="")

    return rec, calls


class DataServicesWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pg = ds._pg_health
        self.valkey = ds._valkey_health
        # Fresh trackers per test: module-level state must not leak.
        ds._pg_health.__dict__.update(health.Tracker().__dict__)
        ds._valkey_health.__dict__.update(health.Tracker().__dict__)

    def _tick(self, *, deep_ok: bool, running: bool = True) -> list[list[str]]:
        rec, calls = _docker_recorder()
        with (
            mock.patch.object(ds, "_docker", side_effect=rec),
            mock.patch.object(ds, "_container_running", return_value=running),
            mock.patch.object(
                ds, "_port_open",
                side_effect=lambda *_a, **_k: running,
            ),
            mock.patch.object(health, "probe_postgres",
                              return_value=(deep_ok, "SELECT 1: wedged")),
            # valkey rides the same tick; it is healthy here — this file
            # tests one service's state machine at a time.
            mock.patch.object(health, "probe_valkey", return_value=(True, "")),
        ):
            ds.supervise_data_once()
        return calls

    def test_deep_failure_flips_state_unhealthy_without_restart(self) -> None:
        # Inside the cold-start grace the failure is recorded (it is real:
        # the port answers but the protocol does not) yet nothing restarts.
        ds._containers[ds.POSTGRES_CONTAINER]["since"] = time.monotonic()
        for _ in range(4):
            self._tick(deep_ok=False)
        snap = ds.postgres_snapshot()
        self.assertEqual(snap["health"]["state"], "unhealthy")
        self.assertIn("wedged", snap["health"]["last_error"] or "")
        self.assertEqual(snap["state"], "unhealthy")
        # Not yet past grace: no container was touched.
        calls = self._tick(deep_ok=False)
        self.assertFalse(any(
            c[:2] == ["rm", "-f"] and ds.POSTGRES_CONTAINER in c for c in calls
        ))

    def test_deep_failures_past_limit_and_grace_recreate(self) -> None:
        # Past cold-start grace a wedged daemon is recreated — on the pass
        # AFTER its verdict surfaced (two-phase healing: the unhealthy state
        # must be observable in /api/data before anything restarts).
        ds._containers[ds.POSTGRES_CONTAINER]["since"] = time.monotonic() - 10_000
        calls: list[list[str]] = []
        for _ in range(4):
            calls = self._tick(deep_ok=False)
        self.assertFalse(any(
            c[:2] == ["rm", "-f"] and ds.POSTGRES_CONTAINER in c for c in calls
        ))
        snap = ds.postgres_snapshot()
        self.assertEqual(snap["health"]["state"], "unhealthy")
        # The next pass heals it.
        calls = self._tick(deep_ok=False)
        self.assertTrue(any(
            c[:2] == ["rm", "-f"] and ds.POSTGRES_CONTAINER in c for c in calls
        ))

    def test_within_grace_no_recreate_and_status_starting(self) -> None:
        ds._containers[ds.POSTGRES_CONTAINER]["since"] = time.monotonic()
        calls: list[list[str]] = []
        for _ in range(3):
            calls = self._tick(deep_ok=False)
        self.assertFalse(any(
            c[:2] == ["rm", "-f"] and ds.POSTGRES_CONTAINER in c for c in calls
        ))
        # The tracker has not reached its limit yet: still starting, honest.
        self.assertEqual(ds.postgres_snapshot()["health"]["state"], "starting")

    def test_deep_success_recovers_ready(self) -> None:
        self._tick(deep_ok=False)
        self._tick(deep_ok=True)
        snap = ds.postgres_snapshot()
        self.assertEqual(snap["health"]["state"], "ready")
        self.assertEqual(snap["state"], "ready")

    def test_stopped_container_is_unhealthy_then_starting(self) -> None:
        rec, calls = _docker_recorder()
        with (
            mock.patch.object(ds, "_docker", side_effect=rec),
            mock.patch.object(ds, "_container_running", return_value=False),
            mock.patch.object(ds, "_container_exists", return_value=True),
        ):
            # First pass: the outage surfaces (unhealthy + last_error), no heal.
            ds.supervise_data_once()
            self.assertEqual(
                ds._pg_health.snapshot()["state"], "unhealthy"
            )
            self.assertIn("not running", self.pg.snapshot()["last_error"] or "")
            self.assertFalse(any(c[0] == "start" for c in calls))
            # Second pass heals: a start is attempted and accounting resets.
            ds.supervise_data_once()
        self.assertEqual(
            ds._pg_health.snapshot()["state"], "starting"
        )  # the unhealthy verdict described the dead proc, now restarted
        self.assertTrue(any(c[0] == "start" for c in calls))


class LiteLLMWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        # Same sandbox as SupervisorTests in test_litellm.py: the config write
        # must land somewhere writable and per-test isolated.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self._orig_paths = (
            litellm.MASTER_KEY_PATH,
            litellm.CONFIG_PATH,
            litellm.DATA_ROOT,
            litellm.APPLIED_HASH_PATH,
        )
        litellm.MASTER_KEY_PATH = root / "master-key"
        litellm.CONFIG_PATH = root / "config.yaml"
        litellm.DATA_ROOT = root
        litellm.APPLIED_HASH_PATH = root / "config.sha256"

        def restore() -> None:  # type: ignore[no-untyped-def]
            (
                litellm.MASTER_KEY_PATH,
                litellm.CONFIG_PATH,
                litellm.DATA_ROOT,
                litellm.APPLIED_HASH_PATH,
            ) = self._orig_paths

        self.addCleanup(restore)
        litellm._state.clear()
        litellm._state.update({litellm.LITELLM_CONTAINER: {"state": "starting"}})

    def _model(self):  # type: ignore[no-untyped-def]
        return mock.patch.object(litellm, "_active_model_id", return_value="m1")

    def _tick(self, *, probe_result) -> list[list[str]]:  # type: ignore[no-untyped-def]
        rec, calls = _docker_recorder()
        with (
            self._model(),
            mock.patch.object(litellm, "_docker", side_effect=rec),
            mock.patch.object(litellm, "_container_running", return_value=True),
            mock.patch.object(litellm, "_container_exists", return_value=True),
            mock.patch.object(litellm, "_port_open", return_value=True),
            # side_effect must be a callable: an iterable would feed the mock
            # one value per call instead of answering with this verdict.
            mock.patch.object(
                health, "probe_http",
                side_effect=lambda *a, **k: probe_result,
            ),
        ):
            litellm.supervise_litellm_once()
        return calls

    def test_readiness_failure_flips_unhealthy_then_recreates(self) -> None:
        fail = (False, "http 503 from /health/readiness: db: disconnected")
        for _ in range(3):
            self._tick(probe_result=fail)
        snap = litellm.litellm_snapshot()
        self.assertEqual(snap["state"], "unhealthy")
        self.assertIn("db: disconnected", snap["health"]["last_error"] or "")
        # Past grace, at the fail limit: the wedged proxy is recreated.
        litellm._state[litellm.LITELLM_CONTAINER]["since"] = time.monotonic() - 10_000
        calls = self._tick(probe_result=fail)
        self.assertTrue(any(c[:2] == ["rm", "-f"] for c in calls))


class ModuleHealthTests(unittest.TestCase):
    def test_spec_with_probe_passes_the_gate(self) -> None:
        spec = {"docker": {"probe": {"port": 18101, "path": "/healthz"}}}
        self.assertIsNone(mm._require_health_signal("m", spec, "img:1"))

    def test_spec_without_probe_needs_an_image_healthcheck(self) -> None:
        spec = {"docker": {}}
        with mock.patch.object(mm, "_image_has_healthcheck", return_value=False):
            verdict = mm._require_health_signal("m", spec, "img:1")
        self.assertIsNotNone(verdict)
        self.assertEqual(verdict[0], "fail")
        self.assertIn("no health signal", verdict[1])
        with mock.patch.object(mm, "_image_has_healthcheck", return_value=True):
            self.assertIsNone(mm._require_health_signal("m", spec, "img:1"))

    def test_deep_probe_without_any_signal_is_an_explicit_failure(self) -> None:
        ok, detail = mm._deep_probe({"docker": {}})
        self.assertFalse(ok)
        self.assertIn("no health signal", detail)

    def test_docker_healthcheck_wins_when_the_image_defines_one(self) -> None:
        with mock.patch.object(health, "probe_docker_health",
                               return_value=False) as dh:
            ok, detail = mm._deep_probe({"docker": {"probe": {"port": 1}}}, "c")
        self.assertFalse(ok)
        self.assertIn("HEALTHCHECK", detail)
        dh.assert_called_once_with("c")

    def test_snapshot_rows_carry_health(self) -> None:
        rows = [{"id": "mcp-gateway", "state": "enabled", "version": "0.1.0",
                 "error": None}]
        with (
            mock.patch.object(mm.db, "module_rows", return_value=rows),
            mock.patch.object(mm, "_op_state", return_value=None),
        ):
            snap = mm.snapshot()
        entry = snap["installed"][0]
        self.assertIn("health", entry)
        self.assertIn(entry["health"]["state"], ("starting", "ready", "unhealthy"))


if __name__ == "__main__":
    unittest.main()
