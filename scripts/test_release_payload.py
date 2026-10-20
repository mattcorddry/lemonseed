#!/usr/bin/env python3
"""Unit tests for release_payload (M8 P2): canonical tar, sharding, size gate,
and the consumer's verify → assemble → re-verify path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

import lemonseed_artifacts as artifacts  # noqa: E402
import release_payload  # noqa: E402

# The tracked QEMU-only trust set (private key tracked on purpose; never a
# release key) — same one the update smoke uses.
SMOKE_KEYS = ROOT / "keys" / "qemu"
os.environ.setdefault("LEMONSEED_RELEASE_KEYS_DIR", str(SMOKE_KEYS))

# Small part size so multi-chunk + remainder behavior is exercised without
# gigabytes of bytes.
PART_BYTES = 64 * 1024
BODIES = {
    "vmlinuz": b"kernel-" + b"K" * 20000,
    "initrd.img": b"initrd-" + b"I" * 30000,
    "root.squashfs": b"SQUASH-" + b"S" * 40 * 1024,
}


def sign_dir(directory: Path) -> None:
    """Sign a staged release manifest with the QEMU smoke key, in place —
    exactly what `make release` does via scripts/sign.sh."""
    subprocess.run(
        [str(SCRIPTS / "sign.sh"), str(directory)],
        check=True,
        stdout=subprocess.DEVNULL,
        env={
            **os.environ,
            "SIGN_KEY": str(SMOKE_KEYS / "qemu-release-v1.priv"),
            "SIGN_PUB_NAME": "qemu-release-v1.pub",
        },
    )


def make_release(tmp: Path, *, part_bytes: int = PART_BYTES) -> Path:
    arts = tmp / "build"
    arts.mkdir(parents=True, exist_ok=True)
    manifest = {"version": "test.01", "artifacts": {}}
    for name, body in BODIES.items():
        (arts / name).write_bytes(body)
        manifest["artifacts"][name] = {
            "path": name,
            "sha256": hashlib.sha256(body).hexdigest(),
            "length": len(body),
        }
    (arts / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # A fake installer asset, so every staged set carries the installer block.
    iso = tmp / "lemonseed-installer.iso"
    iso.write_bytes(b"ISO-FIXTURE" + b"\0" * 2048)

    out = tmp / "release"
    release = release_payload.main(
        [
            "pack", "--artifacts-dir", str(arts), "--out-dir", str(out),
            "--part-bytes", str(part_bytes), "--installer", str(iso),
        ]
    )
    assert release == 0
    sign_dir(out)
    return out


def assembled_bytes(files: dict[str, Path], key: str) -> bytes:
    return files[key].read_bytes()


class PackTest(unittest.TestCase):
    def test_pack_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = make_release(Path(tmp) / "a")
            second = make_release(Path(tmp) / "b")
            parts = sorted(p.name for p in first.glob("payload-*.part"))
            self.assertGreater(len(parts), 1, "test should exercise multi-chunk + remainder")
            for name in parts:
                self.assertEqual(
                    (first / name).read_bytes(), (second / name).read_bytes(),
                    f"{name} differs between identical packs",
                )
            self.assertEqual(
                (first / "manifest.json").read_bytes(), (second / "manifest.json").read_bytes()
            )

    def test_pack_writes_size_gate_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = make_release(Path(tmp))
            manifest = json.loads((release / "manifest.json").read_text())
            tar_block = manifest["payload"]["tar"]
            self.assertEqual(
                tar_block["length"], sum(b["length"] for b in manifest["payload"]["chunks"])
            )
            installer = manifest["installer"]
            self.assertEqual(installer["name"], "lemonseed-installer.iso")

    def test_size_gate_fails_naming_the_offender(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            arts = tmp / "build"
            arts.mkdir()
            body = b"S" * 10 * 1024
            manifest = {"version": "test.01", "artifacts": {}}
            for name in BODIES:
                (arts / name).write_bytes(body)
                manifest["artifacts"][name] = {
                    "path": name,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "length": len(body),
                }
            (arts / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            # One artifact over the (shrunk) cap → its part is over the cap.
            rc = release_payload.main(
                [
                    "pack", "--artifacts-dir", str(arts), "--out-dir", str(tmp / "r"),
                    "--part-bytes", str(8 * 1024), "--max-asset-bytes", str(4 * 1024),
                ]
            )
            self.assertEqual(rc, 1)

    def test_installer_size_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            release = make_release(tmp)
            big = tmp / "lemonseed-installer.iso"
            big.write_bytes(b"I" * (128 * 1024))
            rc = release_payload.main(
                [
                    "pack", "--artifacts-dir", str(release), "--out-dir", str(tmp / "r2"),
                    "--installer", str(big), "--part-bytes", str(PART_BYTES),
                    "--max-asset-bytes", str(64 * 1024),
                ]
            )
            self.assertEqual(rc, 1)


class AssembleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.release = make_release(Path(self.tmp.name))
        self.download = Path(self.tmp.name) / "dl"

    def test_round_trip_from_local_dir(self) -> None:
        files, version, _ = artifacts.acquire_artifacts(
            download_dir=self.download, directory=str(self.release)
        )
        self.assertEqual(version, "test.01")
        for key, body in BODIES.items():
            self.assertEqual(files[key].read_bytes(), body, key)
        # The assembled staging is 1x the payload — no tarball stored anywhere.
        staging = self.download / "payload"
        extracted = sum(f.stat().st_size for f in staging.iterdir())
        self.assertEqual(extracted, sum(len(b) for b in BODIES.values()))

    def test_remainder_part_is_exercised(self) -> None:
        manifest = json.loads((self.release / "manifest.json").read_text())
        chunks = manifest["payload"]["chunks"]
        part_bytes = chunks[0]["length"]
        self.assertGreater(len(chunks), 1, "test should exercise multi-chunk")
        self.assertLess(
            chunks[-1]["length"], part_bytes,
            "the final part should be a remainder, not a full part",
        )

    def test_corrupt_part_is_refused(self) -> None:
        tampered = Path(self.tmp.name) / "tampered"
        shutil.copytree(self.release, tampered)
        part = tampered / "payload-000.part"
        raw = bytearray(part.read_bytes())
        raw[10] ^= 0x01
        part.write_bytes(bytes(raw))
        sign_dir(tampered)
        with self.assertRaises(artifacts.ArtifactError) as ctx:
            artifacts.acquire_artifacts(download_dir=self.download, directory=str(tampered))
        self.assertIn("payload-000.part", str(ctx.exception))

    def test_truncated_part_is_refused(self) -> None:
        tampered = Path(self.tmp.name) / "tampered"
        shutil.copytree(self.release, tampered)
        part = tampered / "payload-000.part"
        part.write_bytes(part.read_bytes()[:100])
        sign_dir(tampered)
        with self.assertRaises(artifacts.ArtifactError):
            artifacts.acquire_artifacts(download_dir=self.download, directory=str(tampered))

    def test_tar_hash_mismatch_is_refused(self) -> None:
        tampered = Path(self.tmp.name) / "tampered"
        shutil.copytree(self.release, tampered)
        manifest = json.loads((tampered / "manifest.json").read_text())
        # Parts re-verified fine; the *tar* block lies — the assembly's pass-1
        # hash check must catch it before anything is extracted.
        manifest["payload"]["tar"]["sha256"] = "11" * 32
        (tampered / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        sign_dir(tampered)
        with self.assertRaises(artifacts.ArtifactError) as ctx:
            artifacts.acquire_artifacts(download_dir=self.download, directory=str(tampered))
        self.assertIn("tar mismatch", str(ctx.exception))

    def test_enospc_cleans_staging(self) -> None:
        """A full staging filesystem is a legible refusal, and the staging area
        is cleaned (PACKAGING.md §4.1)."""
        artifacts.acquire_artifacts(download_dir=self.download, directory=str(self.release))
        staging = self.download / "payload"
        self.assertTrue(staging.exists(), "the first assemble should have staged the payload")
        # Simulate a full disk mid-extraction on the next assemble.
        enospc = OSError(28, "No space left on device")
        with mock.patch.object(
            artifacts, "_copy_member",
            side_effect=lambda member, src, dest, sha: (_ for _ in ()).throw(enospc),
        ):
            with self.assertRaises(artifacts.ArtifactError) as ctx:
                artifacts.acquire_artifacts(download_dir=self.download, directory=str(self.release))
        self.assertIn("staging filesystem is full", str(ctx.exception))
        self.assertFalse(staging.exists(), "staging must be cleaned after ENOSPC")

    def test_free_space_preflight_refuses(self) -> None:
        fake = mock.Mock(free=1024)  # 1 KiB free, payload needs ~90 KiB
        with mock.patch.object(artifacts.shutil, "disk_usage", return_value=fake):
            with self.assertRaises(artifacts.ArtifactError) as ctx:
                artifacts.acquire_artifacts(download_dir=self.download, directory=str(self.release))
        self.assertIn("not enough space", str(ctx.exception))
        self.assertIn("free", str(ctx.exception))

    def test_from_url(self) -> None:
        """`--url` with a file:// base hits the same assembly path."""
        files, version, _ = artifacts.acquire_artifacts(
            download_dir=self.download / "url", url=self.release.as_uri() + "/"
        )
        self.assertEqual(version, "test.01")
        for key, body in BODIES.items():
            self.assertEqual(files[key].read_bytes(), body, key)


