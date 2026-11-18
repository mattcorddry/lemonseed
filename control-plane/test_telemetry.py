"""Unit tests for the app-bar telemetry math (M9 P4 scope creep).

The NET bar's sampling loop and the LOAD bar's max(cpu, gpu) rule are the
product here; sysfs/ip/ethtool are stubbed or bypassed.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import stub


_NET_CLOCK = {"t": 100.0}


def feed(samples: list[tuple[int, int]], link_mbps: float = 1000.0):
    """Drive _net_bar through a sequence of (tx, rx) counter snapshots.

    The clock persists across calls on purpose: module-level EWMA state means
    each feed() window continues from the previous one.
    """
    out = []

    def counters(iface):  # type: ignore[no-untyped-def]
        return samples.pop(0)

    with (
        mock.patch.object(stub, "_primary_iface", return_value="eth0"),
        mock.patch.object(stub, "_counters", side_effect=counters),
        mock.patch.object(stub, "_link_mbps", return_value=link_mbps),
        mock.patch.object(stub.time, "monotonic", side_effect=lambda: _NET_CLOCK.__setitem__(
            "t", _NET_CLOCK["t"] + 5.0) or _NET_CLOCK["t"]),
    ):
        for _ in range(len(samples)):
            out.append(stub._net_bar())
    return out


class NetBarTests(unittest.TestCase):
    def setUp(self) -> None:
        stub._net_state.clear()
        _NET_CLOCK["t"] = 100.0

    def test_first_sample_seeds_only(self):
        # The first call has no previous sample: it returns nothing to show.
        with (
            mock.patch.object(stub, "_primary_iface", return_value="eth0"),
            mock.patch.object(stub, "_counters", return_value=(10, 10)),
            mock.patch.object(stub.time, "monotonic", return_value=100.0),
        ):
            self.assertEqual(stub._net_bar(), (None, None))

    def test_max_of_tx_and_rx(self):
        # Only rx moves: the bar must reflect Rx despite a silent Tx side.
        results = feed([(0, 0), (0, 62_500_000)])  # 10% of the 1 Gbps link over 5 s
        _pct, frac = [r for r in results if r[0] is not None][0]
        self.assertAlmostEqual(frac, 0.1, places=3)

    def test_log_scale_puts_ten_percent_of_link_near_half(self):
        results = feed([(0, 0), (0, 62_500_000)])
        pct = [p for p, _f in results if p is not None][0]
        self.assertEqual(pct, 52)  # log10(1 + 99*0.1)/2 * 100

    def test_full_link_saturates(self):
        results = feed([(0, 0), (0, 625_000_000)])
        pct = [p for p, _f in results if p is not None][0]
        self.assertEqual(pct, 100)

    def test_idle_link_reads_zero_not_none(self):
        results = feed([(0, 0), (0, 0)])
        pct = [p for p, _f in results if p is not None][0]
        self.assertEqual(pct, 0)

    def test_ewma_smooths_a_burst(self):
        # A saturating burst then silence: the bar must still show motion
        # afterwards instead of snapping to zero (alpha = 0.3).
        feed([(0, 0), (0, 625_000_000)])
        results = feed([(0, 937_500_000)])  # +50% of link this window
        pct2, frac2 = [r for r in results if r[0] is not None][0]
        self.assertAlmostEqual(frac2, 0.85, places=2)  # 0.7*1 + 0.3*0.5
        self.assertGreater(pct2, 80)

    def test_counter_reset_keeps_last_value(self):
        feed([(0, 0), (0, 62_500_000)])
        # rx went backwards (interface restart): keep the smoothed value.
        with (
            mock.patch.object(stub, "_primary_iface", return_value="eth0"),
            mock.patch.object(stub, "_counters", return_value=(0, 1)),
            mock.patch.object(stub.time, "monotonic", return_value=999.0),
        ):
            pct, frac = stub._net_bar()
        self.assertIsNotNone(pct)
        self.assertEqual(pct, 52)

    def test_interface_change_resets_the_window(self):
        feed([(0, 0), (0, 62_500_000)])
        with (
            mock.patch.object(stub, "_primary_iface", return_value="wlan0"),
            mock.patch.object(stub, "_counters", return_value=(7, 7)),
            mock.patch.object(stub.time, "monotonic", return_value=999.0),
        ):
            self.assertEqual(stub._net_bar(), (None, None))

    def test_link_rate_fallback_when_ethtool_is_useless(self):
        # speed -1 (unknown) must not divide by zero: it falls back to 1 Gbps.
        with mock.patch.object(
            stub.subprocess, "run",
            return_value=mock.Mock(stdout='[{"speed": -1}]'),
        ):
            self.assertEqual(stub._link_mbps("eth0"), 1000.0)


class LoadBarTests(unittest.TestCase):
    def _snapshot(self, load1: float, threads: int = 8, gpu=None) -> dict:
        with (
            mock.patch.object(stub.os, "getloadavg", return_value=(load1, 0, 0)),
            mock.patch.object(stub.hardware, "_cpu_info",
                              return_value={"threads": threads}),
            mock.patch.object(stub, "_gpu_busy_pct", return_value=gpu),
            mock.patch.object(stub.hardware, "_disk_free",
                              return_value={"total_bytes": 2, "used_bytes": 1}),
            mock.patch.object(stub, "_net_bar", return_value=(None, None)),
        ):
            return stub.telemetry_snapshot()

    def test_cpu_only_when_no_gpu(self):
        snap = self._snapshot(0.8)
        self.assertEqual(snap["load_pct"], 10.0)  # 0.8 / 8 threads
        self.assertIsNone(snap["gpu_pct"])

    def test_gpu_dominates_the_bar_when_busier(self):
        snap = self._snapshot(0.4, threads=16, gpu=87.0)
        # cpu would be 2.5%; the GPU is doing the work: max wins.
        self.assertEqual(snap["load_pct"], 87.0)

    def test_gpu_absent_in_qemu_profiles(self):
        snap = self._snapshot(1.6, threads=4, gpu=None)
        self.assertEqual(snap["load_pct"], 40.0)


if __name__ == "__main__":
    unittest.main()
