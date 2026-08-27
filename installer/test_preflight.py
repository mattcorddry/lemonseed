#!/usr/bin/env python3
"""Unit tests for installer host preflight (no root, no hardware)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

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


if __name__ == "__main__":
    raise SystemExit(unittest.main())
