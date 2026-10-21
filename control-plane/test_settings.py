"""Unit tests for the settings wrapper (M8 P3, D-C4).

The release channel's authority is the ``cp_meta`` row; the file on /var is
its materialization for the DB-free root jobs, and the installer's kernel
command line stub is the seed for a fresh install. Every fallback here is
also a behavior the appliance depends on when Postgres is down.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import settings


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.file = Path(self._tmp.name) / "channel"
        patch = mock.patch.object(settings, "CHANNEL_FILE", self.file)
        patch.start()
        self.addCleanup(patch.stop)
        self.cmdline = Path(self._tmp.name) / "cmdline"
        patch = mock.patch.object(settings, "_CMDLINE", self.cmdline)
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_set_writes_the_table_and_the_file(self) -> None:
        with mock.patch.object(settings.db, "cp_set_meta", return_value=True) as meta:
            self.assertTrue(settings.set_setting("release_channel", "beta"))
        meta.assert_called_once_with("release_channel", "beta")
        self.assertEqual(self.file.read_text(encoding="utf-8").strip(), "beta")

    def test_unknown_names_and_values_are_refused(self) -> None:
        with mock.patch.object(settings.db, "cp_set_meta") as meta:
            self.assertFalse(settings.set_setting("release_channel", "nonsense"))
            self.assertFalse(settings.set_setting("who_knows", "beta"))
        meta.assert_not_called()

    def test_a_down_database_fails_the_set(self) -> None:
        with mock.patch.object(settings.db, "cp_set_meta", return_value=False):
            self.assertFalse(settings.set_setting("release_channel", "beta"))
        self.assertFalse(self.file.exists())

    def test_get_reads_the_table_first(self) -> None:
        with mock.patch.object(settings.db, "cp_get_meta", return_value="beta"):
            self.assertEqual(settings.get_setting("release_channel"), "beta")
        # A read never writes: adoption happens at the first set.
        self.assertFalse(self.file.exists())

    def test_get_adopts_the_seeded_file_when_the_table_is_empty(self) -> None:
        self.file.write_text("beta\n", encoding="utf-8")
        with mock.patch.object(settings.db, "cp_get_meta", return_value=None):
            self.assertEqual(settings.get_setting("release_channel"), "beta")

    def test_get_adopts_the_installer_cmdline_stub(self) -> None:
        # A fresh install carries the choice on the ESP stub, which the kernel
        # puts on the command line; the file does not exist yet.
        self.cmdline.write_text(
            "root=fstab init=/init lemonseed.slot=a lemonseed.channel=beta console=tty0\n",
            encoding="utf-8",
        )
        with mock.patch.object(settings.db, "cp_get_meta", return_value=None):
            self.assertEqual(settings.get_setting("release_channel"), "beta")

    def test_the_table_outranks_the_seeded_file(self) -> None:
        self.file.write_text("beta\n", encoding="utf-8")
        with mock.patch.object(settings.db, "cp_get_meta", return_value="stable"):
            self.assertEqual(settings.get_setting("release_channel"), "stable")

    def test_the_default_is_stable_when_nothing_was_seeded(self) -> None:
        with mock.patch.object(settings.db, "cp_get_meta", return_value=None):
            self.assertEqual(settings.get_setting("release_channel"), "stable")

    def test_a_garbage_file_never_bricks_the_reader(self) -> None:
        self.file.write_text("nonsense\n", encoding="utf-8")
        with mock.patch.object(settings.db, "cp_get_meta", return_value=None):
            self.assertEqual(settings.get_setting("release_channel"), "stable")


if __name__ == "__main__":
    unittest.main()
