#!/usr/bin/env python3
"""Unit tests for lemonseed_verify (M8 P1): fail-closed signature verification.

Uses the committed known-answer vector (scripts/test-data/signing/) for the
openssl ECDSA P-256 verify path, and freshly generated keys in a temp dir for the
refusal matrix. Requires openssl on PATH, like the signer.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import lemonseed_verify as verify  # noqa: E402

VECTOR_DIR = SCRIPTS / "test-data" / "signing"
VECTOR_MANIFEST = (VECTOR_DIR / "manifest.json").read_bytes()
VECTOR_SIG = (VECTOR_DIR / "manifest.json.sig").read_bytes()
VECTOR_PUB = VECTOR_DIR / "known-answer.pub"


def openssl_available() -> bool:
    try:
        subprocess.run(
            ["openssl", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


class TrustSetTestCase(unittest.TestCase):
    def make_trust_dir(self, keys: list[Path], *, version: int = 1, raw: str | None = None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        entries = []
        for key in keys:
            name = f"{key.name}"
            (directory / name).write_bytes(key.read_bytes())
            entries.append({"version": 1, "algorithm": "ecdsa-p256", "file": name})
        if raw is not None:
            (directory / verify.TRUST_MANIFEST_NAME).write_text(raw, encoding="utf-8")
        else:
            (directory / verify.TRUST_MANIFEST_NAME).write_text(
                json.dumps({"version": version, "keys": entries}), encoding="utf-8"
            )
        return directory

    def sign(self, key: Path, data: bytes) -> bytes:
        import subprocess as sp

        with tempfile.TemporaryDirectory() as tmp:
            sig = Path(tmp) / "x.sig"
            data_file = Path(tmp) / "x.data"
            data_file.write_bytes(data)
            sp.run(
                ["openssl", "pkeyutl", "-sign", "-inkey", str(key), "-rawin",
                 "-in", str(data_file), "-out", str(sig)],
                check=True, stdout=sp.DEVNULL, stderr=sp.DEVNULL,
            )
            return sig.read_bytes()


@unittest.skipUnless(openssl_available(), "openssl required")
class KnownAnswerTest(TrustSetTestCase):
    """Pins the openssl invocation against a third-party-computable answer."""

    def test_good_signature_verifies(self):
        trust = self.make_trust_dir([VECTOR_PUB])
        verified = verify.verify_manifest(VECTOR_MANIFEST, VECTOR_SIG, trust)
        self.assertEqual(verified.name, "known-answer.pub")

    def test_verify_manifest_files_roundtrip(self):
        trust = self.make_trust_dir([VECTOR_PUB])
        verified = verify.verify_manifest_files(VECTOR_DIR / "manifest.json", directory=trust)
        self.assertEqual(verified.name, "known-answer.pub")

    def test_ecdsa_signatures_are_randomized(self):
        # RFC 6979 is NOT used: two signatures over the same bytes differ, and
        # both must verify — the verifier never assumes deterministic nonces
        # and never normalizes an s value (SIGNING.md §2).
        trust = self.make_trust_dir([VECTOR_PUB])
        first = self.sign(VECTOR_DIR / "known-answer.priv", VECTOR_MANIFEST)
        second = self.sign(VECTOR_DIR / "known-answer.priv", VECTOR_MANIFEST)
        self.assertNotEqual(first, second)
        self.assertEqual(verify.verify_manifest(VECTOR_MANIFEST, first, trust).name, "known-answer.pub")
        self.assertEqual(verify.verify_manifest(VECTOR_MANIFEST, second, trust).name, "known-answer.pub")

    def test_wrong_key_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = Path(tmp) / "other.pub"
            subprocess.run(
                [
                    "openssl", "genpkey", "-algorithm", "EC",
                    "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-pkeyopt", "ec_param_enc:named_curve",
                    "-out", Path(tmp) / "other.priv",
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["openssl", "pkey", "-in", Path(tmp) / "other.priv", "-pubout", "-out", str(other)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            trust = self.make_trust_dir([other])
            with self.assertRaises(verify.VerificationError) as ctx:
                verify.verify_manifest(VECTOR_MANIFEST, VECTOR_SIG, trust)
            self.assertIn("does not verify", str(ctx.exception))

    def test_tampered_manifest_byte_refuses(self):
        trust = self.make_trust_dir([VECTOR_PUB])
        tampered = VECTOR_MANIFEST.replace(b"known-answer", b"known-answerX")
        self.assertNotEqual(tampered, VECTOR_MANIFEST)
        with self.assertRaises(verify.VerificationError):
            verify.verify_manifest(tampered, VECTOR_SIG, trust)

    def test_altered_after_signing_refuses(self):
        # A signature produced over *some* manifest, then the manifest edited:
        # valid-looking sig, different bytes.
        trust = self.make_trust_dir([VECTOR_PUB])
        manifest = json.loads(VECTOR_MANIFEST.decode("utf-8"))
        manifest["version"] = "altered"
        altered = json.dumps(manifest).encode("utf-8")
        with self.assertRaises(verify.VerificationError):
            verify.verify_manifest(altered, VECTOR_SIG, trust)

    def test_truncated_signature_refuses(self):
        trust = self.make_trust_dir([VECTOR_PUB])
        with self.assertRaises(verify.VerificationError):
            verify.verify_manifest(VECTOR_MANIFEST, VECTOR_SIG[:32], trust)

    def test_empty_signature_refuses(self):
        trust = self.make_trust_dir([VECTOR_PUB])
        with self.assertRaises(verify.VerificationError):
            verify.verify_manifest(VECTOR_MANIFEST, b"", trust)


@unittest.skipUnless(openssl_available(), "openssl required")
class TrustSetRefusalTest(TrustSetTestCase):
    def test_missing_trust_dir_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(verify.VerificationError) as ctx:
                verify.verify_manifest(VECTOR_MANIFEST, VECTOR_SIG, Path(tmp) / "absent")
            self.assertIn("trust set missing", str(ctx.exception))

    def test_missing_json_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / verify.TRUST_MANIFEST_NAME).unlink(missing_ok=True)
            with self.assertRaises(verify.VerificationError) as ctx:
                verify.load_trust_set(directory)
            self.assertIn("trust set missing", str(ctx.exception))

    def test_malformed_json_refuses(self):
        trust = self.make_trust_dir([], raw="not json at all")
        with self.assertRaises(verify.VerificationError) as ctx:
            verify.load_trust_set(trust)
        self.assertIn("unreadable", str(ctx.exception))

    def test_non_integer_version_refuses(self):
        trust = self.make_trust_dir([VECTOR_PUB], raw='{"version": "one", "keys": []}')
        with self.assertRaises(verify.VerificationError) as ctx:
            verify.load_trust_set(trust)
        self.assertIn("positive integer", str(ctx.exception))

    def test_empty_key_list_refuses(self):
        trust = self.make_trust_dir([VECTOR_PUB], raw='{"version": 1, "keys": []}')
        with self.assertRaises(verify.VerificationError) as ctx:
            verify.load_trust_set(trust)
        self.assertIn("non-empty list", str(ctx.exception))

    def test_unknown_algorithm_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / verify.TRUST_MANIFEST_NAME).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "keys": [{"version": 1, "algorithm": "rsa", "file": "k.pub"}],
                    }
                ),
                encoding="utf-8",
            )
            (directory / "k.pub").write_bytes(b"irrelevant")
            with self.assertRaises(verify.VerificationError) as ctx:
                verify.load_trust_set(directory)
            self.assertIn("unsupported algorithm", str(ctx.exception))

    def test_glob_pattern_is_never_expanded(self):
        # No globs, ever: a literal '*.pub' in the trust list is a filename to
        # open, not a pattern to expand — and here it simply does not exist.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / verify.TRUST_MANIFEST_NAME).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "keys": [{"version": 1, "algorithm": "ecdsa-p256", "file": "*.pub"}],
                    }
                ),
                encoding="utf-8",
            )
            (directory / "some-other.pub").write_bytes(b"not listed, never trusted")
            with self.assertRaises(verify.VerificationError) as ctx:
                verify.load_trust_set(directory)
            self.assertIn("missing from", str(ctx.exception))

    def test_listed_key_missing_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / verify.TRUST_MANIFEST_NAME).write_text(
                json.dumps(
                    {
                        "version": 1,
                        "keys": [{"version": 1, "algorithm": "ecdsa-p256", "file": "absent.pub"}],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(verify.VerificationError) as ctx:
                verify.load_trust_set(directory)
            self.assertIn("missing from", str(ctx.exception))

    def test_any_trusted_key_accepts(self):
        with tempfile.TemporaryDirectory() as tmp:
            second = Path(tmp) / "second.priv"
            second_pub = Path(tmp) / "second.pub"
            subprocess.run(
                [
                    "openssl", "genpkey", "-algorithm", "EC",
                    "-pkeyopt", "ec_paramgen_curve:P-256",
                    "-pkeyopt", "ec_param_enc:named_curve",
                    "-out", str(second),
                ],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["openssl", "pkey", "-in", str(second), "-pubout", "-out", str(second_pub)],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            trust = self.make_trust_dir([second_pub, VECTOR_PUB])
            # Signed by the *second* key in the set: the overlap window (§6).
            sig = self.sign(second, VECTOR_MANIFEST)
            verified = verify.verify_manifest(VECTOR_MANIFEST, sig, trust)
            self.assertEqual(verified.name, "second.pub")

    def test_env_override_points_the_trust_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = verify.trust_dir()
            try:
                import os

                os.environ[verify.TRUST_DIR_ENV] = tmp
                self.assertEqual(verify.trust_dir(), Path(tmp))
            finally:
                if old == Path(verify.TRUST_DIR_DEFAULT):
                    os.environ.pop(verify.TRUST_DIR_ENV, None)
                else:
                    os.environ[verify.TRUST_DIR_ENV] = str(old)


class SigningMetadataTest(unittest.TestCase):
    def make_manifest(self, **signing):
        return {"version": "x", "signing": signing}

    def test_enabled_and_listed_key_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "lemonseed-release.pub").write_bytes(b"key")
            import os

            os.environ[verify.TRUST_DIR_ENV] = tmp
            try:
                verify.check_signing_metadata(
                    self.make_manifest(enabled=True, scheme="detached-ecdsa-p256", public_key="lemonseed-release.pub")
                )
            finally:
                os.environ.pop(verify.TRUST_DIR_ENV, None)

    def test_wrong_scheme_refuses(self):
        # Single trust generation: the previous scheme is rejected outright,
        # not merely superseded (SIGNING.md §2).
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "lemonseed-release.pub").write_bytes(b"key")
            import os

            os.environ[verify.TRUST_DIR_ENV] = tmp
            try:
                with self.assertRaises(verify.VerificationError) as ctx:
                    verify.check_signing_metadata(
                        self.make_manifest(enabled=True, scheme="detached-ed25519", public_key="lemonseed-release.pub")
                    )
                self.assertIn("expected", str(ctx.exception))
            finally:
                os.environ.pop(verify.TRUST_DIR_ENV, None)

    def test_missing_scheme_refuses(self):
        with self.assertRaises(verify.VerificationError) as ctx:
            verify.check_signing_metadata(self.make_manifest(enabled=True, public_key="lemonseed-release.pub"))
        self.assertIn("expected", str(ctx.exception))

    def test_disabled_signing_refuses(self):
        with self.assertRaises(verify.VerificationError) as ctx:
            verify.check_signing_metadata(self.make_manifest(enabled=False))
        self.assertIn("not enabled", str(ctx.exception))

    def test_missing_signing_block_refuses(self):
        with self.assertRaises(verify.VerificationError) as ctx:
            verify.check_signing_metadata({"version": "x"})
        self.assertIn("not enabled", str(ctx.exception))

    def test_unlisted_public_key_refuses(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            os.environ[verify.TRUST_DIR_ENV] = tmp
            try:
                with self.assertRaises(verify.VerificationError) as ctx:
                    verify.check_signing_metadata(
                        self.make_manifest(enabled=True, scheme="detached-ecdsa-p256", public_key="release-v9.pub")
                    )
                self.assertIn("does not contain it", str(ctx.exception))
            finally:
                os.environ.pop(verify.TRUST_DIR_ENV, None)


if __name__ == "__main__":
    unittest.main()
