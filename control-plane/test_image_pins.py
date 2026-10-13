#!/usr/bin/env python3
"""Tests for the container-image pin contract (M6.6 S5.1).

Two jobs. First, the module itself: a malformed pin must fail loudly at the
first read, never silently resolve to a different image. Second, the *contract*
around it — these are source-text assertions over the repo, because the failure
mode we are closing is "the same image named in four places and nothing
checking they agreed". Nix cannot be evaluated from a unit test, so the
rendered-config assertions live in the QEMU smoke test and the textual ones
(Nix reads the pins file, no boot-path pulls) live here; precedent for reading
Nix as text is PackagingTests in test_control_plane.py.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CP = Path(__file__).resolve().parent
ROOT = CP.parent

import image_pins  # noqa: E402

REQUIRED_KEYS = ("open_webui", "postgres", "valkey")

# Files that may name a container image. Docs, proposals and tests are excluded
# on purpose: a doc quoting an old digest is stale prose, not a running system.
SCHEMA_DIRS = ("nixos", "installer", "boot", "scripts")
SCHEMA_FILES = ("flake.nix", "Makefile")


def _stub_file(text: str) -> Path:
    """A pins file on disk, for malformed-input tests."""
    tmp = Path(tempfile.mkdtemp()) / "container-images.json"
    tmp.write_text(text, encoding="utf-8")
    return tmp


class PinsFileTests(unittest.TestCase):
    def setUp(self) -> None:
        image_pins.reset_cache()

    def tearDown(self) -> None:
        image_pins.reset_cache()

    def test_every_required_image_is_pinned(self) -> None:
        pins = image_pins.load_pins()
        for key in REQUIRED_KEYS:
            self.assertIn(key, pins, f"{key} must be pinned")

    def test_entries_validate_and_refs_are_well_formed(self) -> None:
        for key in REQUIRED_KEYS:
            pin = image_pins.entry(key)
            self.assertEqual(set(pin), {"name", "tag", "digest"})
            self.assertRegex(pin["digest"], r"^sha256:[0-9a-f]{64}$")
            tag_ref = image_pins.tag_ref(key)
            digest_ref = image_pins.digest_ref(key)
            self.assertEqual(tag_ref, f"{pin['name']}:{pin['tag']}")
            self.assertEqual(digest_ref, f"{pin['name']}@{pin['digest']}")
            # A digest reference must never carry a tag, and vice versa: mixing
            # them is what makes `docker run` silently pick the wrong image.
            self.assertNotIn("@", tag_ref)
            self.assertNotIn(":", pin["tag"])
            self.assertEqual(image_pins.version_label(key), pin["tag"])

    def test_tags_are_release_pins_not_floating(self) -> None:
        """`:latest`/`:main` may be fine for CI but never for an appliance."""
        for key in REQUIRED_KEYS:
            tag = image_pins.entry(key)["tag"]
            self.assertNotIn(tag, ("latest", "main", "stable", "edge"))
            self.assertRegex(tag, r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    def test_json_is_valid_and_object_shaped(self) -> None:
        raw = json.loads(image_pins.PINS_FILE.read_text(encoding="utf-8"))
        self.assertIsInstance(raw, dict)
        for key, item in raw.items():
            self.assertIsInstance(item, dict, key)
            self.assertEqual(set(item), {"name", "tag", "digest"}, key)

    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(image_pins.ImagePinError):
            image_pins.entry("definitely_not_an_image")

    def test_malformed_entries_are_rejected(self) -> None:
        cases = {
            "bad digest": '{"x": {"name": "n", "tag": "t", "digest": "sha256:deadbeef"}}',
            "digest without prefix": '{"x": {"name": "n", "tag": "t", "digest": "abc123"}}',
            "floating tag": '{"x": {"name": "n", "tag": "latest", "digest": "%s"}}'
            % ("sha256:" + "0" * 64),
            "tag with digest": '{"x": {"name": "n", "tag": "a@b", "digest": "%s"}}'
            % ("sha256:" + "0" * 64),
            "missing tag": '{"x": {"name": "n", "digest": "%s"}}' % ("sha256:" + "0" * 64),
            "empty object": "{}",
            "not an object": "[]",
            "not json": "{name: ",
        }
        for label, text in cases.items():
            with self.subTest(label):
                path = _stub_file(text)
                with self.assertRaises(image_pins.ImagePinError):
                    image_pins.entry("x", path)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(image_pins.ImagePinError):
            image_pins.load_pins(Path("/nonexistent/container-images.json"))

    def test_cache_is_used_and_resettable(self) -> None:
        """Cached means a rewritten file is not re-read until we say so."""
        digest = "sha256:" + "0" * 64
        path = _stub_file('{"x": {"name": "n", "tag": "t", "digest": "%s"}}' % digest)
        with mock.patch.object(image_pins, "PINS_FILE", path):
            self.assertEqual(image_pins.entry("x")["tag"], "t")
            path.write_text(
                '{"x": {"name": "n", "tag": "t2", "digest": "%s"}}' % digest,
                encoding="utf-8",
            )
            self.assertEqual(image_pins.entry("x")["tag"], "t", "cache should be serving")
            image_pins.reset_cache()
            self.assertEqual(image_pins.entry("x")["tag"], "t2")


class SingleSourceOfTruthTests(unittest.TestCase):
    """The pin must be named in exactly one place per language.

    A literal image name/tag copied into Nix, the flake or CP Python is how the
    appliance ended up running three different images from four sources.
    """

    def _sources(self) -> list[Path]:
        files = [ROOT / name for name in SCHEMA_FILES]
        for sub in SCHEMA_DIRS:
            files.extend(p for p in (ROOT / sub).rglob("*") if p.is_file())
        files.extend(sorted(CP.glob("*.py")))
        kept = [
            p
            for p in files
            if p.suffix in {".nix", ".py", ".sh", ".json", ".conf", ".md"}
            and p.name != "container-images.json"
            and not p.name.startswith("test_")
            and "proposals" not in p.parts
        ]
        # Guard against a vacuous pass: the scan is only meaningful if it runs
        # over the real appliance sources.
        self.assertGreater(len(kept), 30, f"scan set looks wrong: {kept}")
        return kept

    def test_no_image_ref_literal_outside_the_pins_file(self) -> None:
        pins = image_pins.load_pins()
        literals = set()
        for pin in pins.values():
            literals.add(f"{pin['name']}:{pin['tag']}")
            literals.add(f"{pin['name']}@{pin['digest']}")
        offenders: list[str] = []
        for path in self._sources():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for literal in literals:
                if literal in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {literal}")
        self.assertEqual(
            offenders,
            [],
            "image references belong in control-plane/container-images.json only",
        )

    def test_no_floating_refs_in_appliance_sources(self) -> None:
        floating = re.compile(r"(?:open-webui|pgvector|valkey|redis)/?[\w.-]*:(latest|main|nightly)\b")
        offenders: list[str] = []
        for path in self._sources():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for match in floating.finditer(text):
                offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)}")
        self.assertEqual(offenders, [])

    def test_nix_reads_the_pins_file(self) -> None:
        """Nix must derive refs from the JSON, not from re-typed strings."""
        for rel in ("nixos/modules/data-services.nix", "nixos/modules/qemu-docker-cache.nix"):
            with self.subTest(rel):
                text = (ROOT / rel).read_text(encoding="utf-8")
                self.assertIn("builtins.fromJSON", text)
                self.assertIn("container-images.json", text)
        flake = (ROOT / "flake.nix").read_text(encoding="utf-8")
        self.assertIn("container-images.json", flake)

    def test_cp_installs_the_pins_file_and_module(self) -> None:
        """A missing entry here boots-loops the CP while make test stays green."""
        text = (ROOT / "nixos" / "modules" / "control-plane.nix").read_text(encoding="utf-8")
        for name in ("image_pins.py", "container-images.json"):
            self.assertIn(f"${{cpSrc}}/{name}", text, f"{name} is not installed")


class BootPathContractTests(unittest.TestCase):
    """S5.3: an unbounded `docker pull` must not sit on the boot path."""

    def setUp(self) -> None:
        self.text = (ROOT / "nixos" / "modules" / "data-services.nix").read_text(
            encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.text = ""

    def test_no_boot_time_pulls(self) -> None:
        # Line-anchored: the file explains at length why the pull was removed,
        # and those comments must not count as violations.
        self.assertIsNone(re.search(r"^\s*ExecStartPre\b", self.text, re.MULTILINE))
        self.assertNotIn("}/bin/docker pull", self.text)

    def test_start_timeout_is_local_work_only(self) -> None:
        self.assertNotIn('TimeoutStartSec = "1800"', self.text)
        self.assertEqual(len(re.findall(r'TimeoutStartSec = "120"', self.text)), 2)

    def test_start_scripts_run_the_resolved_ref_and_hand_off(self) -> None:
        self.assertIn("resolve_image", self.text)
        self.assertIn("needs_recreate", self.text)
        # Two hand-offs (postgres + valkey): no local image -> CP supervisor pulls.
        self.assertEqual(len(re.findall(r"the control plane will pull it", self.text)), 2)

    def test_every_start_path_announces_the_image(self) -> None:
        """Four paths per service must log the reference, not just `docker run`.

        The containers use `--restart unless-stopped`, so dockerd normally has
        them up again before these oneshot units run. An announcement that only
        sits next to `docker run` is therefore invisible on every boot after
        the first — which is what broke the QEMU assertion for S5.
        """
        for label in ("lemonseed-postgres", "lemonseed-valkey"):
            announces = re.findall(rf'echo "{label}: image ', self.text)
            self.assertEqual(
                len(announces),
                4,
                f"{label} does not announce its image on every start path",
            )
        self.assertIn("container_image", self.text)

    def test_image_drift_is_checked_by_reference_not_image_id(self) -> None:
        """`.Config.Image` is a string compare; image IDs disagree across stores."""
        self.assertIn("{{.Config.Image}}", self.text)
        self.assertNotIn("{{.Image}}", self.text)

    def test_owui_service_still_has_a_pull_budget_constant(self) -> None:
        # The Open WebUI pull runs off the boot path but still needs a cap sized
        # to its transfer, or a stalled connection hangs the supervisor tick.
        text = (CP / "modules.py").read_text(encoding="utf-8")
        match = re.search(r"OWUI_PULL_TIMEOUT_SEC = (\d+)", text)
        self.assertIsNotNone(match)
        self.assertGreaterEqual(int(match.group(1)), 1800)


if __name__ == "__main__":
    unittest.main()
