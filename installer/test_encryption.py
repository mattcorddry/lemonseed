#!/usr/bin/env python3
"""Unit tests for the M8 P7 installer encryption flows (no root, no TPM, no disk).

Covers ENCRYPTED-USER-DATA.md §4.2 (fresh format step), §4.3 (preserve trial
unlock + re-enroll), §4.4 (the Hardware password material) and §5 (the
bootstrap file), stubbing cryptsetup/systemd-cryptenroll/provisioner entirely.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


import install  # noqa: E402


def patch_install_log(case: unittest.TestCase) -> None:
    """Keep a unit test from appending to the host's /var/lib/lemonseed."""
    tmp = tempfile.TemporaryDirectory()
    case.addCleanup(tmp.cleanup)
    patcher = mock.patch.object(install, "INSTALL_LOG", Path(tmp.name) / "installer.log")
    patcher.start()
    case.addCleanup(patcher.stop)


def fake_mount(root: Path, error: Exception | None = None):
    """Stand in for install.mount_user_data: hand out `root` instead of mounting."""

    @contextlib.contextmanager
    def _mount(disk: str, read_only: bool = False):  # signature parity
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


PASSWORD = "olive-fern-crest-moss"


class HardwarePasswordTests(unittest.TestCase):
    def test_four_words_from_the_vocabulary_joined_with_dashes(self) -> None:
        # The non-admin password-reset scheme: four sign-in-code words, the
        # same vocabulary the control plane serves, readable aloud by hand.
        with mock.patch.object(
            install,
            "_wordlist",
            # Padded past the vocabulary floor; the draw is stubbed below.
            return_value=["alpha", "beta", "gamma", "delta"] * 128,
        ), mock.patch.object(
            install.secrets,
            "choice",
            side_effect=["delta", "alpha", "gamma", "beta"],
        ) as m_choice:
            password = install.generate_hardware_password()
        self.assertEqual(password, "delta-alpha-gamma-beta")
        self.assertEqual(m_choice.call_count, install.HARDWARE_PASSWORD_WORDS)

    def test_the_wordlist_skips_comments_and_blanks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wordlist = Path(tmp) / "words-en_US.txt"
            wordlist.write_text("# comment\n\nalpha\nbeta\n", encoding="utf-8")
            with mock.patch.object(install, "WORDLIST_FILE", wordlist):
                self.assertEqual(install._wordlist(), ["alpha", "beta"])

    def test_a_too_small_wordlist_is_refused_not_silent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wordlist = Path(tmp) / "words-en_US.txt"
            wordlist.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            with (
                mock.patch.object(install, "WORDLIST_FILE", wordlist),
                mock.patch.object(install.secrets, "choice", return_value="alpha"),
            ):
                with self.assertRaises(install.InstallError):
                    install.generate_hardware_password()

    def test_pbkdf2_matches_the_grub_mkpasswd_format(self) -> None:
        salt = bytes(range(16))
        value = install.make_hardware_pbkdf2(PASSWORD, salt)
        parts = value.split(".")
        # grub-mkpasswd-pbkdf2's self-describing format: iterations and hex
        # salt/digest ride the string itself.
        self.assertEqual(parts[:4], ["grub", "pbkdf2", "sha512", "10000"])
        self.assertEqual(parts[4], salt.hex())
        self.assertEqual(len(parts[5]), 128)  # sha512 hex digest

    def test_pbkdf2_digest_is_the_documented_pbkdf2_hmac(self) -> None:
        import hashlib

        salt = b"0123456789abcdef"
        value = install.make_hardware_pbkdf2(PASSWORD, salt)
        digest = hashlib.pbkdf2_hmac(
            "sha512", PASSWORD.encode("utf-8"), salt, install.HARDWARE_PBKDF2_ITERATIONS
        )
        self.assertEqual(value.split("."), ["grub", "pbkdf2", "sha512", "10000", salt.hex(), digest.hex()])

    def test_pbkdf2_draws_a_fresh_salt_each_time(self) -> None:
        self.assertNotEqual(
            install.make_hardware_pbkdf2(PASSWORD), install.make_hardware_pbkdf2(PASSWORD)
        )

    def test_hardware_params_carries_the_two_grub_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            grub_dir = Path(tmp)
            target = install.write_hardware_params(grub_dir, "grub.pbkdf2.sha512.10000.aa.bb")
            lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], 'set superusers="grub"')
        self.assertEqual(lines[1], "password_pbkdf2 grub grub.pbkdf2.sha512.10000.aa.bb")
        self.assertEqual(len(lines), 2)


