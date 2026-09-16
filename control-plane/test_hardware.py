#!/usr/bin/env python3
"""Unit tests for AMD/Vulkan hardware detection and the vendor-aware planner.

No GPU, Vulkan, or lemond required — vulkaninfo / nvidia-smi / lemond HTTP are
mocked.
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))

import catalog  # noqa: E402
import hardware  # noqa: E402

# A representative `vulkaninfo --summary` block for a single AMD device.
# Real format: "GPU0:" header line, then tab-indented "key = value" lines.
VULKANINFO_SUMMARY = """GPU0:
apiVersion     = 1.4.300
driverID       = AMD
driverName     = radv
deviceName     = AMD Radeon RX 6800
deviceType     = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU
"""


class VulkanDetectionTests(unittest.TestCase):
    @mock.patch.object(
        hardware.subprocess,
        "check_output",
        return_value=VULKANINFO_SUMMARY,
    )
    def test_parses_amd_device(self, _co: mock.Mock) -> None:
        gpus = hardware._amd_gpus_vulkaninfo()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["vendor"], "amd")
        self.assertEqual(gpus[0]["name"], "AMD Radeon RX 6800")
        self.assertEqual(gpus[0]["device_type"], "PHYSICAL_DEVICE_TYPE_DISCRETE_GPU")
        # vulkaninfo does not expose VRAM.
        self.assertEqual(gpus[0]["vram_gb"], 0)

    @mock.patch.object(
        hardware.subprocess,
        "check_output",
        side_effect=FileNotFoundError("vulkaninfo not installed"),
    )
    def test_missing_tool_returns_empty(self, _co: mock.Mock) -> None:
        self.assertEqual(hardware._amd_gpus_vulkaninfo(), [])

    @mock.patch.object(
        hardware.subprocess,
        "check_output",
        side_effect=subprocess.CalledProcessError(1, "vulkaninfo"),
    )
    def test_nonzero_exit_returns_empty(self, _co: mock.Mock) -> None:
        self.assertEqual(hardware._amd_gpus_vulkaninfo(), [])


class LlvmpipeFilterTests(unittest.TestCase):
    """vulkaninfo CPU-type devices (llvmpipe) must be excluded."""

    VULKANINFO_LLVMPIPE = """GPU0:
apiVersion     = 1.4.300
driverID       = MESA
driverName     = llvmpipe
deviceName     = llvmpipe (LLVM 21.1.8, 256 bits)
deviceType     = PHYSICAL_DEVICE_TYPE_CPU
"""

    VULKANINFO_IGPU = """GPU0:
apiVersion     = 1.4.300
driverID       = AMD
driverName     = radv
deviceName     = AMD Radeon 8060S Graphics
deviceType     = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
"""

    # Two devices: llvmpipe (CPU) + real iGPU. The parser must use the
    # GPU header lines as device boundaries (deviceType appears before
    # deviceName within each block).
    VULKANINFO_BOTH = """GPU0:
apiVersion     = 1.4.300
driverID       = MESA
driverName     = llvmpipe
deviceName     = llvmpipe (LLVM 21.1.8, 256 bits)
deviceType     = PHYSICAL_DEVICE_TYPE_CPU
GPU1:
apiVersion     = 1.4.300
driverID       = AMD
driverName     = radv
deviceName     = AMD Radeon 8060S Graphics
deviceType     = PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU
"""

    @mock.patch.object(
        hardware.subprocess,
        "check_output",
        return_value=VULKANINFO_LLVMPIPE,
    )
    def test_llvmpipe_only_returns_empty(self, _co: mock.Mock) -> None:
        # A box with only the software renderer has no real GPU.
        self.assertEqual(hardware._amd_gpus_vulkaninfo(), [])

    @mock.patch.object(
        hardware.subprocess,
        "check_output",
        return_value=VULKANINFO_IGPU,
    )
    def test_integrated_gpu_is_kept(self, _co: mock.Mock) -> None:
        gpus = hardware._amd_gpus_vulkaninfo()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["name"], "AMD Radeon 8060S Graphics")
        self.assertEqual(gpus[0]["device_type"], "PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU")

    @mock.patch.object(
        hardware.subprocess,
        "check_output",
        return_value=VULKANINFO_BOTH,
    )
    def test_llvmpipe_filtered_igpu_kept(self, _co: mock.Mock) -> None:
        gpus = hardware._amd_gpus_vulkaninfo()
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["name"], "AMD Radeon 8060S Graphics")
        self.assertEqual(gpus[0]["index"], 0)


class LlamaCliDetectionTests(unittest.TestCase):
    """llama-cli --list-devices reports real GTT/VRAM sizes (vulkaninfo does not)."""

    LLAMA_CLI_OUTPUT = """CPU: AMD Ryzen AI Max+ 395 with Radeon 8060S Graphics (64 threads)
Total size: 64384 MiB
Vulkan0: AMD Radeon 8060S Graphics (RADV STRIX_HALO) (56210 MiB, 50007 MiB free)
"""

    LLAMA_CLI_MULTI = """CPU: Intel Core i9 (16 threads)