class FlatManifestRegressionTest(unittest.TestCase):
    def test_flat_manifest_has_no_length_requirement(self) -> None:
        """A flat set (no payload block) must not be asked for artifact
        lengths — that requirement is payload-only (PACKAGING.md §4)."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "vmlinuz").write_bytes(b"kernel")
            (src / "initrd.img").write_bytes(b"initrd")
            (src / "root.squashfs").write_bytes(b"squash")
            (src / "manifest.json").write_text(
                json.dumps(
                    {
                        "version": "flat",
                        "artifacts": {
                            "vmlinuz": {"path": "vmlinuz"},
                            "initrd.img": {"path": "initrd.img"},
                            "root.squashfs": {"path": "root.squashfs"},
                        },
                    }
                ),
                encoding="utf-8",
            )
            subprocess.run(
                [str(SCRIPTS / "sign.sh"), str(src)],
                check=True, stdout=subprocess.DEVNULL,
                env={
                    **os.environ,
                    "SIGN_KEY": str(SMOKE_KEYS / "qemu-release-v1.priv"),
                    "SIGN_PUB_NAME": "qemu-release-v1.pub",
                },
            )
            files, version, _ = artifacts.acquire_artifacts(
                download_dir=Path(tmp) / "dl", directory=str(src)
            )
            self.assertEqual(version, "flat")
            self.assertTrue(files["vmlinuz"].is_file())


if __name__ == "__main__":
    unittest.main()
