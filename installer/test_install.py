#!/usr/bin/env python3
"""Unit tests for installer pipeline helpers (no root, no hardware)."""

from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