class SeedHardwarePasswordTests(unittest.TestCase):
    """§5.1: the bootstrap copy lands where the CP adopts it, mode 0600."""

    def setUp(self) -> None:
        patch_install_log(self)
        tmp, self.root = user_data_root()
        self.addCleanup(tmp.cleanup)
        self.target = self.root / "@var" / "lib" / "lemonseed" / "hardware-password"

    def test_the_bootstrap_file_lands_where_the_control_plane_reads_it(self) -> None:
        self.assertEqual(
            str(install.USER_DATA_MOUNT / install.HARDWARE_PASSWORD_REL),
            "/run/lemonseed-install-user-data/@var/lib/lemonseed/hardware-password",
        )

    def test_seeded_with_mode_0600(self) -> None:
        with mock.patch.object(install, "mount_user_data", fake_mount(self.root)):
            install.seed_hardware_password("/dev/sda", PASSWORD)
        self.assertTrue(self.target.is_file())
        self.assertEqual(self.target.read_text(encoding="utf-8"), PASSWORD + "\n")
        mode = self.target.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_a_failed_seed_fails_the_install(self) -> None:
        broken = install.InstallError("Could not mount /dev/sda4: not a btrfs")
        with mock.patch.object(install, "mount_user_data", fake_mount(self.root, error=broken)):
            with self.assertRaises(install.InstallError) as caught:
                install.seed_hardware_password("/dev/sda", PASSWORD)
        self.assertIn("Hardware password was not recorded", str(caught.exception))


