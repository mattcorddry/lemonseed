"""Unit tests for the Hardware-password adoption (M8 P7, ENCRYPTED-USER-DATA.md §5).

The SHA-512 crypt port is pinned to known answers produced by
``openssl passwd -6``; adoption is exercised against a stubbed database and a
temporary state directory, with no Postgres, TPM or root required (AGENTS.md).
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hardware_password as hwp


def _openssl_sha512_crypt(password: str, salt: str) -> str:
    """Reference vectors from the host's openssl (the ground truth for $6$)."""
    out = subprocess.run(
        ["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
        check=False,
        input=password + "\n",
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


class Sha512CryptTests(unittest.TestCase):
    """The pure-Python $6$ port against openssl-produced known answers."""

    VECTORS = [
        # (password, salt) — short, single-char, max salt, and multi-word
        # passwords, which walk the length-bit loop differently.
        ("hello-world-foo", "abcd1234"),
        ("X", "zzzz9999zzzz"),
        ("apple-bacon-chard-date", "0123456789abcdef"),
        ("a" * 100, "abcdefgh"),
    ]

    def test_matches_openssl_known_answers(self) -> None:
        for password, salt in self.VECTORS:
            with self.subTest(password=password):
                self.assertEqual(
                    hwp.sha512_crypt(password, salt),
                    _openssl_sha512_crypt(password, salt),
                )

    def test_rounds_are_explicit_when_not_the_default(self) -> None:
        want = _openssl_sha512_crypt("pw", "salt")
        got = hwp.sha512_crypt("pw", "salt")
        self.assertEqual(got, want)
        self.assertNotIn("rounds=", got)
        custom = hwp.sha512_crypt("pw", "salt", rounds=6000)
        self.assertIn("$6$rounds=6000$", custom)


class Pbkd2LineTests(unittest.TestCase):
    def test_line_is_grub_format_with_hex_salt_and_digest(self) -> None:
        line = hwp.make_pbkdf2_line("some password")
        self.assertTrue(line.startswith("grub.pbkdf2.sha512.10000."))
        rest = line[len("grub.pbkdf2.sha512.10000.") :]
        salt_hex, digest_hex = rest.split(".")
        # 16-byte salt, 64-byte SHA-512 digest.
        self.assertEqual(len(salt_hex), 32)
        self.assertEqual(len(digest_hex), 128)
        int(salt_hex, 16)
        int(digest_hex, 16)

    def test_lines_are_independently_salted(self) -> None:
        self.assertNotEqual(
            hwp.make_pbkdf2_line("same"), hwp.make_pbkdf2_line("same")
        )


class AdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.bootstrap = root / "hardware-password"
        self.pbkdf2 = root / "grub-hardware.pbkdf2"
        self.root_hash = root / "root-password-hash"
        self.shadow = root / "shadow"
        self.shadow.write_text(
            "root:!:1::::\n"
            "user:$6$old$hash:1::::\n",
            encoding="utf-8",
        )
        self._patches = [
            mock.patch.object(hwp, "BOOTSTRAP_FILE", self.bootstrap),
            mock.patch.object(hwp, "PBKDF2_FILE", self.pbkdf2),
            mock.patch.object(hwp, "ROOT_HASH_FILE", self.root_hash),
            mock.patch.object(hwp, "SHADOW_FILE", self.shadow),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.db_rows: dict[str, str] = {}

        def get_meta(key):
            return self.db_rows.get(key)

        def set_meta(key, value):
            self.db_rows[key] = value
            return True

        get_patch = mock.patch.object(hwp.db, "cp_get_meta", side_effect=get_meta)
        set_patch = mock.patch.object(hwp.db, "cp_set_meta", side_effect=set_meta)
        for patch in (get_patch, set_patch):
            patch.start()
            self.addCleanup(patch.stop)

    def test_first_boot_adopts_stores_and_deletes_the_bootstrap(self) -> None:
        self.bootstrap.write_text("apple-bacon-chard-date\n", encoding="utf-8")
        self.assertTrue(hwp.adopt())
        # The plaintext left the filesystem: the DB is the only copy (§5.1).
        self.assertFalse(self.bootstrap.exists())
        self.assertEqual(
            self.db_rows[hwp.DB_KEY], "apple-bacon-chard-date"
        )
        # Derived material landed private, and the root hash is a real $6$ hash
        # of the adopted password (not the plaintext).
        self.assertTrue(self.pbkdf2.exists())
        self.assertTrue(self.root_hash.exists())
        for path in (self.pbkdf2, self.root_hash):
            self.assertEqual(
                oct(path.stat().st_mode & 0o777), "0o600", path.name
            )
        self.assertTrue(self.root_hash.read_text().startswith("$6$"))
        # The shadow entry carries the same hash the declarative
        # hashedPasswordFile will re-apply on every activation.
        root_field = self.shadow.read_text().splitlines()[0].split(":")[1]
        self.assertEqual(root_field, self.root_hash.read_text().strip())
        # Other shadow lines are untouched.
        self.assertIn("$6$old$hash", self.shadow.read_text())

    def test_second_boot_is_a_noop(self) -> None:
        self.bootstrap.write_text("apple-bacon-chard-date\n", encoding="utf-8")
        self.assertTrue(hwp.adopt())
        self.assertTrue(hwp.adopt())
        self.assertFalse(self.bootstrap.exists())
        first = self.pbkdf2.read_text()
        hwp.adopt()
        self.assertEqual(first, self.pbkdf2.read_text())

    def test_a_box_that_never_had_the_file_is_an_unperturbed_noop(self) -> None:
        self.assertFalse(hwp.adopt())
        self.assertEqual(self.db_rows, {})
        self.assertFalse(self.pbkdf2.exists())
        self.assertFalse(self.root_hash.exists())

    def test_db_failure_defers_keeping_the_bootstrap(self) -> None:
        self.bootstrap.write_text("apple-bacon-chard-date\n", encoding="utf-8")
        self.db_rows.clear()
        with mock.patch.object(
            hwp.db, "cp_set_meta", side_effect=[False]
        ) as set_meta:
            self.assertFalse(hwp.adopt())
        set_meta.assert_called_once()
        self.assertTrue(self.bootstrap.exists())

    def test_reveal_degrades_to_not_stored_without_the_plaintext(self) -> None:
        self.assertEqual(hwp.reveal(), None)
        self.assertEqual(self.db_rows, {})

    def test_reveal_adopts_lazily(self) -> None:
        self.bootstrap.write_text("apple-bacon-chard-date\n", encoding="utf-8")
        self.assertEqual(hwp.reveal(), "apple-bacon-chard-date")


class ShadowEditTests(unittest.TestCase):
    def test_missing_root_line_is_reported_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shadow = Path(tmp) / "shadow"
            shadow.write_text("user:!:1::::\n", encoding="utf-8")
            with mock.patch.object(hwp, "SHADOW_FILE", shadow):
                self.assertFalse(hwp._set_root_shadow("$6$x$y"))

    def test_other_lines_survive_the_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shadow = Path(tmp) / "shadow"
            shadow.write_text(
                "root:!:1::::\nuser:$6$keep$me:1::::\n", encoding="utf-8"
            )
            with mock.patch.object(hwp, "SHADOW_FILE", shadow):
                self.assertTrue(hwp._set_root_shadow("$6$new$hash"))
            lines = shadow.read_text().splitlines()
            self.assertEqual(lines[0].split(":")[1], "$6$new$hash")
            self.assertEqual(lines[1].split(":")[1], "$6$keep$me")


if __name__ == "__main__":
    unittest.main()
