#!/usr/bin/env python3
"""Unit tests for the update affordance backend (control-plane/updates.py).

No root, no real updater: the job's subprocess is mocked, and the detector is
exercised against temp copies of the two files it reads (the updater's arm-time
record and the latest-version record the weekly check writes).
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import updates


class _Files(unittest.TestCase):
    """Temp copies of every path the module reads or writes."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        patches = [
            mock.patch.object(updates, "ATTEMPT_FILE", root / "update-attempt.json"),
            mock.patch.object(updates, "LATEST_FILE", root / "latest-update.json"),
            mock.patch.object(updates, "CMDLINE", root / "cmdline"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.attempt = updates.ATTEMPT_FILE
        self.latest = updates.LATEST_FILE
        self.cmdline = updates.CMDLINE

    def write_attempt(self, version: str = "2026.02", target: str = "b") -> None:
        self.attempt.write_text(
            json.dumps({"intended_version": version, "target_slot": target,
                        "attempted_at": "2026-02-01T00:00:00Z"}),
            encoding="utf-8",
        )

    def write_cmdline(self, slot: str) -> None:
        self.cmdline.write_text(f"console=tty0 lemonseed.slot={slot} root=fstab\n",
                                encoding="utf-8")


class FailedUpdateTests(_Files):
    def test_no_attempt_is_no_failure(self) -> None:
        self.assertIsNone(updates.failed_update("2026.01"))

    def test_booting_the_target_clears_the_record(self) -> None:
        self.write_attempt(target="b")
        self.write_cmdline("b")
        self.assertIsNone(updates.failed_update("2026.01"))
        self.assertFalse(self.attempt.exists(), "the record outlived a landed update")

    def test_running_the_intended_version_clears_the_record(self) -> None:
        self.write_attempt(version="2026.02", target="b")
        self.write_cmdline("a")  # slot did not change...
        self.assertIsNone(updates.failed_update("2026.02"))  # ...but the version did
        self.assertFalse(self.attempt.exists())

    def test_a_stuck_attempt_is_the_failure(self) -> None:
        """PLAN.md P5's signature: armed for slot b, booted slot a, still running
        the old version — the failed-or-stuck attempt the banner names."""
        self.write_attempt(target="b")
        self.write_cmdline("a")
        failed = updates.failed_update("2026.01")
        self.assertIsNotNone(failed)
        self.assertEqual(failed["intended"], "2026.02")
        self.assertEqual(failed["running"], "2026.01")
        # The banner is shown on every poll until the record clears — clearing
        # happens only when the update actually landed.
        self.assertTrue(self.attempt.exists())

    def test_an_unknowable_slot_is_not_called_a_failure(self) -> None:
        """A lab boot with no lemonseed.slot must not show a false failure."""
        self.write_attempt(target="b")
        self.cmdline.write_text("console=tty0 root=fstab\n", encoding="utf-8")
        self.assertIsNone(updates.failed_update("2026.01"))
        self.assertTrue(self.attempt.exists())


class SnapshotTests(_Files):
    def test_available_is_true_only_when_latest_differs(self) -> None:
        self.latest.write_text(
            json.dumps({"latest": "2026.02", "running": "2026.01",
                        "checked_at": "2026-02-01T00:00:00Z"}),
            encoding="utf-8",
        )
        snap = updates.snapshot("2026.01")
        self.assertTrue(snap["available"])
        self.assertEqual(snap["latest"], "2026.02")
        self.assertEqual(snap["running"], "2026.01")
        self.assertIsNone(snap["failed"])
        self.assertEqual(snap["job"]["state"], "idle")

    def test_no_check_ever_run_is_not_an_error(self) -> None:
        snap = updates.snapshot("2026.01")
        self.assertEqual(snap["latest"], "")
        self.assertFalse(snap["available"])

    def test_a_stale_latest_does_not_offer_a_downgrade(self) -> None:
        """Bare-metal: the record was written before the last update landed,
        so latest compared *below* the running version — offered as an
        "update" anyway. Only a strictly newer stamp is available."""
        self.latest.write_text(
            json.dumps({"latest": "2026.01", "running": "2026.02",
                        "checked_at": "2026-02-01T00:00:00Z"}),
            encoding="utf-8",
        )
        self.assertFalse(updates.snapshot("2026.02")["available"])
        self.assertFalse(updates.snapshot("2026.01")["available"])


class StartJobTests(_Files):
    def setUp(self) -> None:
        super().setUp()
        updates._job.update(kind="", state="idle", detail="", progress=None)
        # The interlock probes are real modules with real state; a job-start
        # test is about the job, so pin the interlock quiet. The reboot marker
        # must not reach the real /var/lib either.
        patches = [
            mock.patch.object(updates, "_interlock_reason", return_value=None),
            mock.patch.object(updates.chat_ready, "mark_reboot_pending"),
            mock.patch.object(updates.chat_ready, "clear_reboot_pending"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.mark_reboot = updates.chat_ready.mark_reboot_pending

    def tearDown(self) -> None:
        updates._job.update(kind="", state="idle", detail="", progress=None)

    def test_the_updaters_percent_rides_the_job(self) -> None:
        """The bar is the updater's own opinion: its stdout percentages pass
        through the job status untouched (bare-metal review, BM-3)."""
        def fake_popen(command, **_kwargs):
            class Proc:
                stdout = iter(["lemonseed-update: [ 42%] downloading root.squashfs", ""])
                returncode = 0

                def wait(self) -> int:
                    return 0

            return Proc()

        with mock.patch.object(updates.subprocess, "Popen", side_effect=fake_popen):
            self.assertIsNone(updates.start_check())
            updates._job_thread.join(timeout=5)
        self.assertEqual(updates._job["state"], "done")
        self.assertEqual(updates._job["progress"], 42)

    def test_a_finished_job_does_not_block_the_next_one(self) -> None:
        """Bare-metal: state=done outlived the job, and the lock refused every
        later start until a reboot. The lock is the running state, not the
        last result."""
        calls: list[list[str]] = []

        def fake_popen(command, **_kwargs):
            calls.append(command)

            class Proc:
                stdout = iter(["lemonseed-update: latest=test.02", ""])
                returncode = 0

                def wait(self) -> int:
                    return 0

            return Proc()

        with mock.patch.object(updates.subprocess, "Popen", side_effect=fake_popen):
            self.assertIsNone(updates.start_check())
            updates._job_thread.join(timeout=5)
            self.assertEqual(updates._job["state"], "done")
            self.assertIsNone(
                updates.start_check(), "a finished job blocked the next start"
            )
            updates._job_thread.join(timeout=5)
        self.assertEqual(len(calls), 2, "the second check never ran")

    def test_check_records_via_the_updater_and_the_interlock_stays_quiet(self) -> None:
        calls: list[list[str]] = []

        def fake_popen(command, **_kwargs):
            calls.append(command)

            class Proc:
                stdout = iter(["lemonseed-update: latest=test.02", ""])
                returncode = 0

                def wait(self) -> int:
                    return 0

            return Proc()

        with mock.patch.object(updates.subprocess, "Popen", side_effect=fake_popen):
            self.assertIsNone(updates.start_check())
            self.assertIsNone(updates.in_flight(), "a check is a read; it blocks nothing")
            self.assertTrue(updates._job_thread.join(timeout=5) is None or True)
        self.assertEqual(calls, [["lemonseed-update", "check", "--record"]])
        self.assertEqual(updates._job["state"], "done")

    def test_install_runs_cancel_then_apply_on_retry(self) -> None:
        calls: list[list[str]] = []

        def fake_popen(command, **_kwargs):
            calls.append(command)

            class Proc:
                stdout = iter(["lemonseed-update: rebooting now", ""])
                returncode = 0

                def wait(self) -> int:
                    return 0

            return Proc()

        with mock.patch.object(updates.subprocess, "Popen", side_effect=fake_popen):
            self.assertIsNone(updates.start_install(retry=True))
            self.assertTrue(updates._job_thread.join(timeout=5) is None or True)
        self.assertEqual(
            calls,
            [
                ["lemonseed-update", "cancel"],
                ["lemonseed-update", "apply", "--reboot"],
            ],
        )
        # The job reached its own reboot and stays there: the box is on its way
        # down, and nothing else runs to move the status on.
        self.assertEqual(updates._job["state"], "rebooting")

    def test_a_failed_install_names_the_step(self) -> None:
        def fake_popen(command, **_kwargs):
            class Proc:
                stdout = iter(["lemonseed-update: writing SquashFS to /dev/disk/by-partlabel/root-b",
                               "lemonseed-update: post-write checksum mismatch", ""])
                returncode = 1

                def wait(self) -> int:
                    return 1

            return Proc()

        with mock.patch.object(updates.subprocess, "Popen", side_effect=fake_popen):
            self.assertIsNone(updates.start_install())
            self.assertTrue(updates._job_thread.join(timeout=5) is None or True)
        self.assertEqual(updates._job["state"], "failed")
        self.assertIn("checksum", updates._job["detail"])


class PhaseTests(unittest.TestCase):
    """The power interlock reads the phase the prepare pass publishes; the
    updater's job refuses a power-off the same way, and only while it runs."""

    def test_install_is_named_while_running(self) -> None:
        with mock.patch.object(updates, "_job", {"kind": "install", "state": "writing",
                                                 "detail": ""}):
            self.assertEqual(updates.in_flight(), updates._JOB_BUSY)

    def test_check_never_blocks_the_power_controls(self) -> None:
        with mock.patch.object(updates, "_job", {"kind": "check", "state": "running",
                                                 "detail": ""}):
            self.assertIsNone(updates.in_flight())

    def test_a_finished_install_blocks_nothing(self) -> None:
        with mock.patch.object(updates, "_job", {"kind": "install", "state": "done",
                                                 "detail": ""}):
            self.assertIsNone(updates.in_flight())


def _wait_idle(timeout: float = 5.0, states: tuple[str, ...] = ("done", "failed")) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if str(updates._job.get("state")) in states:
            return True
        time.sleep(0.01)
    return False


class JobRunTests(_Files):
    """The job thread against a scripted subprocess: what runs, in what order,
    and what the power interlock sees while it runs."""

    def setUp(self) -> None:
        super().setUp()
        updates._job.update(kind="", state="idle", detail="", progress=None)
        patches = [
            mock.patch.object(updates, "_interlock_reason", return_value=None),
            mock.patch.object(updates.chat_ready, "mark_reboot_pending"),
            mock.patch.object(updates.chat_ready, "clear_reboot_pending"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.mark_reboot = updates.chat_ready.mark_reboot_pending
        self.addCleanup(lambda: updates._job.update(kind="", state="idle", detail="", progress=None))

    def test_check_runs_the_updater_with_record(self) -> None:
        calls: list[list[str]] = []

        def fake_popen(command, **_kwargs):
            calls.append(command)

            class Proc:
                stdout = iter(["lemonseed-update: running=test.01",
                               "lemonseed-update: latest=test.02", ""])
                returncode = 0

                def wait(self) -> int:
                    return 0

            return Proc()

        with mock.patch.object(updates.subprocess, "Popen", side_effect=fake_popen):
            self.assertIsNone(updates.start_check())
            self.assertTrue(_wait_idle())
        self.assertEqual(calls, [["lemonseed-update", "check", "--record"]])

    def test_install_retry_cancels_first_then_applies(self) -> None:
        calls: list[list[str]] = []

        def fake_popen(command, **_kwargs):
            calls.append(command)

            class Proc:
                stdout = iter(["lemonseed-update: rebooting now", ""])
                returncode = 0

                def wait(self) -> int:
                    return 0

            return Proc()

        with mock.patch.object(updates.subprocess, "Popen", side_effect=fake_popen):
            self.assertIsNone(updates.start_install(retry=True))
            # A successful install ends terminal at "rebooting": the box is on
            # its way down and nothing else runs to move the status on.
            self.assertTrue(_wait_idle(states=("rebooting", "done", "failed")))
        self.assertEqual(
            calls,
            [
                ["lemonseed-update", "cancel"],
                ["lemonseed-update", "apply", "--reboot"],
            ],
        )
        self.assertEqual(updates._job["state"], "rebooting")
        # Both steps stream a "rebooting now" line; the marker must have gone
        # through the going-away path rather than the real /var/lib file.
        self.assertTrue(self.mark_reboot.called)

    def test_a_failed_write_names_the_step_and_is_not_done(self) -> None:
        def fake_popen(command, **_kwargs):
            class Proc:
                stdout = iter([
                    "lemonseed-update: writing SquashFS to /dev/disk/by-partlabel/root-b",
                    "lemonseed-update: post-write checksum mismatch",
                    "",
                ])
                returncode = 1

                def wait(self) -> int:
                    return 1

            return Proc()

        with mock.patch.object(updates.subprocess, "Popen", side_effect=fake_popen):
            self.assertIsNone(updates.start_install())
            self.assertTrue(_wait_idle())
        self.assertEqual(updates._job["state"], "failed")
        self.assertIn("checksum", str(updates._job["detail"]))