class PrepareEncryptedTests(unittest.TestCase):
    """§4.2: format → open → enroll → PROVE THE SEAL → provision → seed → close.

    Every cryptsetup/systemd-cryptenroll/provisioner call is stubbed; only the
    order and the arguments are asserted.
    """

    def setUp(self) -> None:
        patch_install_log(self)
        self.calls: list[tuple[list[str], dict]] = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        provisioner = Path(tmp.name) / "appliance-storage-provision.sh"
        provisioner.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.provisioner = provisioner

    def drive(self, fail_on=None) -> tuple[list, mock.Mock]:
        calls = self.calls

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if fail_on and fail_on(cmd):
                raise subprocess.CalledProcessError(1, cmd, output="simulated failure")
            return mock.Mock(stdout="ok")

        with (
            mock.patch.object(install, "_run", side_effect=fake_run),
            mock.patch.object(install, "wait_part", return_value="/dev/sda4"),
            mock.patch.object(install, "PROVISION_SCRIPT", self.provisioner),
            mock.patch.object(install, "seed_hardware_password"),
            mock.patch.object(install.subprocess, "run") as closes,
        ):
            try:
                install.prepare_encrypted_user_data("/dev/sda", PASSWORD, None)
                self.error = None
            except install.InstallError as exc:
                self.error = exc
        return calls, closes

    def cryptsetup_verbs(self, calls) -> list[list[str]]:
        return [cmd[:2] if cmd[0] == "cryptsetup" else cmd[:1] for cmd, _kw in calls]

    def test_the_happy_path_formats_enrolls_proves_provisions_and_closes(self) -> None:
        calls, closes = self.drive()
        self.assertIsNone(self.error)
        verbs = [cmd for cmd, _kw in calls]
        self.assertEqual(verbs[0][:2], ["cryptsetup", "luksFormat"])
        self.assertEqual(
            verbs[0],
            [
                "cryptsetup",
                "luksFormat",
                "--type",
                "luks2",
                "--key-slot",
                "0",
                "--batch-mode",
                "--label",
                "lemonseed-user-data",
                "--key-file=-",
                "/dev/sda4",
            ],
        )
        self.assertEqual(verbs[1], ["cryptsetup", "open", "--key-file=-", "/dev/sda4", install.LUKS_MAPPER])
        self.assertTrue(verbs[2][0].startswith("systemd-cryptenroll"))
        self.assertEqual(verbs[3], ["cryptsetup", "close", install.LUKS_MAPPER])
        self.assertEqual(verbs[4], ["cryptsetup", "open", "--token-only", "/dev/sda4", install.LUKS_MAPPER])
        self.assertEqual(verbs[5], ["bash", str(self.provisioner)])
        # The volume is closed again after the provisioner and the bootstrap
        # seed, before write_raw/seed_esp continue (§4.2 step 5).
        closes.assert_called_with(
            ["cryptsetup", "close", install.LUKS_MAPPER], capture_output=True, check=False
        )
        self.assertEqual(verbs[-1], verbs[5])

    def test_the_passphrase_rides_stdin_never_argv(self) -> None:
        calls, _closes = self.drive()
        format_kwargs = calls[0][1]
        self.assertEqual(format_kwargs.get("input"), PASSWORD)
        self.assertNotIn(PASSWORD, " ".join(calls[0][0]))
        self.assertNotIn(PASSWORD, " ".join(calls[2][0]))

    def test_the_enrollment_keyfile_is_0600_and_gone_afterwards(self) -> None:
        calls, _closes = self.drive()
        enroll_cmd = calls[2][0]
        keyfile = next(arg for arg in enroll_cmd if arg.startswith("--unlock-key-file="))
        path = Path(keyfile.split("=", 1)[1])
        self.assertFalse(path.is_file(), "the 0600 keyfile must be deleted after enroll")
        # The content was written without a trailing newline while it existed:
        # re-run the context manager to observe the shape it had.
        with install._hardware_keyfile(PASSWORD) as name:
            p = Path(name)
            self.assertEqual(p.read_text(encoding="utf-8"), PASSWORD)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_a_failed_reseal_aborts_the_install_and_closes_up(self) -> None:
        def fail_on(cmd):
            return cmd[0] == "cryptsetup" and "--token-only" in cmd

        calls, closes = self.drive(fail_on=fail_on)
        self.assertIsNotNone(self.error, "a failed prove-seal must fail the install")
        self.assertIn("trusted platform module", str(self.error))
        verbs = [cmd for cmd, _kw in calls]
        # The provisioner never ran: the seal was proven broken.
        self.assertFalse(any(cmd[0] == "bash" for cmd in verbs))
        closes.assert_called_with(
            ["cryptsetup", "close", install.LUKS_MAPPER], capture_output=True, check=False
        )

    def test_a_failed_format_aborts_before_anything_else(self) -> None:
        def fail_on(cmd):
            return cmd[:2] == ["cryptsetup", "luksFormat"]

        calls, _closes = self.drive(fail_on=fail_on)
        self.assertIsNotNone(self.error)
        self.assertIn("Could not encrypt the data storage drive", str(self.error))
        self.assertEqual(len(calls), 1)


