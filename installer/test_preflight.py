#!/usr/bin/env python3
"""Unit tests for installer host preflight (no root, no hardware)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import disks  # noqa: E402
import preflight  # noqa: E402


class PreflightTests(unittest.TestCase):
    def test_eight_gb_class_memtotal_passes(self) -> None:
        # Typical "8 GB" box after BIOS/GPU reservations (user-reported example).
        result = preflight.evaluate_host(
            ram_total_kb=8_065_332, ram_available_gb=7.25, cores=4
        )
        self.assertTrue(result["ram_ok"])
        self.assertTrue(result["cores_ok"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["reasons"], [])

    def test_memtotal_just_below_floor_fails(self) -> None:
        result = preflight.evaluate_host(
            ram_total_kb=preflight.MIN_MEMTOTAL_KB - 1,
            ram_available_gb=6.0,
            cores=8,
        )
        self.assertFalse(result["ram_ok"])
        self.assertFalse(result["ok"])
        self.assertTrue(any("8 GB of memory" in r for r in result["reasons"]))

    def test_low_ram_fails_with_friendly_copy(self) -> None:
        result = preflight.evaluate_host(ram_total_gb=4.0, cores=8)
        self.assertFalse(result["ok"])
        self.assertIn("about 4.0 GB", result["reasons"][0])

    def test_too_few_cores_fails(self) -> None:
        result = preflight.evaluate_host(
            ram_total_gb=16.0, ram_available_gb=12.0, cores=2
        )
        self.assertFalse(result["ok"])
        self.assertTrue(result["ram_ok"])
        self.assertFalse(result["cores_ok"])
        self.assertTrue(any("processor cores" in r for r in result["reasons"]))

    def test_cpu_path_ok(self) -> None:
        result = preflight.evaluate_host(
            ram_total_gb=16.0, ram_available_gb=12.0, cores=8
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["ram_ok"])
        self.assertTrue(result["cores_ok"])

    def test_require_raises(self) -> None:
        with mock.patch.object(
            preflight,
            "evaluate_host",
            return_value={"ok": False, "reasons": ["not enough RAM"]},
        ):
            from install import InstallError

            with self.assertRaises(InstallError):
                preflight.require_host_ready()


class EncryptionOfferTests(unittest.TestCase):
    """D-E1: the checkbox state follows from a usable TPM, not from presence
    of a sysfs node — and the no-TPM copy is the exact sentence users see."""

    def test_a_usable_tpm_offers_the_checkbox_ticked_and_untickable(self) -> None:
        # §4.1: checked by default; the user may untick it (a VM whose host
        # volume is already encrypted), so it stays enabled, never forced.
        offer = disks.encryption_offer(True)
        self.assertTrue(offer["available"])
        self.assertTrue(offer["enabled"])
        self.assertTrue(offer["checked"])
        self.assertEqual(offer["warning"], "")

    def test_no_tpm_greys_the_checkbox_out_with_the_exact_warning(self) -> None:
        offer = disks.encryption_offer(False)
        self.assertFalse(offer["available"])
        self.assertFalse(offer["enabled"])
        self.assertFalse(offer["checked"])
        self.assertEqual(
            offer["warning"],
            "This PC has no trusted platform module (TPM), so Lemonseed cannot "
            "encrypt the data storage drive. You can still install in "
            "unencrypted mode.",
        )

    def test_the_trial_probes_the_resource_manager_not_just_sysfs(self) -> None:
        # /sys/class/tpm/tpm0 alone is not enough: a BIOS-disabled TPM still
        # shows it on some firmware. The probe must open /dev/tpmrm0.
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Path(tmp) / "tpm0"
            rm = Path(tmp) / "tpmrm0"
            sysfs.mkdir()
            rm.write_text("")
            with (
                mock.patch.object(disks, "TPM_SYSFS", sysfs),
                mock.patch.object(disks, "TPM_RM", rm),
                mock.patch.object(disks.os, "open", return_value=7) as m_open,
                mock.patch.object(disks.os, "close") as m_close,
            ):
                self.assertTrue(disks.tpm2_available())
            m_open.assert_called_once_with(rm, mock.ANY)
            m_close.assert_called_once_with(7)

    def test_an_unopenable_resource_manager_is_no_tpm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = Path(tmp) / "tpm0"
            rm = Path(tmp) / "tpmrm0"
            sysfs.mkdir()
            rm.write_text("")
            with (
                mock.patch.object(disks, "TPM_SYSFS", sysfs),
                mock.patch.object(disks, "TPM_RM", rm),
                mock.patch.object(disks.os, "open", side_effect=OSError("busy")),
            ):
                self.assertFalse(disks.tpm2_available())

    def test_absent_tpm_fails_without_touching_the_device(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(disks, "TPM_SYSFS", Path(tmp)),
                mock.patch.object(disks, "TPM_RM", Path(tmp) / "tpmrm0"),
                mock.patch.object(disks.os, "open") as m_open,
            ):
                self.assertFalse(disks.tpm2_available())
            m_open.assert_not_called()


class LuksTrialTests(unittest.TestCase):
    """Trial unlocks answer 'does the seal work?' and never hold the volume:
    both helpers close the throwaway mapper whatever the outcome."""

    def test_token_unlock_opens_then_closes(self) -> None:
        with mock.patch.object(disks.subprocess, "run") as m_run:
            m_run.return_value = mock.Mock(returncode=0)
            self.assertTrue(disks.luks_token_unlock("/dev/sda4"))
        argvs = [call.args[0] for call in m_run.call_args_list]
        self.assertEqual(argvs[0], ["cryptsetup", "open", "--token-only", "/dev/sda4", disks.TRIAL_MAPPER])
        self.assertEqual(argvs[-1], ["cryptsetup", "close", disks.TRIAL_MAPPER])

    def test_token_unlock_closes_after_a_failed_trial(self) -> None:
        with mock.patch.object(disks.subprocess, "run") as m_run:
            m_run.return_value = mock.Mock(returncode=1)
            self.assertFalse(disks.luks_token_unlock("/dev/sda4"))
        argvs = [call.args[0] for call in m_run.call_args_list]
        self.assertEqual(argvs[-1], ["cryptsetup", "close", disks.TRIAL_MAPPER])

    def test_password_unlock_feeds_the_passphrase_on_stdin(self) -> None:
        with mock.patch.object(disks.subprocess, "run") as m_run:
            m_run.return_value = mock.Mock(returncode=0)
            self.assertTrue(disks.luks_password_unlock("/dev/sda4", "olive-fern-crest-moss"))
        open_call = m_run.call_args_list[0]
        self.assertEqual(
            open_call.args[0],
            ["cryptsetup", "open", "--key-file=-", "/dev/sda4", disks.TRIAL_MAPPER],
        )
        self.assertEqual(open_call.kwargs["input"], "olive-fern-crest-moss")

    def test_password_unlock_closes_after_a_refused_password(self) -> None:
        with mock.patch.object(disks.subprocess, "run") as m_run:
            m_run.return_value = mock.Mock(returncode=2)
            self.assertFalse(disks.luks_password_unlock("/dev/sda4", "wrong"))
        argvs = [call.args[0] for call in m_run.call_args_list]
        self.assertEqual(argvs[-1], ["cryptsetup", "close", disks.TRIAL_MAPPER])

    def test_is_luks_checks_the_luks_magic_via_cryptsetup(self) -> None:
        with mock.patch.object(disks.subprocess, "run") as m_run:
            m_run.return_value = mock.Mock(returncode=0)
            self.assertTrue(disks.is_luks("/dev/sda4"))
        m_run.assert_called_once_with(
            ["cryptsetup", "isLuks", "/dev/sda4"], capture_output=True, check=False
        )

    def test_a_locked_luks_volume_still_offers_preserve(self) -> None:
        # §4.3: the token failing at discovery time does not kill the preserve
        # switch — the options page collects the Hardware password instead.
        # The LUKS2 label the installer stamps is what positively identifies
        # the locked volume as Lemonseed's without any key.
        node = {
            "type": "disk",
            "children": [
                {
                    "type": "part",
                    "path": "/dev/sda4",
                    "partlabel": disks.STORAGE_PARTLABEL,
                    "parttype": disks.LVM_PARTTYPE,
                    "size": 100,
                }
            ],
        }
        with (
            mock.patch.object(disks, "is_luks", return_value=True),
            mock.patch.object(disks, "luks_token_unlock", return_value=False),
            mock.patch.object(disks, "luks_label", return_value=disks.LUKS_LABEL),
        ):
            summary = disks._preservable_user_data(node)
        self.assertEqual(
            summary,
            {"storage_uuid": None, "size_bytes": 100, "luks": True, "locked": True},
        )

    def test_a_locked_volume_without_the_lemonseed_label_greys_preserve(self) -> None:
        # Round-3 finding: a fresh-but-empty LUKS2 container (a failed previous
        # install) must not default preserve on — without the installer's label
        # we cannot even tell the volume is ours, so the summary stays None.
        node = {
            "type": "disk",
            "children": [
                {
                    "type": "part",
                    "path": "/dev/sda4",
                    "partlabel": disks.STORAGE_PARTLABEL,
                    "parttype": disks.LVM_PARTTYPE,
                    "size": 100,
                }
            ],
        }
        with (
            mock.patch.object(disks, "is_luks", return_value=True),
            mock.patch.object(disks, "luks_token_unlock", return_value=False),
            mock.patch.object(disks, "luks_label", return_value=""),
        ):
            self.assertIsNone(disks._preservable_user_data(node))

    def test_luks_label_reads_the_luksdump_header(self) -> None:
        with mock.patch.object(
            disks,
            "_run_text",
            return_value="LUKS header information\nlabel: lemonseed-user-data\nsubsystem: -",
        ):
            self.assertEqual(disks.luks_label("/dev/sda4"), "lemonseed-user-data")

    def test_a_missing_label_is_empty(self) -> None:
        with mock.patch.object(disks, "_run_text", return_value=""):
            self.assertEqual(disks.luks_label("/dev/sda4"), "")


if __name__ == "__main__":
    raise SystemExit(unittest.main())
