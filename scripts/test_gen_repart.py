#!/usr/bin/env python3
"""Unit tests for gen-repart.py (no root required)."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("gen_repart", SCRIPTS / "gen-repart.py")
assert spec and spec.loader
gen_repart = importlib.util.module_from_spec(spec)
sys.modules["gen_repart"] = gen_repart
spec.loader.exec_module(gen_repart)


class GenRepartTests(unittest.TestCase):
    def test_size_to_bytes(self) -> None:
        self.assertEqual(gen_repart.size_to_bytes("512M"), str(512 * 1024**2))
        self.assertEqual(gen_repart.size_to_bytes("8G"), str(8 * 1024**3))
        self.assertIsNone(gen_repart.size_to_bytes("grow"))

    def test_role_type_aliases(self) -> None:
        self.assertEqual(gen_repart.role_type({"type": "ef00"}), "esp")
        self.assertEqual(gen_repart.role_type({"role": "lvm-pv"}), gen_repart.LVM_TYPE_UUID)

    def test_parse_simple_yaml_fallback(self) -> None:
        text = """
schema: test
partitions:
  - name: esp
    size: 512M
    type: ef00
  - name: root-a
    size: 4G
    role: squashfs-root
"""
        data = gen_repart.parse_simple_yaml(text)
        self.assertEqual(data["schema"], "test")
        self.assertEqual(len(data["partitions"]), 2)
        self.assertEqual(data["partitions"][0]["name"], "esp")

    def test_emit_repart_grow_partition(self) -> None:
        conf = gen_repart.emit_repart({"name": "user-data", "size": "grow", "role": "lvm-pv"}, 3)
        self.assertIn("SizeMinBytes=64M", conf)
        self.assertIn("Weight=1000", conf)


if __name__ == "__main__":
    raise SystemExit(unittest.main())