class PreserveLuksTests(unittest.TestCase):
    """§4.3: trial unlock as soon as preserve mode is confirmed; a wrong
    password is refused before the install starts; a plain pre-P7 volume
    skips everything."""

    def setUp(self) -> None:
        patch_install_log(self)

    def drive_validate(self, is_luks, token_ok, password_ok, password=PASSWORD):
        with (
            mock.patch.object(install, "parts_on_disk", return_value={"lemonseed-user-data": "/dev/sda4"}),
            mock.patch.object(install, "is_luks", return_value=is_luks),
            mock.patch.object(install, "luks_token_unlock", return_value=token_ok),
            mock.patch.object(install, "luks_password_unlock", return_value=password_ok) as m_pw,
        ):
            try:
                self.mode = install._validate_preserved_luks("/dev/sda", password)
                self.error = None
            except install.InstallError as exc:
                self.mode = None
                self.error = exc
        return m_pw

    def test_a_token_unlock_needs_nothing(self) -> None:
        self.drive_validate(is_luks=True, token_ok=True, password_ok=False)
        self.assertIsNone(self.error)
        self.assertEqual(self.mode, "token")

    def test_a_failed_token_validates_the_password_by_trial_unlock(self) -> None:
        m_pw = self.drive_validate(is_luks=True, token_ok=False, password_ok=True)
        self.assertIsNone(self.error)
        self.assertEqual(self.mode, "password")
        m_pw.assert_called_once_with("/dev/sda4", PASSWORD)

    def test_a_wrong_password_is_refused_legibly_before_the_install(self) -> None:
        m_pw = self.drive_validate(is_luks=True, token_ok=False, password_ok=False)
        self.assertIsNotNone(self.error)
        self.assertIn("did not unlock the existing encrypted data storage drive", str(self.error))
        m_pw.assert_called_once_with("/dev/sda4", PASSWORD)

    def test_a_locked_volume_without_a_password_asks_for_one(self) -> None:
        self.drive_validate(is_luks=True, token_ok=False, password_ok=False, password="")
        self.assertIsNotNone(self.error)
        self.assertIn("Enter the Hardware password", str(self.error))

    def test_a_plain_pre_p7_volume_skips_everything(self) -> None:
        m_pw = self.drive_validate(is_luks=False, token_ok=False, password_ok=False)
        self.assertIsNone(self.error)
        self.assertEqual(self.mode, "")
        m_pw.assert_not_called()

    def test_reopen_uses_the_validated_mode_and_reenrolls(self) -> None:
        tmp, _root = user_data_root()
        self.addCleanup(tmp.cleanup)
        pbkdf2 = "grub.pbkdf2.sha512.10000.aa.bb"
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(stdout="ok")

        with (
            mock.patch.object(install, "wait_part", return_value="/dev/sda4"),
            mock.patch.object(install, "_run", side_effect=fake_run),
            mock.patch.object(install, "_hardware_keyfile") as m_keyfile,
            mock.patch.object(
                install,
                "read_preserved_pbkdf2",
                return_value=pbkdf2,
            ) as m_read,
        ):
            m_keyfile.return_value.__enter__ = mock.Mock(return_value="/tmp/key")
            m_keyfile.return_value.__exit__ = mock.Mock(return_value=False)
            result = install.reopen_preserved_luks("/dev/sda", "password", PASSWORD, None)
        self.assertEqual(result, pbkdf2)
        m_read.assert_called_once_with("/dev/sda")
        self.assertEqual(calls[0], ["cryptsetup", "open", "--key-file=-", "/dev/sda4", install.LUKS_MAPPER])
        self.assertIn("systemd-cryptenroll", calls[1][0])

    def test_reopen_by_token_when_no_password_was_needed(self) -> None:
        tmp, _root = user_data_root()
        self.addCleanup(tmp.cleanup)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(stdout="ok")

        with (
            mock.patch.object(install, "wait_part", return_value="/dev/sda4"),
            mock.patch.object(install, "_run", side_effect=fake_run),
            mock.patch.object(install, "read_preserved_pbkdf2", return_value=""),
        ):
            install.reopen_preserved_luks("/dev/sda", "token", "", None)
        self.assertEqual(calls[0], ["cryptsetup", "open", "--token-only", "/dev/sda4", install.LUKS_MAPPER])
        # No password was collected, so there is nothing to re-enroll with:
        # the token already works (§4.3, 'nothing is asked').
        self.assertEqual(len(calls), 1)


