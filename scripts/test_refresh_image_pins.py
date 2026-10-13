#!/usr/bin/env python3
"""Unit tests for refresh-image-pins.py.

No docker, no network: the registry query is injected, and the parsing that
surrounds it is what could actually get a wrong image onto an appliance.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "refresh_image_pins", SCRIPTS / "refresh-image-pins.py"
)
assert spec and spec.loader
rip = importlib.util.module_from_spec(spec)
sys.modules["refresh_image_pins"] = rip
spec.loader.exec_module(rip)

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64

SAMPLE = {
    "postgres": {"name": "pgvector/pgvector", "tag": "pg16", "digest": DIGEST_A},
    "valkey": {"name": "valkey/valkey", "tag": "8-alpine", "digest": DIGEST_A},
}

IMAGETOOLS_OUTPUT = f"""Name:      valkey/valkey:8-alpine
Digest:    {DIGEST_B}
MediaType: application/vnd.oci.image.index.v1+json
Manifests:
  Name:      valkey/valkey:8-alpine
  Digest:    sha256:{"c" * 64}
  MediaType: application/vnd.docker.distribution.manifest.v2+json
  Platform:  linux/amd64
"""


class ParseDigestTests(unittest.TestCase):
    def test_takes_the_index_digest_not_a_platform_one(self) -> None:
        self.assertEqual(rip.parse_digest(IMAGETOOLS_OUTPUT), DIGEST_B)

    def test_rejects_output_without_a_digest(self) -> None:
        with self.assertRaises(rip.RefreshError):
            rip.parse_digest("ERROR: manifest unknown")

    def test_rejects_a_truncated_digest(self) -> None:
        with self.assertRaises(rip.RefreshError):
            rip.parse_digest("Digest: sha256:deadbeef")

    def test_registry_digest_needs_docker(self) -> None:
        with mock.patch.object(rip.shutil, "which", return_value=None):
            with self.assertRaises(rip.RefreshError) as ctx:
                rip.registry_digest("valkey/valkey:8-alpine")
        self.assertIn("docker not found", str(ctx.exception))

    def test_registry_digest_propagates_a_failed_query(self) -> None:
        done = mock.Mock(returncode=1, stdout="", stderr="Error: no such host")
        with mock.patch.object(rip.shutil, "which", return_value="/usr/bin/docker"):
            with mock.patch.object(rip.subprocess, "run", return_value=done):
                with self.assertRaises(rip.RefreshError) as ctx:
                    rip.registry_digest("valkey/valkey:8-alpine")
        self.assertIn("no such host", str(ctx.exception))

    def test_registry_digest_runs_a_query_not_a_pull(self) -> None:
        done = mock.Mock(returncode=0, stdout=IMAGETOOLS_OUTPUT, stderr="")
        with mock.patch.object(rip.shutil, "which", return_value="/usr/bin/docker"):
            with mock.patch.object(rip.subprocess, "run", return_value=done) as m_run:
                self.assertEqual(rip.registry_digest("valkey/valkey:8-alpine"), DIGEST_B)
        args = m_run.call_args.args[0]
        self.assertEqual(args[:4], ["docker", "buildx", "imagetools", "inspect"])
        self.assertNotIn("pull", args)


class PlanTests(unittest.TestCase):
    def test_classifies_same_moved_and_new_tag(self) -> None:
        def resolver(name_tag: str) -> str:
            return DIGEST_A if "pgvector" in name_tag else DIGEST_B

        rows = rip.plan(SAMPLE, ["postgres", "valkey"], resolver)
        self.assertEqual([r["status"] for r in rows], ["same", "moved"])

    def test_set_tag_forces_new_tag_status(self) -> None:
        rows = rip.plan(
            SAMPLE,
            ["postgres"],
            lambda _nt: DIGEST_B,
            {"postgres": "pg17"},
        )
        self.assertEqual(rows[0]["status"], "new-tag")
        self.assertEqual(rows[0]["tag"], "pg17")

    def test_resolves_the_requested_tag(self) -> None:
        seen: list[str] = []

        def resolver(name_tag: str) -> str:
            seen.append(name_tag)
            return DIGEST_A

        rip.plan(SAMPLE, ["valkey"], resolver, {"valkey": "9-alpine"})
        self.assertEqual(seen, ["valkey/valkey:9-alpine"])

    def test_unknown_key_is_an_error(self) -> None:
        with self.assertRaises(rip.RefreshError):
            rip.plan(SAMPLE, ["nope"], lambda _nt: DIGEST_A)

    def test_unknown_set_tag_key_is_an_error(self) -> None:
        with self.assertRaises(rip.RefreshError):
            rip.plan(SAMPLE, ["postgres"], lambda _nt: DIGEST_A, {"nope": "x"})

    def test_garbage_digest_from_registry_is_rejected(self) -> None:
        with self.assertRaises(rip.RefreshError):
            rip.plan(SAMPLE, ["postgres"], lambda _nt: "sha512:abc")


class WriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()) / "container-images.json"
        self.tmp.write_text(json.dumps(SAMPLE, indent=2) + "\n", encoding="utf-8")

    def _run(self, argv: list[str], resolver) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = rip.main(argv + ["--pins", str(self.tmp)], resolver=resolver)
        return code, buf.getvalue()

    def test_check_reports_drift_without_writing(self) -> None:
        code, out = self._run(["--check"], lambda _nt: DIGEST_B)
        self.assertEqual(code, 1)
        self.assertIn("out of date", out)
        self.assertEqual(json.loads(self.tmp.read_text()), SAMPLE)

    def test_check_clean_exits_zero(self) -> None:
        code, out = self._run(["--check"], lambda _nt: DIGEST_A)
        self.assertEqual(code, 0)
        self.assertIn("all pins current", out)

    def test_write_updates_digest_and_keeps_order_and_others(self) -> None:
        code, _out = self._run([], lambda name_tag: DIGEST_B if "valkey" in name_tag else DIGEST_A)
        self.assertEqual(code, 0)
        data = json.loads(self.tmp.read_text())
        self.assertEqual(data["valkey"]["digest"], DIGEST_B)
        self.assertEqual(data["postgres"]["digest"], DIGEST_A, "untouched key must survive")
        self.assertEqual(list(data), ["postgres", "valkey"], "key order must survive")

    def test_write_hints_at_the_fixed_output_hash_file(self) -> None:
        _code, out = self._run(["valkey"], lambda _nt: DIGEST_B)
        self.assertIn("qemu-docker-cache.nix", out)

    def test_unresolvable_pin_writes_nothing(self) -> None:
        def resolver(name_tag: str) -> str:
            if "valkey" in name_tag:
                return DIGEST_B
            raise rip.RefreshError("registry unreachable")

        code, _out = self._run([], resolver)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(self.tmp.read_text()), SAMPLE)

    def test_no_drift_writes_nothing(self) -> None:
        before = self.tmp.read_text()
        code, out = self._run([], lambda _nt: DIGEST_A)
        self.assertEqual(code, 0)
        self.assertIn("nothing to write", out)
        self.assertEqual(self.tmp.read_text(), before)

    def test_bad_set_tag_syntax_is_a_usage_error(self) -> None:
        def stable(_name_tag: str) -> str:
            return DIGEST_A

        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                rip.main(["--set-tag", "postgres", "--pins", str(self.tmp)], resolver=stable)


class InTreePinsTests(unittest.TestCase):
    def test_shipped_pins_file_is_refreshable(self) -> None:
        """Guard the real file against a schema the rebaser cannot read."""
        pins = rip.load_pins(rip.PINS_PATH)
        self.assertIn("open_webui", pins)
        for key, pin in pins.items():
            self.assertEqual(set(pin), {"name", "tag", "digest"}, key)
        by_name = {pin["name"]: pin["digest"] for pin in pins.values()}
        rows = rip.plan(pins, sorted(pins), lambda nt: by_name[nt.split(":")[0]])
        self.assertTrue(all(r["status"] == "same" for r in rows), rows)


if __name__ == "__main__":
    unittest.main()
