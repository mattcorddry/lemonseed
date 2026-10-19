#!/usr/bin/env python3
"""Unit tests for installer pipeline helpers (no root, no hardware)."""

from __future__ import annotations

import contextlib
import inspect
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import syslog  # noqa: E402

import install  # noqa: E402
import server  # noqa: E402


def fake_mount(root: Path, error: Exception | None = None):
    """Stand in for install.mount_user_data: hand out `root` instead of mounting."""

    @contextlib.contextmanager
    def _mount(disk: str, read_only: bool = False):  # signature parity with the real helper
        if error is not None:
            raise error
        yield root

    return _mount


def user_data_root() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    """A tmp directory shaped like a mounted user-data volume (its @var)."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    (root / "@var" / "lib" / "lemonseed").mkdir(parents=True)
    return tmp, root


class LogTests(unittest.TestCase):
    def test_log_writes_to_file_and_syslog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "installer.log"
            with (
                mock.patch.object(install, "INSTALL_LOG", log_path),
                mock.patch.object(syslog, "syslog") as m_syslog,
            ):
                install._log(syslog.LOG_INFO, "hello world")
            self.assertTrue(log_path.is_file())
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("hello world", content)
            self.assertRegex(content, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z hello world$")
            m_syslog.assert_called_once_with(syslog.LOG_INFO, content.strip())

    def test_log_survives_missing_syslog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "installer.log"
            with (
                mock.patch.object(install, "INSTALL_LOG", log_path),
                mock.patch.object(syslog, "syslog", side_effect=OSError("no /dev/log")),
            ):
                install._log(syslog.LOG_ERR, "still logged")
            self.assertIn("still logged", log_path.read_text(encoding="utf-8"))


class RunTests(unittest.TestCase):
    def test_run_mirrors_output_to_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "installer.log"
            completed = mock.Mock(stdout="line one\nline two\n", stderr="", returncode=0)
            with (
                mock.patch.object(install, "INSTALL_LOG", log_path),
                mock.patch.object(syslog, "syslog"),
                mock.patch.object(install.subprocess, "run", return_value=completed) as m_run,
            ):
                result = install._run(["bash", "/etc/lemonseed/disk-repart.sh", "--wipe", "/dev/sda"])
            self.assertIs(result, completed)
            m_run.assert_called_once()
            kwargs = m_run.call_args.kwargs
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            content = log_path.read_text(encoding="utf-8")
            self.assertIn("$ bash /etc/lemonseed/disk-repart.sh --wipe /dev/sda: line one", content)
            self.assertIn("line two", content)


def patch_install_log(case: unittest.TestCase) -> None:
    """Keep a unit test from appending to the host's /var/lib/lemonseed."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    patcher = mock.patch.object(install, "INSTALL_LOG", Path(tmp.name) / "installer.log")
    patcher.start()
    case.addCleanup(patcher.stop)


