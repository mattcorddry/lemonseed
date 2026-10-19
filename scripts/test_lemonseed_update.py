#!/usr/bin/env python3
"""Unit tests for lemonseed-update / lemonseed_artifacts (no root required)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lemonseed_artifacts as artifacts  # noqa: E402
import lemonseed_update as update  # noqa: E402


class ArtifactTests(unittest.TestCase):
    def test_sha256_file_and_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blob"
            data = b"lemonseed-ab-update-test\n" * 100
            path.write_bytes(data)
            digest = artifacts.sha256_file(path)
            self.assertEqual(digest, hashlib.sha256(data).hexdigest())
            self.assertEqual(artifacts.sha256_device(str(path), len(data)), digest)

    def test_acquire_local_manifest_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dest = Path(tmp) / "dl"
            src.mkdir()
            files = {
                "vmlinuz": b"kernel-bytes",
                "initrd.img": b"initrd-bytes",
                "root.squashfs": b"squash-bytes",
            }
            manifest = {
                "version": "test.01",
                "artifacts": {},
            }
            for name, body in files.items():
                (src / name).write_bytes(body)
                manifest["artifacts"][name] = {
                    "path": name,
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            (src / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            got, version, _ = artifacts.acquire_artifacts(
                download_dir=dest,
                directory=str(src),
            )
            self.assertEqual(version, "test.01")
            for name in files:
                self.assertTrue(got[name].is_file())

    def test_acquire_rejects_bad_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dest = Path(tmp) / "dl"
            src.mkdir()
            (src / "vmlinuz").write_bytes(b"kernel")
            (src / "initrd.img").write_bytes(b"initrd")
            (src / "root.squashfs").write_bytes(b"squash")
            (src / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": "bad",
                        "artifacts": {
                            "vmlinuz": {"path": "vmlinuz", "sha256": "00" * 32},
                            "initrd.img": {
                                "path": "initrd.img",
                                "sha256": hashlib.sha256(b"initrd").hexdigest(),
                            },
                            "root.squashfs": {
                                "path": "root.squashfs",
                                "sha256": hashlib.sha256(b"squash").hexdigest(),
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(artifacts.ArtifactError):
                artifacts.acquire_artifacts(download_dir=dest, directory=str(src))

    def test_main_exits_nonzero_on_checksum_failure(self) -> None:
        """A corrupt artifact must leave main() at exit 1 — the control plane
        turns that into the failed job state the panel's Retry/Cancel answers
        (bare-metal review, BM-4: a truncated push left the checksum failing
        forever until the file was re-fetched)."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            body = b"SQUASH" + b"\0" * 4096
            manifest = {
                "version": "bad",
                "artifacts": {
                    "vmlinuz": {"path": "vmlinuz"},
                    "initrd.img": {"path": "initrd.img"},
                    "root.squashfs": {"path": "root.squashfs", "sha256": "ff" * 32},
                },
            }
            (src / "vmlinuz").write_bytes(b"kernel")
            (src / "initrd.img").write_bytes(b"initrd")
            (src / "root.squashfs").write_bytes(body)
            (src / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            err = io.StringIO()
            # Slot plumbing is mocked so the assertion stays on the checksum:
            # the failure must come from the artifact, not the A/B preflight.
            # The download dir is pointed at the temp tree so the run never
            # touches /var/lib/lemonseed as a non-root user.
            dl = Path(tmp) / "dl"
            with mock.patch.object(update.os, "geteuid", return_value=0), \
                    mock.patch.object(update, "preflight"), \
                    mock.patch.object(update, "current_slot", return_value="a"), \
                    mock.patch.object(update, "inactive_slot", return_value="b"), \
                    mock.patch.object(update, "DOWNLOAD_DIR", dl), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(update.main(["apply", "--dir", str(src)]), 1)
            self.assertIn("Checksum mismatch for root.squashfs", err.getvalue())


class UpdateLogicTests(unittest.TestCase):
    def test_inactive_slot(self) -> None:
        self.assertEqual(update.inactive_slot("a"), "b")
        self.assertEqual(update.inactive_slot("b"), "a")

    def test_refuse_active_overwrite(self) -> None:
        with mock.patch.object(update, "refuse_dirty_state"), mock.patch.object(
            update, "current_slot", return_value="a"
        ):
            with self.assertRaises(update.UpdateError) as ctx:
                update.write_inactive_slot(
                    {
                        "root.squashfs": Path("/dev/null"),
                        "vmlinuz": Path("/dev/null"),
                        "initrd.img": Path("/dev/null"),
                    },
                    "a",
                    force=True,
                )
            self.assertIn("active slot", str(ctx.exception))

    def test_refuse_dirty_without_force(self) -> None:
        with mock.patch.object(
            update, "ab_status", return_value={"boot_once": "b", "boot_attempted": ""}
        ):
            with self.assertRaises(update.UpdateError):
                update.refuse_dirty_state(force=False)
            update.refuse_dirty_state(force=True)

    def test_cmdline_slot(self) -> None:
        with mock.patch("lemonseed_update.Path") as mocked_path:
            instance = mocked_path.return_value
            instance.is_file.return_value = True
            instance.read_text.return_value = "console=tty0 lemonseed.slot=b root=fstab"
            self.assertEqual(update.cmdline_slot(), "b")

    def test_preflight_refuses_an_unknowable_slot(self) -> None:
        """No lemonseed.slot and no boot_default: 'the inactive slot' is a
        guess, and a wrong guess overwrites the running root."""
        with mock.patch.object(update, "cmdline_slot", return_value=None), mock.patch.object(
            update, "ab_status", return_value={}
        ), mock.patch.object(update, "ATTEMPT_FILE", Path("/nonexistent/attempt.json")):
            with self.assertRaises(update.UpdateError) as ctx:
                update.preflight(force=False)
            self.assertIn("slot is active", str(ctx.exception))

    def test_preflight_refuses_a_leftover_attempt_record(self) -> None:
        """An arm-time record nobody cleared means a previous attempt never
        landed; bare apply would stack a second attempt on top of it."""
        with tempfile.TemporaryDirectory() as tmp:
            attempt = Path(tmp) / "update-attempt.json"
            attempt.write_text("{}", encoding="utf-8")
            with mock.patch.object(update, "cmdline_slot", return_value="a"), mock.patch.object(
                update, "ab_status", return_value={"boot_default": "a"}
            ), mock.patch.object(update, "ATTEMPT_FILE", attempt):
                with self.assertRaises(update.UpdateError) as ctx:
                    update.preflight(force=False)
                self.assertIn("cancel", str(ctx.exception))
                # --force is the operator's escape hatch, as the message says.
                update.preflight(force=True)


class UpdateApplyTests(unittest.TestCase):
    def test_apply_writes_inactive_and_sets_boot_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            arts = root / "arts"
            arts.mkdir()
            bodies = {
                "vmlinuz": b"kernel-b",
                "initrd.img": b"initrd-b",
                "root.squashfs": b"SQUASH-B" + b"\0" * 4096,
            }
            manifest = {"version": "u1", "artifacts": {}}
            for name, body in bodies.items():
                (arts / name).write_bytes(body)
                manifest["artifacts"][name] = {
                    "path": name,
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            (arts / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

            root_a = root / "root-a"
            root_b = root / "root-b"
            root_a.write_bytes(b"OLD-A" + b"\0" * 8192)
            root_b.write_bytes(b"\0" * 8192)
            esp_dir = root / "esp"
            (esp_dir / "EFI" / "lemonseed").mkdir(parents=True)
            (esp_dir / "grub").mkdir(parents=True)
            # Minimal grubenv via helper script path
            ab = root / "ab.sh"
            ab.write_text(
                "#!/bin/bash\n"
                "set -euo pipefail\n"
                "case \"$1\" in\n"
                "  status) echo boot_default=a; echo boot_once=; echo boot_attempted= ;;\n"
                "  set-once) echo set boot_once=$2; printf '%s' \"$2\" > \"$(dirname \"$0\")/once\" ;;\n"
                "  *) exit 1 ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            ab.chmod(0o755)

            def fake_part(label: str) -> str:
                mapping = {
                    "root-a": str(root_a),
                    "root-b": str(root_b),
                    "ESP": str(root / "esp-part"),
                }
                return mapping[label]

            (root / "esp-part").write_bytes(b"fat")

            # The updater refuses to arm a slot when no grub.cfg template
            # exists (bare-metal review); give it the real one.
            template = root / "grub.cfg.template"
            template.write_text("set default=0\n", encoding="utf-8")

            def fake_run(cmd, check=True, text=True, stdout=None, stderr=None):
                if cmd[:1] == ["blockdev"] and "--getsize64" in cmd:
                    path = Path(cmd[-1])
                    return subprocess.CompletedProcess(cmd, 0, str(path.stat().st_size), "")
                if cmd[0] == str(ab) or str(cmd[0]).endswith("ab.sh"):
                    return subprocess.run(
                        cmd, check=check, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
                    )
                raise AssertionError(f"unexpected command: {cmd}")

            with mock.patch.object(update, "AB_HELPER", str(ab)), mock.patch.object(
                update, "current_slot", return_value="a"
            ), mock.patch.object(update, "part_path", side_effect=fake_part), mock.patch.object(
                update, "run", side_effect=fake_run
            ), mock.patch.object(
                update, "DOWNLOAD_DIR", root / "dl"
            ), mock.patch.object(
                update, "ESP_MOUNT", esp_dir
            ), mock.patch.object(
                update, "ensure_esp_mounted", return_value=esp_dir
            ), mock.patch.object(
                update, "umount_esp"
            ), mock.patch.object(
                update, "require_root"
            ), mock.patch.object(
                update, "GRUB_CFG", template
            ), mock.patch.object(
                update, "GRUB_CFG_FALLBACK", root / "no-such-fallback.cfg"
            ), mock.patch.object(
                update, "ATTEMPT_FILE", root / "update-attempt.json"
            ), mock.patch.object(
                artifacts.subprocess,
                "check_output",
                side_effect=lambda cmd, text=True: str(Path(cmd[-1]).stat().st_size),
            ):
                (esp_dir / "EFI" / "lemonseed").mkdir(parents=True, exist_ok=True)
                rc = update.cmd_apply(None, str(arts), reboot=False, force=False)
                self.assertEqual(rc, 0)
                written = root_b.read_bytes()[: len(bodies["root.squashfs"])]
                self.assertEqual(written, bodies["root.squashfs"])
                self.assertEqual(
                    (esp_dir / "EFI" / "lemonseed" / "vmlinuz-b").read_bytes(), bodies["vmlinuz"]
                )
                self.assertEqual((root / "once").read_text(encoding="utf-8"), "b")
                self.assertTrue(root_a.read_bytes().startswith(b"OLD-A"))
                # The ESP grub.cfg came from the template, and the failed-update
                # detector's input was written at arm time.
                self.assertEqual(
                    (esp_dir / "grub" / "grub.cfg").read_text(encoding="utf-8"),
                    "set default=0\n",
                )
                attempt = json.loads((root / "update-attempt.json").read_text(encoding="utf-8"))
                self.assertEqual(attempt["intended_version"], "u1")
                self.assertEqual(attempt["target_slot"], "b")
                self.assertIn("attempted_at", attempt)

    def test_apply_refuses_the_version_already_running(self) -> None:
        """Reinstalling the running version is not an update; refuse it with
        the reason rather than silently rewriting the slot (BM-3, --force
        remains the CLI override)."""
        with mock.patch.object(update, "require_root"), mock.patch.object(
            update, "preflight"
        ), mock.patch.object(update, "current_slot", return_value="a"), mock.patch.object(
            update, "acquire_artifacts", return_value=({}, "2026.02", {})
        ), mock.patch.object(update, "running_version", return_value="2026.02"):
            with self.assertRaises(update.UpdateError) as ctx:
                update.cmd_apply(None, None, reboot=False, force=False)
            self.assertIn("already running", str(ctx.exception))

    def test_apply_announces_finish_and_grace_before_reboot(self) -> None:
        """The panel polls every 2s; without a 100% line and a short grace the
        reboot outruns the poll and the dialog freezes at the last write
        percent (bare-metal review, BM-4)."""
        finished: list[str] = []
        slept: list[float] = []
        reboots: list[list[str]] = []
        files = {
            "vmlinuz": Path("/nonexistent/vmlinuz"),
            "initrd.img": Path("/nonexistent/initrd.img"),
            "root.squashfs": Path("/nonexistent/root.squashfs"),
        }
        err = io.StringIO()
        with mock.patch.object(update, "require_root"), \
                mock.patch.object(update, "preflight"), \
                mock.patch.object(update, "current_slot", return_value="a"), \
                mock.patch.object(update, "inactive_slot", return_value="b"), \
                mock.patch.object(update, "acquire_artifacts",
                                  return_value=(files, "v9", {})), \
                mock.patch.object(update, "running_version", return_value="v8"), \
                mock.patch.object(update, "write_inactive_slot"), \
                mock.patch.object(update, "record_attempt"), \
                mock.patch.object(update, "REBOOT_GRACE_S", 0.05), \
                mock.patch.object(update, "run",
                                  side_effect=lambda cmd, **k: reboots.append(cmd)), \
                mock.patch.object(update.time, "sleep",
                                  side_effect=lambda s: slept.append(s)):
            with contextlib.redirect_stdout(err):
                self.assertEqual(update.cmd_apply(url=None, directory=None,
                                                  reboot=True, force=False), 0)
        finished = [ln for ln in err.getvalue().splitlines() if "100%" in ln]
        self.assertTrue(any("Update finished" in ln for ln in finished), err.getvalue())
        self.assertEqual(slept, [0.05])
        self.assertEqual(reboots, [["systemctl", "reboot"]])


class UpdateCheckTests(unittest.TestCase):
    def test_check_reports_and_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            latest_file = Path(tmp) / "latest-update.json"
            with mock.patch.object(
                update,
                "fetch_manifest",
                return_value=({"version": "test.02"}, "http://base/"),
            ), mock.patch.object(update, "running_version", return_value="test.01"), mock.patch.object(
                update, "LATEST_FILE", latest_file
            ):
                rc = update.cmd_check(None, record=True)
                self.assertEqual(rc, 0)
            self.assertTrue(latest_file.is_file())
            record = json.loads(latest_file.read_text(encoding="utf-8"))
            self.assertEqual(record["latest"], "test.02")
            self.assertEqual(record["running"], "test.01")
            self.assertIn("checked_at", record)

    def test_cancel_clears_the_attempt_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attempt = Path(tmp) / "update-attempt.json"
            attempt.write_text("{}", encoding="utf-8")
            esp_dir = Path(tmp) / "esp"
            (esp_dir / "grub").mkdir(parents=True)
            (esp_dir / "grub" / "grubenv").write_bytes(b"env")
            with mock.patch.object(update, "ATTEMPT_FILE", attempt), mock.patch.object(
                update, "ESP_MOUNT", esp_dir
            ), mock.patch.object(update, "ensure_esp_mounted", return_value=esp_dir), mock.patch.object(
                update, "umount_esp"
            ), mock.patch.object(update, "run"), mock.patch.object(update, "require_root"):
                update.cmd_cancel()
            self.assertFalse(attempt.exists())


if __name__ == "__main__":
    raise SystemExit(unittest.main())