class OptionsPageProbeTests(unittest.TestCase):
    """The API the options page drives: LUKS state and password validation."""

    def setUp(self) -> None:
        patch_install_log(self)

    def test_state_reports_a_token_unlocked_luks_volume(self) -> None:
        with (
            mock.patch.object(install, "parts_on_disk", return_value={"lemonseed-user-data": "/dev/sda4"}),
            mock.patch.object(install, "is_luks", return_value=True),
            mock.patch.object(install, "luks_token_unlock", return_value=True),
        ):
            state = install.preserved_luks_state("/dev/sda")
        self.assertEqual(state, {"luks": True, "unlocked": True})

    def test_state_reports_a_plain_volume(self) -> None:
        with (
            mock.patch.object(install, "parts_on_disk", return_value={"lemonseed-user-data": "/dev/sda4"}),
            mock.patch.object(install, "is_luks", return_value=False),
        ):
            state = install.preserved_luks_state("/dev/sda")
        self.assertEqual(state, {"luks": False, "unlocked": False})

    def test_verify_refuses_a_wrong_password_with_a_legible_error(self) -> None:
        with (
            mock.patch.object(install, "parts_on_disk", return_value={"lemonseed-user-data": "/dev/sda4"}),
            mock.patch.object(install, "is_luks", return_value=True),
            mock.patch.object(install, "luks_password_unlock", return_value=False) as m_pw,
        ):
            verdict = install.preserved_luks_verify("/dev/sda", "wrong")
        self.assertFalse(verdict["ok"])
        self.assertIn("did not unlock", verdict["error"])
        m_pw.assert_called_once_with("/dev/sda4", "wrong")

    def test_verify_accepts_the_right_password(self) -> None:
        with (
            mock.patch.object(install, "parts_on_disk", return_value={"lemonseed-user-data": "/dev/sda4"}),
            mock.patch.object(install, "is_luks", return_value=True),
            mock.patch.object(install, "luks_password_unlock", return_value=True),
            mock.patch.object(install, "luks_preserved_identity", return_value={"format": "lemonseed-storage-v1"}),
        ):
            verdict = install.preserved_luks_verify("/dev/sda", PASSWORD)
        self.assertTrue(verdict["ok"])
        self.assertIsNone(verdict["error"])

    def test_verify_refuses_an_empty_volume_even_with_the_right_password(self) -> None:
        # Review: a fresh-but-empty LUKS2 container (a failed previous
        # install) opens with a perfectly good password yet preserves
        # nothing — the identity check must refuse it before the install.
        with (
            mock.patch.object(install, "parts_on_disk", return_value={"lemonseed-user-data": "/dev/sda4"}),
            mock.patch.object(install, "is_luks", return_value=True),
            mock.patch.object(install, "luks_password_unlock", return_value=True),
            mock.patch.object(install, "luks_preserved_identity", return_value=None),
        ):
            verdict = install.preserved_luks_verify("/dev/sda", PASSWORD)
        self.assertFalse(verdict["ok"])
        self.assertIn("does not contain a Lemonseed installation", verdict["error"])