Total size: 32768 MiB
Vulkan0: AMD Radeon RX 6800 (RADV NAVI10) (16384 MiB, 16000 MiB free)
Vulkan1: AMD Radeon RX 6800 (RADV NAVI10) (16384 MiB, 16000 MiB free)
"""

    def _run(self, output: str) -> list:
        with mock.patch.object(hardware.Path, "exists", return_value=True), \
             mock.patch.object(hardware.subprocess, "check_output", return_value=output):
            return hardware._amd_gpus_llama_cli()

    def test_parses_single_device(self) -> None:
        gpus = self._run(self.LLAMA_CLI_OUTPUT)
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["vendor"], "amd")
        self.assertEqual(gpus[0]["name"], "AMD Radeon 8060S Graphics (RADV STRIX_HALO)")
        self.assertAlmostEqual(gpus[0]["vram_gb"], 56210 / 1024.0, places=1)

    def test_parses_multiple_devices(self) -> None:
        gpus = self._run(self.LLAMA_CLI_MULTI)
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0]["name"], "AMD Radeon RX 6800 (RADV NAVI10)")
        self.assertEqual(gpus[1]["index"], 1)

    def test_missing_binary_returns_empty(self) -> None:
        with mock.patch.object(hardware.Path, "exists", return_value=False):
            self.assertEqual(hardware._amd_gpus_llama_cli(), [])

    def test_no_vulkan_devices_returns_empty(self) -> None:
        out = "CPU: Intel Core i5 (8 threads)\nTotal size: 16384 MiB\n"
        self.assertEqual(self._run(out), [])


class SystemInfoNvidiaFilterTests(unittest.TestCase):
    """lemond placeholder entries (available=false) must be skipped."""

    def test_unavailable_nvidia_placeholder_skipped(self) -> None:
        info = {
            "devices": {
                "nvidia_gpu": [
                    {"available": False, "name": "", "error": "No NVIDIA discrete GPU found"},
                ],
            },
        }
        self.assertEqual(hardware._gpus_from_system_info(info), [])

    def test_available_nvidia_is_kept(self) -> None:
        info = {
            "devices": {
                "nvidia_gpu": [
                    {"available": True, "index": 0, "name": "RTX 3090",
                     "vram_gb": 24, "driver_version": "560.35"},
                ],
            },
        }
        gpus = hardware._gpus_from_system_info(info)
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["name"], "RTX 3090")
        self.assertTrue(gpus[0]["available"])

    def test_mixed_available_and_unavailable(self) -> None:
        info = {
            "devices": {
                "nvidia_gpu": [
                    {"available": True, "index": 0, "name": "RTX 3090",
                     "vram_gb": 24, "driver_version": "560.35"},
                    {"available": False, "name": "", "error": "gone"},
                ],
            },
        }
        gpus = hardware._gpus_from_system_info(info)
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["name"], "RTX 3090")


class SelectBackendTests(unittest.TestCase):
    def test_nvidia_prefers_cuda(self) -> None:
        hw = {"gpus": [{"vendor": "nvidia", "name": "RTX 3090", "vram_gb": 24}]}
        self.assertEqual(catalog.select_backend(hw), "cuda")

    def test_amd_prefers_vulkan(self) -> None:
        hw = {"gpus": [{"vendor": "amd", "name": "RX 6800", "vram_gb": 0}]}
        self.assertEqual(catalog.select_backend(hw), "vulkan")

    def test_no_gpu_falls_back_to_cpu(self) -> None:
        self.assertEqual(catalog.select_backend({"gpus": []}), "cpu")

    def test_nvidia_wins_over_amd(self) -> None:
        hw = {
            "gpus": [
                {"vendor": "amd", "name": "iGPU", "vram_gb": 0},
                {"vendor": "nvidia", "name": "RTX 1660", "vram_gb": 6},
            ]
        }
        self.assertEqual(catalog.select_backend(hw), "cuda")

    def test_llvmpipe_only_falls_back_to_cpu(self) -> None:
        # After the filter, a box with only llvmpipe has no AMD GPU entry,
        # so select_backend must return "cpu".
        hw = {"gpus": []}
        self.assertEqual(catalog.select_backend(hw), "cpu")

    def test_amd_igpu_selects_vulkan(self) -> None:
        hw = {"gpus": [{"vendor": "amd", "name": "Radeon 8060S", "vram_gb": 0}]}
        self.assertEqual(catalog.select_backend(hw), "vulkan")


class PlanModelTests(unittest.TestCase):
    def _hw(self, gpus: list, backend_state: str | None = None) -> dict:
        info: dict = {}
        if backend_state is not None:
            info["recipes"] = {"llamacpp": {"backends": {
                "cuda": {"state": backend_state},
                "vulkan": {"state": backend_state},
            }}}
        return {
            "gpus": gpus,
            "ram_gb": 32,
            "lemond": {"healthy": True, "system_info": info},
        }

    def test_amd_vulkan_zero_vram_selects_small_model(self) -> None:
        # AMD Vulkan reports vram_gb=0; the planner must still select a model
        # (VRAM fit is delegated to lemond at load time) rather than bailing.
        plan = catalog.plan_model(self._hw([{"vendor": "amd", "name": "RX 6800", "vram_gb": 0}]))
        self.assertEqual(plan["state"], "selected")
        self.assertIsNotNone(plan["selection"])

    def test_no_gpu_not_ready(self) -> None:
        plan = catalog.plan_model(self._hw([]))
        self.assertEqual(plan["state"], "not-ready")
        self.assertIn("No GPU", plan["reason"])

    def test_nvidia_cuda_unsupported_not_ready(self) -> None:
        plan = catalog.plan_model(
            self._hw([{"vendor": "nvidia", "name": "RTX 3090", "vram_gb": 24}], "unsupported")
        )
        self.assertEqual(plan["state"], "not-ready")
        self.assertIn("llamacpp:cuda", plan["reason"])

    def test_nvidia_cuda_ok_selects(self) -> None:
        plan = catalog.plan_model(
            self._hw([{"vendor": "nvidia", "name": "RTX 3090", "vram_gb": 24}], "installed")
        )
        self.assertEqual(plan["state"], "selected")


if __name__ == "__main__":
    unittest.main()
