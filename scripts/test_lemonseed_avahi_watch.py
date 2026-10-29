#!/usr/bin/env python3
"""Unit tests for lemonseed-avahi-watch: rename detection and reclaim policy.

The dbus module is unavailable (and unnecessary) for `make test`, so it is
stubbed out before import; only the pure decision logic and the restart
plumbing are exercised.
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

sys.modules.setdefault("dbus", types.ModuleType("dbus"))

import lemonseed_avahi_watch as watch  # noqa: E402


class DecideTestCase(unittest.TestCase):
    def test_matching_name_is_ok(self):
        self.assertEqual(watch.decide("lemonseed1", "lemonseed1", None, None), "ok")
        # GetHostName is non-fqdn, but tolerate a fully-qualified answer too.
        self.assertEqual(watch.decide("lemonseed1.local", "lemonseed1", None, None), "ok")

    def test_rename_with_free_name_restarts(self):
        self.assertEqual(watch.decide("lemonseed1-2", "lemonseed1", None, None), "restart")

    def test_rename_with_ghost_self_record_restarts(self):
        self.assertEqual(
            watch.decide("lemonseed1-2", "lemonseed1", "192.168.0.163", "192.168.0.163"),
            "restart",
        )

    def test_rename_owned_by_another_host_leaves_it(self):
        self.assertEqual(
            watch.decide("lemonseed1-2", "lemonseed1", "192.168.0.50", "192.168.0.163"),
            "leave",
        )


class RestartTestCase(unittest.TestCase):
    def test_restart_runs_systemctl_without_check(self):
        with mock.patch.object(watch.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            self.assertTrue(watch.restart_avahi())
        run.assert_called_once_with(
            ["systemctl", "restart", "avahi-daemon.service"], check=False
        )

    def test_restart_failure_is_reported(self):
        with mock.patch.object(watch.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1)
            self.assertFalse(watch.restart_avahi())


if __name__ == "__main__":
    unittest.main()
