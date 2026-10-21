#!/usr/bin/env python3
"""Unit tests for lemonseed-update / lemonseed_artifacts (no root required)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import typing
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import lemonseed_artifacts as artifacts  # noqa: E402
import lemonseed_update as update  # noqa: E402

# The smoke trust set shipped in-tree: its private key is tracked (QEMU-only,
# never a release key), so unit tests and root smoke tests can sign local
# artifact sets against it. The verifier is pointed at it before any test
# fetches a manifest.
SMOKE_KEYS = Path(__file__).resolve().parents[1] / "keys" / "qemu"
SMOKE_PRIV = SMOKE_KEYS / "qemu-release-v1.priv"
os.environ.setdefault("LEMONSEED_RELEASE_KEYS_DIR", str(SMOKE_KEYS))


def write_signed_manifest(directory: Path, manifest: dict) -> None:
    """Write manifest.json with the signing metadata, then sign in place —
    exactly what the shipped sets look like after scripts/sign.sh."""
    manifest = dict(manifest)
    manifest["signing"] = {
        "scheme": "detached-ed25519",
        "enabled": True,
        "public_key": "qemu-release-v1.pub",
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    subprocess.run(
        [
            "openssl", "pkeyutl", "-sign", "-inkey", str(SMOKE_PRIV), "-rawin",
            "-in", str(directory / "manifest.json"),
            "-out", str(directory / "manifest.json.sig"),
        ],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


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
            write_signed_manifest(src, manifest)
            got, version, _ = artifacts.acquire_artifacts(
                download_dir=dest,
                directory=str(src),
            )
            self.assertEqual(version, "test.01")
            for name in files:
                self.assertTrue(got[name].is_file())

    def test_acquire_local_manifest_requires_signature(self) -> None:
        """A local set whose manifest exists but is unsigned is refused —
        there is no hash-only path (M8 P1, D10)."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dest = Path(tmp) / "dl"
            src.mkdir()
            (src / "vmlinuz").write_bytes(b"kernel-bytes")
            (src / "initrd.img").write_bytes(b"initrd-bytes")
            (src / "root.squashfs").write_bytes(b"squash-bytes")
            (src / "manifest.json").write_text(
                json.dumps({"version": "test.01", "artifacts": {}}), encoding="utf-8"
            )
            with self.assertRaises(artifacts.ArtifactError) as ctx:
                artifacts.acquire_artifacts(download_dir=dest, directory=str(src))
            self.assertIn("signature", str(ctx.exception))

    def test_acquire_rejects_bad_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            dest = Path(tmp) / "dl"
            src.mkdir()
            (src / "vmlinuz").write_bytes(b"kernel")
            (src / "initrd.img").write_bytes(b"initrd")
            (src / "root.squashfs").write_bytes(b"squash")
            manifest = {
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
            write_signed_manifest(src, manifest)
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
            write_signed_manifest(src, manifest)
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
            write_signed_manifest(arts, manifest)

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
                rc = update.cmd_check("http://base/", None, record=True)
                self.assertEqual(rc, 0)
            self.assertTrue(latest_file.is_file())
            record = json.loads(latest_file.read_text(encoding="utf-8"))
            self.assertEqual(record["latest"], "test.02")
            self.assertEqual(record["running"], "test.01")
            self.assertEqual(record["channel"], "stable")
            self.assertIn("checked_at", record)

    def test_check_discovers_the_channel_release(self) -> None:
        """No --url: the channel resolves to a release and the manifest comes
        from that release's asset base (M8 P3)."""
        with tempfile.TemporaryDirectory() as tmp:
            latest_file = Path(tmp) / "latest-update.json"
            with mock.patch.object(
                update,
                "resolve_release_base",
                return_value=("http://cdn/v-test.02/", "v-test.02"),
            ) as resolved, mock.patch.object(
                update,
                "fetch_manifest",
                return_value=({"version": "test.02"}, "http://cdn/v-test.02/"),
            ) as fetched, mock.patch.object(
                update, "running_version", return_value="test.01"
            ), mock.patch.object(
                update, "LATEST_FILE", latest_file
            ):
                rc = update.cmd_check(None, "beta", record=True)
                self.assertEqual(rc, 0)
            resolved.assert_called_once_with("beta")
            self.assertIn("http://cdn/v-test.02/", fetched.call_args[0][0])
            record = json.loads(latest_file.read_text(encoding="utf-8"))
            self.assertEqual(record["channel"], "beta")
            self.assertEqual(record["latest"], "test.02")

    def test_no_candidate_on_the_channel_is_up_to_date_not_an_error(self) -> None:
        """Only prereleases published with stable selected: the same answer as
        a candidate no newer than running — no offer, exit 0 (D-C2/Q5)."""
        with tempfile.TemporaryDirectory() as tmp:
            latest_file = Path(tmp) / "latest-update.json"
            with mock.patch.object(
                update, "resolve_release_base", return_value=None
            ), mock.patch.object(update, "running_version", return_value="test.01"), mock.patch.object(
                update, "LATEST_FILE", latest_file
            ):
                rc = update.cmd_check(None, "stable", record=True)
                self.assertEqual(rc, 0)
            record = json.loads(latest_file.read_text(encoding="utf-8"))
            self.assertEqual(record["latest"], "test.01")
            self.assertEqual(record["channel"], "stable")


class UpdateSwitchTests(unittest.TestCase):
    """D-C2: the channel only filters candidates; the version comparison is
    the only update decision, so switching beta → stable never downgrades."""

    def _apply(self, *, channel: str | None, force: bool) -> typing.Any:
        return update.cmd_apply(None, None, channel=channel, reboot=False, force=force)

    def test_switching_to_stable_never_downgrades(self) -> None:
        with mock.patch.object(update, "require_root"), mock.patch.object(
            update, "preflight"
        ), mock.patch.object(update, "current_slot", return_value="a"), mock.patch.object(
            update, "acquire_artifacts", return_value=({}, "2026.01", {})
        ), mock.patch.object(update, "running_version", return_value="2026.02"):
            with self.assertRaises(update.UpdateError) as ctx:
                self._apply(channel="stable", force=False)
            self.assertIn("refusing the downgrade", str(ctx.exception))

    def test_force_still_writes_the_older_release(self) -> None:
        with mock.patch.object(update, "require_root"), mock.patch.object(
            update, "preflight"
        ), mock.patch.object(update, "current_slot", return_value="a"), mock.patch.object(
            update, "inactive_slot", return_value="b"
        ), mock.patch.object(
            update, "acquire_artifacts", return_value=({}, "2026.01", {})
        ), mock.patch.object(update, "running_version", return_value="2026.02"), mock.patch.object(
            update, "write_inactive_slot"
        ), mock.patch.object(update, "record_attempt"), mock.patch.object(update, "cleanup_payload"):
            self.assertEqual(self._apply(channel="stable", force=True), 0)

    def test_url_and_channel_are_a_usage_error(self) -> None:
        with mock.patch.object(update, "require_root"):
            with self.assertRaises(update.UpdateError) as ctx:
                self._apply_url_and_channel()
            self.assertIn("use either --url or --channel", str(ctx.exception))

    def _apply_url_and_channel(self) -> typing.Any:
        return update.cmd_apply("http://base/", None, channel="beta", reboot=False, force=False)

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


class DiscoveryTests(unittest.TestCase):
    """D5: the GitHub listing is the whole discovery surface. The stubs are
    exactly the API JSON shape; every refusal is legible, never a traceback."""

    RELEASES = [
        {  # oldest first, so the sort has something to prove
            "tag_name": "v-test.01",
            "draft": False,
            "prerelease": False,
            "created_at": "2026-01-01T00:00:00Z",
            "assets": [
                {"name": "manifest.json", "browser_download_url": "http://cdn/v-test.01/manifest.json"}
            ],
        },
        {  # a draft is never a candidate, whatever its date
            "tag_name": "v-test.99",
            "draft": True,
            "prerelease": False,
            "created_at": "2026-02-01T00:00:00Z",
            "assets": [],
        },
        {  # the prerelease: beta's newest, invisible to stable
            "tag_name": "v-test.03",
            "draft": False,
            "prerelease": True,
            "created_at": "2026-03-01T00:00:00Z",
            "assets": [
                {"name": "manifest.json", "browser_download_url": "http://cdn/v-test.03/manifest.json"}
            ],
        },
        {  # stable's newest
            "tag_name": "v-test.02",
            "draft": False,
            "prerelease": False,
            "created_at": "2026-02-15T00:00:00Z",
            "assets": [
                {"name": "manifest.json", "browser_download_url": "http://cdn/v-test.02/manifest.json"}
            ],
        },
    ]

    def _serve(self, payload, *, status=200, headers=None):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        holder: dict = {}

        class Resp:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(url, timeout=60):
            holder["url"] = url
            if status != 200:
                raise urllib.error.HTTPError(url, status, "rate limited", headers or {}, io.BytesIO(body))
            return Resp(body)

        return holder, mock.patch.object(artifacts, "_urlopen", side_effect=fake_urlopen)

    def test_stable_picks_the_newest_release(self) -> None:
        holder, patch = self._serve(self.RELEASES)
        with patch:
            base, tag = artifacts.resolve_release_base("stable")
        self.assertEqual((base, tag), ("http://cdn/v-test.02/", "v-test.02"))
        self.assertIn("api.github.com", holder["url"])

    def test_beta_picks_the_newest_prerelease(self) -> None:
        _holder, patch = self._serve(self.RELEASES)
        with patch:
            base, tag = artifacts.resolve_release_base("beta")
        self.assertEqual((base, tag), ("http://cdn/v-test.03/", "v-test.03"))

    def test_no_candidates_is_none_not_an_error(self) -> None:
        only_rc = [r for r in self.RELEASES if r["prerelease"]]
        _holder, patch = self._serve(only_rc)
        with patch:
            self.assertIsNone(artifacts.resolve_release_base("stable"))

    def test_malformed_answer_is_legible(self) -> None:
        _holder, patch = self._serve({"no": "list"})
        with patch:
            with self.assertRaises(artifacts.ArtifactError) as ctx:
                artifacts.resolve_release_base("stable")
        self.assertIn("unexpected answer", str(ctx.exception))

    def test_rate_limit_names_the_shared_ip_quota(self) -> None:
        _holder, patch = self._serve(b"[]", status=403, headers={"Retry-After": "42"})
        with patch:
            with self.assertRaises(artifacts.ArtifactError) as ctx:
                artifacts.resolve_release_base("stable")
        self.assertIn("60/hour", str(ctx.exception))
        self.assertIn("42", str(ctx.exception))

    def test_a_release_without_a_manifest_asset_is_skipped_legibly(self) -> None:
        broken = [
            {
                "tag_name": "v-broken",
                "draft": False,
                "prerelease": False,
                "created_at": "2026-04-01T00:00:00Z",
                "assets": [{"name": "something-else.zip", "browser_download_url": "http://cdn/x"}],
            }
        ]
        _holder, patch = self._serve(broken)
        with patch:
            with self.assertRaises(artifacts.ArtifactError) as ctx:
                artifacts.resolve_release_base("stable")
        self.assertIn("v-broken", str(ctx.exception))


class ChannelResolutionTests(unittest.TestCase):
    """D-C4: flag > env > the materialized file > the ESP stub > stable."""

    def test_flag_wins_over_everything(self) -> None:
        with mock.patch.dict(os.environ, {"LEMONSEED_CHANNEL": "stable"}), mock.patch.object(
            artifacts, "CHANNEL_FILE", Path("/nonexistent/channel")
        ), mock.patch.object(artifacts, "cmdline_val", return_value="beta"):
            self.assertEqual(artifacts.resolve_channel("beta"), "beta")

    def test_env_beats_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel_file = Path(tmp) / "channel"
            channel_file.write_text("stable\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"LEMONSEED_CHANNEL": "beta"}), mock.patch.object(
                artifacts, "CHANNEL_FILE", channel_file
            ):
                self.assertEqual(artifacts.resolve_channel(None), "beta")

    def test_the_materialized_file_beats_the_esp_stub(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel_file = Path(tmp) / "channel"
            channel_file.write_text("beta\n", encoding="utf-8")
            with mock.patch.object(artifacts, "CHANNEL_FILE", channel_file), mock.patch.object(
                artifacts, "cmdline_val", return_value="stable"
            ):
                self.assertEqual(artifacts.resolve_channel(None), "beta")

    def test_the_esp_stub_beats_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(artifacts, "CHANNEL_FILE", Path(tmp) / "missing"), mock.patch.object(
                artifacts, "cmdline_val", return_value="beta"
            ):
                self.assertEqual(artifacts.resolve_channel(None), "beta")

    def test_garbage_falls_back_to_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel_file = Path(tmp) / "channel"
            channel_file.write_text("nonsense\n", encoding="utf-8")
            with mock.patch.object(artifacts, "CHANNEL_FILE", channel_file), mock.patch.object(
                artifacts, "cmdline_val", return_value=""
            ):
                self.assertEqual(artifacts.resolve_channel(None), "stable")

    def test_acquire_uses_the_resolved_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                artifacts, "resolve_release_base", return_value=("http://cdn/v1/", "v1")
            ) as resolved, mock.patch.object(
                artifacts,
                "fetch_manifest",
                return_value=({"version": "v1", "payload": {"chunks": []}}, "http://cdn/v1/"),
            ), mock.patch.object(artifacts, "assemble_payload", return_value={}) as assembled:
                _files, version, _manifest = artifacts.acquire_artifacts(
                    download_dir=Path(tmp), channel="beta"
                )
            resolved.assert_called_once_with("beta")
            self.assertEqual(assembled.call_args.kwargs["base_url"], "http://cdn/v1/")
            self.assertEqual(version, "v1")

    def test_acquire_refuses_when_the_channel_has_nothing(self) -> None:
        with mock.patch.object(artifacts, "resolve_release_base", return_value=None):
            with self.assertRaises(artifacts.ArtifactError) as ctx:
                artifacts.acquire_artifacts(download_dir=Path("/tmp"), channel="beta")
        self.assertIn("No release on channel 'beta'", str(ctx.exception))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