class ResetMarkerTests(unittest.TestCase):
    """D-27: the marker lands where auth.py reads it, or the install says it failed."""

    def setUp(self) -> None:
        patch_install_log(self)
        tmp, self.root = user_data_root()
        self.addCleanup(tmp.cleanup)
        self.marker = self.root / "@var" / "lib" / "lemonseed" / "reset-admin"

    def test_marker_destination_matches_the_control_plane(self) -> None:
        # @var is the appliance's /var, so this must be its reset-admin file.
        self.assertEqual(
            str(install.USER_DATA_MOUNT / install.RESET_MARKER_REL),
            "/run/lemonseed-install-user-data/@var/lib/lemonseed/reset-admin",
        )

    def test_marker_written_into_user_data_when_requested(self) -> None:
        with mock.patch.object(install, "mount_user_data", fake_mount(self.root)):
            install.apply_reset_marker("/dev/sda", True)
        self.assertTrue(self.marker.is_file())
        self.assertEqual(self.marker.read_text(encoding="utf-8"), "1\n")

    def test_stale_marker_deleted_when_not_requested(self) -> None:
        self.marker.write_text("1\n", encoding="utf-8")
        with mock.patch.object(install, "mount_user_data", fake_mount(self.root)):
            install.apply_reset_marker("/dev/sda", False)
        self.assertFalse(self.marker.exists())

    def test_stale_marker_removal_is_best_effort(self) -> None:
        self.marker.write_text("1\n", encoding="utf-8")
        with (
            mock.patch.object(install, "mount_user_data", fake_mount(self.root)),
            mock.patch.object(install.Path, "unlink", side_effect=OSError("busy")),
            mock.patch.object(install, "_log"),
        ):
            install.apply_reset_marker("/dev/sda", False)

    def test_unreadable_volume_tolerated_when_nothing_was_requested(self) -> None:
        gone = install.InstallError("No Lemonseed user-data partition found on /dev/sda")
        with (
            mock.patch.object(install, "mount_user_data", fake_mount(self.root, error=gone)),
            mock.patch.object(install, "_log"),
        ):
            install.apply_reset_marker("/dev/sda", False)

    def test_marker_failure_is_reported_not_swallowed(self) -> None:
        broken = install.InstallError("Could not mount /dev/sda4: unknown filesystem type")
        with mock.patch.object(install, "mount_user_data", fake_mount(self.root, error=broken)):
            with self.assertRaises(install.InstallError) as caught:
                install.apply_reset_marker("/dev/sda", True)
        message = str(caught.exception)
        self.assertIn("admin password was not reset", message)
        self.assertIn("unknown filesystem type", message)

    def test_marker_write_error_is_reported(self) -> None:
        with (
            mock.patch.object(install, "mount_user_data", fake_mount(self.root)),
            mock.patch.object(install.Path, "write_text", side_effect=OSError("Read-only file system")),
        ):
            with self.assertRaises(install.InstallError) as caught:
                install.apply_reset_marker("/dev/sda", True)
        self.assertIn("Read-only file system", str(caught.exception))
        self.assertFalse(self.marker.exists())

    def test_esp_is_no_longer_part_of_the_handoff(self) -> None:
        # F-2: the ESP marker had no reader, and the unit its comment named
        # never existed. Neither the write nor that claim may come back.
        source = Path(install.__file__).read_text(encoding="utf-8")
        self.assertNotIn("lemonseed-reset-admin", source)
        self.assertNotIn("lemonseed-auth-reset-seed", source)
        self.assertNotIn("reset_admin_credentials", inspect.signature(install.seed_esp).parameters)