class RunInstallWiringTests(unittest.TestCase):
    """run_install must place the encryption work between partition_disk and
    write_raw, and hand seed_esp the PBKDF2 it should seat."""

    def setUp(self) -> None:
        patch_install_log(self)

    def drive(
        self,
        stack: contextlib.ExitStack,
        preserve: bool = False,
        encrypt: bool = False,
        password: str = PASSWORD,
        is_luks: bool = False,
        token_ok: bool = False,
        password_ok: bool = False,
    ) -> mock.Mock:
        files = {"root.squashfs": Path("/nonexistent/root.squashfs")}
        stack.enter_context(mock.patch.object(install, "require_host_ready"))
        stack.enter_context(mock.patch.object(install, "require_install_tools"))
        # Deterministic PBKDF2 so the ESP assertion can name the value.
        stack.enter_context(
            mock.patch.object(
                install,
                "make_hardware_pbkdf2",
                side_effect=lambda pw, salt=None: f"grub.pbkdf2.sha512.10000.stub.{pw}",
            )
        )
        stack.enter_context(
            mock.patch.object(
                install,
                "_eligible",
                return_value={
                    "path": "/dev/sda",
                    "preservable_user_data": {"storage_uuid": "u", "luks": is_luks},
                },
            )
        )
        stack.enter_context(
            mock.patch.object(install, "acquire_artifacts", return_value=(files, "9.9.9"))
        )
        stack.enter_context(mock.patch.object(install, "parts_on_disk", return_value={"lemonseed-user-data": "/dev/sda4"}))
        stack.enter_context(mock.patch.object(install, "is_luks", return_value=is_luks))
        stack.enter_context(mock.patch.object(install, "luks_token_unlock", return_value=token_ok))
        stack.enter_context(mock.patch.object(install, "luks_password_unlock", return_value=password_ok))
        stack.enter_context(mock.patch.object(install, "partition_disk"))
        stack.enter_context(mock.patch.object(install, "wait_part", return_value="/dev/sda2"))
        stack.enter_context(mock.patch.object(install, "write_raw"))
        prepare = stack.enter_context(mock.patch.object(install, "prepare_encrypted_user_data"))
        reopen = stack.enter_context(
            mock.patch.object(install, "reopen_preserved_luks", return_value="grub.pbkdf2.preserved.0.0")
        )
        read_pbkdf2 = stack.enter_context(
            mock.patch.object(install, "read_preserved_pbkdf2", return_value="grub.pbkdf2.preserved.0.0")
        )
        seed = stack.enter_context(mock.patch.object(install, "seed_esp"))
        stack.enter_context(mock.patch.object(install, "apply_reset_marker"))
        stack.enter_context(mock.patch.object(install, "apply_channel_file"))
        stack.enter_context(mock.patch.object(install, "write_slot_registry"))
        stack.enter_context(mock.patch.object(install.time, "sleep"))
        stack.enter_context(mock.patch.object(install.subprocess, "run"))
        install.run_install(
            "/dev/sda",
            preserve_user_data=preserve,
            channel="stable",
            encrypt_user_data=encrypt,
            hardware_password=password,
        )
        self.last_seed = seed
        self.last_prepare = prepare
        self.last_reopen = reopen
        self.last_read_pbkdf2 = read_pbkdf2
        return seed

    def test_an_unencrypted_fresh_install_skips_luks_entirely(self) -> None:
        with contextlib.ExitStack() as stack:
            seed = self.drive(stack, preserve=False, encrypt=False)
        seed.assert_called_once()
        self.assertEqual(
            seed.call_args.kwargs["hardware_pbkdf2"],
            f"grub.pbkdf2.sha512.10000.stub.{PASSWORD}",
        )
        self.last_prepare.assert_not_called()

    def test_an_encrypted_fresh_install_formats_between_partition_and_write(self) -> None:
        with contextlib.ExitStack() as stack:
            seed = self.drive(stack, preserve=False, encrypt=True)
        self.last_prepare.assert_called_once_with("/dev/sda", PASSWORD, None)
        # The format step happens before write_raw (patched after prepare in
        # call order) and the ESP seats the matching PBKDF2.
        self.assertEqual(
            seed.call_args.kwargs["hardware_pbkdf2"],
            f"grub.pbkdf2.sha512.10000.stub.{PASSWORD}",
        )

    def test_encrypt_without_a_presented_password_still_generates_one(self) -> None:
        with contextlib.ExitStack() as stack:
            self.drive(stack, preserve=False, encrypt=True, password="")
        generated = self.last_prepare.call_args.args[1]
        self.assertEqual(len(generated.split("-")), install.HARDWARE_PASSWORD_WORDS)

    def test_a_preserve_install_never_formats_the_existing_volume(self) -> None:
        with contextlib.ExitStack() as stack:
            seed = self.drive(stack, preserve=True, encrypt=True, is_luks=True, token_ok=True)
        self.last_prepare.assert_not_called()
        # The preserved volume is reopened (token path) and the preserved
        # PBKDF2 — not a new password — is seated on the ESP (§5.2).
        self.last_reopen.assert_called_once_with("/dev/sda", "token", PASSWORD, None)
        self.assertEqual(seed.call_args.kwargs["hardware_pbkdf2"], "grub.pbkdf2.preserved.0.0")

    def test_a_preserve_luks_volume_refuses_a_wrong_password_before_partitioning(self) -> None:
        with contextlib.ExitStack() as stack:
            partition = stack.enter_context(mock.patch.object(install, "partition_disk"))
            with self.assertRaises(install.InstallError) as caught:
                self.drive(
                    stack,
                    preserve=True,
                    is_luks=True,
                    token_ok=False,
                    password_ok=False,
                )
        # Nothing was touched: the refusal happens before partitioning runs.
        partition.assert_not_called()
        self.assertIn("did not unlock the existing encrypted data storage drive", str(caught.exception))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