class UserDataMountTests(unittest.TestCase):
    def setUp(self) -> None:
        patch_install_log(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.mount = Path(tmp.name) / "user-data"
        self.ok = mock.Mock(returncode=0, stdout="", stderr="")

    def test_lv_is_preferred_over_the_raw_partition(self) -> None:
        parts = {"ESP": "/dev/sda1", "lemonseed-user-data": "/dev/sda4"}
        with (
            mock.patch.object(install, "parts_on_disk", return_value=parts),
            mock.patch.object(install.subprocess, "run") as m_run,
            mock.patch.object(
                install.Path, "exists", lambda self: str(self) == "/dev/appliance/user-data"
            ),
        ):
            self.assertEqual(install.user_data_device("/dev/sda"), "/dev/appliance/user-data")
        m_run.assert_called_once_with(
            ["vgchange", "-ay", "appliance"], check=False, capture_output=True
        )

    def test_bare_partition_is_the_fallback_when_there_is_no_lv(self) -> None:
        with (
            mock.patch.object(install, "parts_on_disk", return_value={"lemonseed-user-data": "/dev/sda4"}),
            mock.patch.object(install.subprocess, "run"),
            mock.patch.object(install.Path, "exists", lambda self: False),
        ):
            self.assertEqual(install.user_data_device("/dev/sda"), "/dev/sda4")

    def test_no_user_data_partition_returns_none(self) -> None:
        with mock.patch.object(install, "parts_on_disk", return_value={"ESP": "/dev/sda1"}):
            self.assertIsNone(install.user_data_device("/dev/sda"))

    def test_probe_mounts_read_only_then_hands_the_volume_back(self) -> None:
        with (
            mock.patch.object(install, "USER_DATA_MOUNT", self.mount),
            mock.patch.object(install, "user_data_device", return_value="/dev/appliance/user-data"),
            mock.patch.object(install.os.path, "ismount", return_value=False),
            mock.patch.object(install.subprocess, "run", return_value=self.ok) as m_run,
        ):
            with install.mount_user_data("/dev/sda", read_only=True) as root:
                self.assertEqual(root, self.mount)
        argvs = [call.args[0] for call in m_run.call_args_list]
        self.assertIn(
            [
                "mount",
                "-t",
                "btrfs",
                "-o",
                "ro,subvolid=5",
                "/dev/appliance/user-data",
                str(self.mount),
            ],
            argvs,
        )
        self.assertIn(["umount", str(self.mount)], argvs)
        self.assertIn(["vgchange", "-an", "appliance"], argvs)

    def test_existing_mount_is_reused_not_stacked(self) -> None:
        with (
            mock.patch.object(install, "USER_DATA_MOUNT", self.mount),
            mock.patch.object(install, "user_data_device", return_value="/dev/appliance/user-data"),
            mock.patch.object(install.os.path, "ismount", return_value=True),
            mock.patch.object(install.subprocess, "run", return_value=self.ok) as m_run,
        ):
            with install.mount_user_data("/dev/sda") as root:
                self.assertEqual(root, self.mount)
        m_run.assert_not_called()

    def test_failed_mount_raises_instead_of_yielding(self) -> None:
        failed = mock.Mock(
            returncode=32,
            stdout="",
            stderr="mount: /run/x: unknown filesystem type 'LVM2_member'",
        )
        with (
            mock.patch.object(install, "USER_DATA_MOUNT", self.mount),
            mock.patch.object(install, "user_data_device", return_value="/dev/sda4"),
            mock.patch.object(install.os.path, "ismount", return_value=False),
            mock.patch.object(install.subprocess, "run", return_value=failed),
        ):
            with self.assertRaises(install.InstallError) as caught:
                with install.mount_user_data("/dev/sda"):
                    self.fail("a failed mount must not yield")
        self.assertIn("LVM2_member", str(caught.exception))

    def test_missing_partition_raises_instead_of_yielding(self) -> None:
        with (
            mock.patch.object(install, "USER_DATA_MOUNT", self.mount),
            mock.patch.object(install, "user_data_device", return_value=None),
        ):
            with self.assertRaises(install.InstallError):
                with install.mount_user_data("/dev/sda"):
                    self.fail("a missing volume must not yield")


class AdminRosterTests(unittest.TestCase):
    """D-28: the installer reads the CP's roster, and stays quiet without it."""

    def setUp(self) -> None:
        patch_install_log(self)
        tmp, self.root = user_data_root()
        self.addCleanup(tmp.cleanup)
        self.roster = self.root / "@var" / "lib" / "lemonseed" / "admins.json"

    def write_roster(self, text: str) -> None:
        self.roster.write_text(text, encoding="utf-8")

    def test_present_roster_returns_the_usernames(self) -> None:
        self.write_roster('{"admins": ["alice", "bob"]}\n')
        with mock.patch.object(install, "mount_user_data", fake_mount(self.root)):
            self.assertEqual(install.read_admin_roster("/dev/sda"), ["alice", "bob"])

    def test_missing_roster_is_unknown_not_an_error(self) -> None:
        with mock.patch.object(install, "mount_user_data", fake_mount(self.root)):
            self.assertIsNone(install.read_admin_roster("/dev/sda"))

    def test_malformed_roster_is_unknown(self) -> None:
        self.write_roster('{"admins": ["alice", ')
        with mock.patch.object(install, "mount_user_data", fake_mount(self.root)):
            self.assertIsNone(install.read_admin_roster("/dev/sda"))

    def test_unrecognised_shape_is_unknown(self) -> None:
        self.assertIsNone(install.parse_admin_roster('{"admins": "alice"}'))
        self.assertIsNone(install.parse_admin_roster('["alice"]'))
        self.assertIsNone(install.parse_admin_roster(""))

    def test_unmountable_disk_is_unknown(self) -> None:
        gone = install.InstallError("Could not mount /dev/sda4: mount failed")
        with mock.patch.object(install, "mount_user_data", fake_mount(self.root, error=gone)):
            self.assertIsNone(install.read_admin_roster("/dev/sda"))

    def test_roster_is_read_read_only(self) -> None:
        self.write_roster('{"admins": ["alice"]}')
        seen = {}

        @contextlib.contextmanager
        def spy(disk: str, read_only: bool = False):
            seen["read_only"] = read_only
            yield self.root

        with mock.patch.object(install, "mount_user_data", spy):
            install.read_admin_roster("/dev/sda")
        self.assertTrue(seen["read_only"])

    def test_only_preservable_disks_are_probed(self) -> None:
        self.write_roster('{"admins": ["alice", "bob"]}')
        with mock.patch.object(install, "mount_user_data", fake_mount(self.root)):
            disks = install.attach_admin_rosters(
                [
                    {
                        "path": "/dev/sda",
                        "eligible": True,
                        "preservable_user_data": {"storage_uuid": "u"},
                    },
                    {"path": "/dev/sdb", "eligible": True, "preservable_user_data": None},
                    {
                        "path": "/dev/sdc",
                        "eligible": False,
                        "preservable_user_data": {"storage_uuid": "u"},
                    },
                ]
            )
        self.assertEqual(disks[0]["admin_usernames"], ["alice", "bob"])
        self.assertIsNone(disks[1]["admin_usernames"])
        self.assertIsNone(disks[2]["admin_usernames"])


class ResetAdminCopyTests(unittest.TestCase):
    def test_two_or_more_admins_are_counted_and_named(self) -> None:
        copy = server.reset_admin_copy(["alice", "bob"])
        self.assertIn("2 accounts", copy)
        self.assertIn("alice, bob", copy)
        self.assertIn("chats and files are kept", copy)
        self.assertEqual(
            server.reset_admin_copy(["ana", "bo", "cai"]).split(":")[1],
            " ana, bo, cai. Their chats and files are kept.",
        )

    def test_single_admin_keeps_the_existing_wording(self) -> None:
        self.assertEqual(server.reset_admin_copy(["alice"]), server.RESET_ADMIN_LINE)
        self.assertNotIn("alice", server.RESET_ADMIN_LINE)

    def test_unknown_roster_keeps_the_generic_sentence(self) -> None:
        self.assertEqual(server.reset_admin_copy(None), server.RESET_ADMIN_LINE)
        self.assertEqual(server.reset_admin_copy([]), server.RESET_ADMIN_LINE)

    def test_copy_stays_plain_language(self) -> None:
        sentences = [server.reset_admin_copy(["alice", "bob"]), server.RESET_ADMIN_LINE]
        for sentence in sentences:
            lowered = sentence.lower()
            for word in ("srp", "verifier", "hash", "password"):
                self.assertNotIn(word, lowered)

    def test_row_ids_and_post_wiring_survive(self) -> None:
        for token in ('id="reset-admin-row"', 'id="reset-admin"', 'id="reset-admin-copy"'):
            self.assertIn(token, server.INDEX_HTML)
        app_js = (server.STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn('getElementById("reset-admin")', app_js)
        self.assertIn("reset_admin_credentials", app_js)


class InstallWiringTests(unittest.TestCase):
    """run_install must hand the reset intent to user-data, not to the ESP."""

    def setUp(self) -> None:
        patch_install_log(self)

    def drive_install(self, stack: contextlib.ExitStack, preserve: bool, reset: bool) -> mock.Mock:
        files = {"root.squashfs": Path("/nonexistent/root.squashfs")}
        stack.enter_context(mock.patch.object(install, "require_host_ready"))
        stack.enter_context(
            mock.patch.object(
                install,
                "_eligible",
                return_value={
                    "path": "/dev/sda",
                    "preservable_user_data": {"storage_uuid": "u"},
                },
            )
        )
        stack.enter_context(mock.patch.object(install, "acquire_artifacts", return_value=(files, "9.9.9")))
        stack.enter_context(mock.patch.object(install, "partition_disk"))
        stack.enter_context(mock.patch.object(install, "wait_part", return_value="/dev/sda2"))
        stack.enter_context(mock.patch.object(install, "write_raw"))
        stack.enter_context(mock.patch.object(install, "seed_esp"))
        stack.enter_context(mock.patch.object(install, "parts_on_disk", return_value={}))
        stack.enter_context(mock.patch.object(install.time, "sleep"))
        stack.enter_context(mock.patch.object(install.subprocess, "run"))
        marker = stack.enter_context(mock.patch.object(install, "apply_reset_marker"))
        install.run_install("/dev/sda", preserve_user_data=preserve, reset_admin_credentials=reset)
        return marker

    def test_preserve_install_states_the_reset_intent(self) -> None:
        with contextlib.ExitStack() as stack:
            marker = self.drive_install(stack, preserve=True, reset=True)
        marker.assert_called_once_with("/dev/sda", True)

    def test_preserve_install_also_clears_a_stale_marker(self) -> None:
        with contextlib.ExitStack() as stack:
            marker = self.drive_install(stack, preserve=True, reset=False)
        marker.assert_called_once_with("/dev/sda", False)

    def test_wipe_install_never_writes_a_marker(self) -> None:
        with contextlib.ExitStack() as stack:
            marker = self.drive_install(stack, preserve=False, reset=True)
        marker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
