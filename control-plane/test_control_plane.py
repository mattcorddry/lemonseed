#!/usr/bin/env python3
"""Unit tests for control-plane modules (no root, no network)."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


os.environ.setdefault("APPLIANCE_CP_HOST", "127.0.0.1")
if "APPLIANCE_CP_PORT" not in os.environ:
    os.environ["APPLIANCE_CP_PORT"] = str(_free_port())

import auth  # noqa: E402
import catalog  # noqa: E402
import chat_ready  # noqa: E402
import hardware  # noqa: E402
import model_store  # noqa: E402
import stub  # noqa: E402


def _cuda_hw(vram: float = 6.0, ram: float = 7.65) -> dict:
    return {
        "ram_gb": ram,
        "gpus": [{"index": 0, "name": "GTX 1660 SUPER", "vendor": "nvidia", "vram_gb": vram}],
        "lemond": {
            "healthy": True,
            "system_info": {
                "recipes": {
                    "llamacpp": {
                        "backends": {
                            "cuda": {"state": "installed"},
                            "cpu": {"state": "installed"},
                        }
                    }
                }
            },
        },
    }


class CatalogTests(unittest.TestCase):
    def test_selects_best_gemma_for_prototype_sizing(self) -> None:
        # 6 GiB GPU / 7.65 GiB RAM: Gemma 4 E2B (4 GB VRAM, 6 GB RAM) is the
        # largest Gemma that fits (E4B needs 8 GB VRAM), so it is selected.
        plan = catalog.plan_model(_cuda_hw())
        self.assertEqual(plan["state"], "selected")
        self.assertEqual(plan["family"], "gemma")
        self.assertEqual(plan["selection"]["id"], "Gemma-4-E2B-it-GGUF")
        self.assertEqual(plan["max_vram_gb"], 6.0)

    def test_cpu_only_selects_best_fit(self) -> None:
        # 16 GiB RAM, no GPU: CPU inference requires ram >= min_vram + min_ram.
        # E2B (4+6=10), E4B (8+6=14), and 12B (12+6=18, does not fit) — E4B
        # is the largest Gemma that fits and has the best rank of the rest.
        plan = catalog.plan_model({"ram_gb": 16, "gpus": [], "lemond": {}})
        self.assertEqual(plan["state"], "selected")
        self.assertEqual(plan["family"], "gemma")
        self.assertEqual(plan["selection"]["id"], "Gemma-4-E4B-it-GGUF")
        self.assertIn("CPU inference", plan["reason"])

    def test_cpu_only_not_ready_when_ram_below_floor(self) -> None:
        # 5 GiB RAM, no GPU: even LFM2 1.2B (2+4=6) does not fit.
        plan = catalog.plan_model({"ram_gb": 5, "gpus": [], "lemond": {}})
        self.assertEqual(plan["state"], "not-ready")
        self.assertIn("No catalog model fits", plan["reason"])

    def test_not_ready_when_ram_below_floor(self) -> None:
        # 1 GiB VRAM / 4 GiB RAM: LFM2 1.2B needs 2 GiB VRAM → nothing fits.
        plan = catalog.plan_model(_cuda_hw(vram=1.0, ram=4.0))
        self.assertEqual(plan["state"], "not-ready")
        self.assertIn("No catalog model fits", plan["reason"])

    def test_not_ready_when_cuda_unsupported(self) -> None:
        hw = _cuda_hw()
        hw["lemond"]["system_info"]["recipes"]["llamacpp"]["backends"]["cuda"][
            "state"
        ] = "unsupported"
        plan = catalog.plan_model(hw)
        self.assertEqual(plan["state"], "not-ready")
        self.assertIn("unsupported", plan["reason"])

    def test_custom_catalog_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cat = Path(tmp) / "models.json"
            cat.write_text(
                json.dumps(
                    {
                        "catalog_version": 99,
                        "models": [
                            {
                                "id": "Tiny",
                                "display_name": "Tiny",
                                "min_vram_gb": 1,
                                "min_ram_gb": 2,
                                "rank": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"LEMONSEED_CATALOG": str(cat)}):
                plan = catalog.plan_model(_cuda_hw())
            self.assertEqual(plan["catalog_version"], 99)
            self.assertEqual(plan["selection"]["id"], "Tiny")


class ModelPickerTests(unittest.TestCase):
    """M6.5: family picker, user choice persistence, and planner integration."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.choice_file = Path(self.tmp.name) / "model-choice.json"
        self.patches = [
            mock.patch.object(catalog, "CHOICE_FILE", self.choice_file),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.tmp.cleanup()

    def test_default_family_is_gemma(self) -> None:
        self.assertEqual(catalog.default_family_id(), "gemma")

    def test_families_view_reports_best_fit_and_selection(self) -> None:
        hw = _cuda_hw(vram=12.0, ram=32.0)
        catalog.write_model_choice("qwen")
        families = catalog.families_view(hw)
        by_id = {f["id"]: f for f in families}
        self.assertEqual(len(families), 4)
        self.assertTrue(by_id["gemma"]["default"])
        self.assertEqual(by_id["qwen"]["selected"], True)
        # 12 GiB GPU: Qwen3.5 9B (min 10 GiB) is the best Qwen that fits.
        self.assertEqual(by_id["qwen"]["best_fit"]["id"], "Qwen3.5-9B-GGUF")
        self.assertTrue(by_id["qwen"]["fits"])
        # At 12 GiB VRAM / 32 GiB RAM, gemma, qwen, and lfm2 have a fitting
        # member (LFM2.5 8B A1B needs 8 GiB VRAM); nemotron (20 GiB floor) does
        # not.
        for fid in ("gemma", "qwen", "lfm2"):
            self.assertTrue(by_id[fid]["fits"], fid)
        self.assertEqual(by_id["lfm2"]["best_fit"]["id"], "LFM2.5-8B-A1B")
        self.assertFalse(by_id["nemotron"]["fits"])
        # LFM2.5 1.2B (the CPU-friendly dense model) is in the family.
        lfm_ids = {m["id"] for m in by_id["lfm2"]["models"]}
        self.assertIn("LFM2.5-1.2B-Instruct-GGUF", lfm_ids)
        self.assertNotIn("LFM2-24B-A2B-GGUF", lfm_ids)

    def test_families_view_marks_unfit_family(self) -> None:
        hw = _cuda_hw(vram=4.0, ram=8.0)
        families = catalog.families_view(hw)
        by_id = {f["id"]: f for f in families}
        # nemotron (20 GiB floor) cannot fit 4 GiB.
        self.assertFalse(by_id["nemotron"]["fits"])
        self.assertIsNone(by_id["nemotron"]["best_fit"])
        self.assertTrue(by_id["gemma"]["fits"])

    def test_plan_honors_explicit_family_choice(self) -> None:
        hw = _cuda_hw(vram=12.0, ram=32.0)
        catalog.write_model_choice("qwen")
        plan = catalog.plan_model(hw)
        self.assertEqual(plan["state"], "selected")
        self.assertEqual(plan["family"], "qwen")
        self.assertEqual(plan["selection"]["id"], "Qwen3.5-9B-GGUF")

    def test_plan_auto_picks_default_family(self) -> None:
        hw = _cuda_hw(vram=48.0, ram=64.0)
        plan = catalog.plan_model(hw)
        self.assertEqual(plan["state"], "selected")
        self.assertEqual(plan["family"], "gemma")
        self.assertEqual(plan["selection"]["id"], "Gemma-4-31B-it-GGUF")

    def test_plan_falls_back_when_family_does_not_fit(self) -> None:
        # nemotron (20 GiB floor) cannot fit a 4 GiB GPU; the planner must
        # fall back to the best overall catalog model that does.
        hw = _cuda_hw(vram=4.0, ram=8.0)
        catalog.write_model_choice("nemotron")
        plan = catalog.plan_model(hw)
        self.assertEqual(plan["state"], "selected")
        self.assertNotEqual(plan["family"], "nemotron")
        self.assertIn("Falling back", plan["reason"])

    def test_plan_not_ready_when_nothing_fits(self) -> None:
        # 1 GiB VRAM / 4 GiB RAM: LFM2.5 1.2B needs 2 GiB VRAM → nothing fits.
        hw = _cuda_hw(vram=1.0, ram=4.0)
        plan = catalog.plan_model(hw)
        self.assertEqual(plan["state"], "not-ready")
        self.assertIn("No catalog model fits", plan["reason"])

    def test_choice_roundtrip_and_clear(self) -> None:
        saved = catalog.write_model_choice("gemma")
        self.assertEqual(saved["family"], "gemma")
        self.assertEqual(catalog.read_model_choice()["family"], "gemma")
        catalog.write_model_choice("auto")
        self.assertEqual(catalog.read_model_choice()["family"], "auto")

    def test_read_choice_missing_file(self) -> None:
        self.assertEqual(catalog.read_model_choice(), {"family": "auto", "model_id": None})

    def test_choice_exists_distinguishes_absent_from_auto(self) -> None:
        # Absent file = "no choice yet" (first boot) — distinct from an
        # explicit Automatic pick, which writes the file.
        self.assertFalse(catalog.choice_exists())
        catalog.write_model_choice("auto")
        self.assertTrue(catalog.choice_exists())
        catalog.write_model_choice("qwen")
        self.assertTrue(catalog.choice_exists())

    def test_family_label_uses_maker_and_name(self) -> None:
        self.assertEqual(catalog.family_label("gemma"), "Google Gemma")
        self.assertEqual(catalog.family_label("qwen"), "Alibaba Qwen")

    def test_family_label_unknown_family_is_none(self) -> None:
        self.assertIsNone(catalog.family_label("no-such-family"))


class HardwareTests(unittest.TestCase):
    def setUp(self) -> None:
        # The GPU cache is process-global; reset it so tests don't leak state.
        hardware._GPU_CACHE = []
        hardware._GPU_COLLECTED = False

    def tearDown(self) -> None:
        hardware._GPU_CACHE = []
        hardware._GPU_COLLECTED = False

    def test_max_gpu_vram_uses_largest_not_sum(self) -> None:
        report = {
            "gpus": [
                {"index": 0, "vram_gb": 6.0},
                {"index": 1, "vram_gb": 12.0},
            ]
        }
        self.assertEqual(hardware.max_gpu_vram_gb(report), 12.0)

    def test_largest_gpu_index(self) -> None:
        report = {
            "gpus": [
                {"index": 0, "vram_gb": 6.0},
                {"index": 1, "vram_gb": 12.0},
            ]
        }
        self.assertEqual(hardware.largest_gpu_index(report), 1)

    def test_gpus_from_system_info(self) -> None:
        info = {
            "devices": {
                "nvidia_gpu": [
                    {
                        "index": 0,
                        "name": "GPU A",
                        "vram_gb": 6,
                        "driver_version": "595.71",
                    }
                ]
            }
        }
        gpus = hardware._gpus_from_system_info(info)
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["vram_gb"], 6.0)

    @mock.patch("hardware.subprocess.check_output")
    def test_nvidia_smi_fallback(self, check_output: mock.Mock) -> None:
        check_output.return_value = "0, GTX 1660 SUPER, 6144, 595.71\n"
        gpus = hardware._nvidia_gpus_smi()
        self.assertEqual(len(gpus), 1)
        self.assertAlmostEqual(gpus[0]["vram_gb"], 6.0, places=1)

    @mock.patch("hardware._lemond_get")
    @mock.patch("hardware._nvidia_gpus_smi")
    @mock.patch("hardware._mem_total_gb", return_value=8.0)
    @mock.patch("hardware._cpu_info", return_value={"name": "test", "cores": 4, "threads": 4})
    def test_collect_hardware_proxies_system_info(
        self,
        _cpu: mock.Mock,
        _mem: mock.Mock,
        smi: mock.Mock,
        lemond_get: mock.Mock,
    ) -> None:
        smi.return_value = []
        lemond_get.return_value = {
            "devices": {"cpu": {"available": True, "name": "Xeon", "cores": 8, "threads": 16}},
            "recipes": {"llamacpp": {"backends": {"cuda": {"state": "installed"}}}},
        }
        report = hardware.collect_hardware()
        self.assertTrue(report["lemond"]["healthy"])
        self.assertIn("system_info", report["lemond"])
        self.assertNotIn("llamacpp_cuda", report["lemond"])
        self.assertEqual(report["cpu"]["name"], "Xeon")

    @mock.patch("hardware._mem_total_gb", return_value=8.0)
    @mock.patch("hardware._cpu_info", return_value={"name": "test", "cores": 4, "threads": 4})
    @mock.patch("hardware._amd_gpus_vulkaninfo", return_value=[])
    @mock.patch("hardware._amd_gpus_llama_cli", return_value=[])
    @mock.patch(
        "hardware._nvidia_gpus_smi",
        return_value=[{"index": 0, "name": "Test GPU", "vram_gb": 16.0, "driver_version": "1", "vendor": "nvidia"}],
    )
    @mock.patch("hardware._lemond_get", return_value={})
    def test_gpu_probe_runs_once_per_boot(
        self,
        lemond_get: mock.Mock,
        smi: mock.Mock,
        llama_cli: mock.Mock,
        vulkaninfo: mock.Mock,
        _cpu: mock.Mock,
        _mem: mock.Mock,
    ) -> None:
        hardware.collect_hardware()
        hardware.collect_hardware()
        # External tools are expensive and boot-static: probe once, cache the rest.
        # (A non-empty result is cached; an empty result is re-probed to handle
        # the first-boot race where the GPU driver is not ready yet.)
        self.assertEqual(smi.call_count, 1)
        self.assertEqual(llama_cli.call_count, 1)
        self.assertEqual(vulkaninfo.call_count, 1)

    @mock.patch("hardware._mem_total_gb", return_value=8.0)
    @mock.patch("hardware._cpu_info", return_value={"name": "test", "cores": 4, "threads": 4})
    @mock.patch("hardware._amd_gpus_vulkaninfo", return_value=[])
    @mock.patch("hardware._amd_gpus_llama_cli", return_value=[])
    @mock.patch("hardware._nvidia_gpus_smi", return_value=[])
    @mock.patch("hardware._lemond_get", return_value={})
    def test_gpu_probe_retries_when_empty(
        self,
        lemond_get: mock.Mock,
        smi: mock.Mock,
        llama_cli: mock.Mock,
        vulkaninfo: mock.Mock,
        _cpu: mock.Mock,
        _mem: mock.Mock,
    ) -> None:
        """An empty GPU result is NOT cached — the driver may not be ready at
        first boot. Subsequent calls re-probe until a GPU is found."""
        hardware.collect_hardware()
        hardware.collect_hardware()
        # Both calls probed (empty result is not cached).
        self.assertEqual(smi.call_count, 2)

    @mock.patch("hardware._mem_total_gb", return_value=8.0)
    @mock.patch("hardware._cpu_info", return_value={"name": "test", "cores": 4, "threads": 4})
    @mock.patch("hardware._amd_gpus_vulkaninfo", return_value=[])
    @mock.patch("hardware._amd_gpus_llama_cli", return_value=[])
    @mock.patch("hardware._nvidia_gpus_smi", return_value=[])
    @mock.patch("hardware._lemond_get", return_value={})
    def test_refresh_gpus_forces_reprobe(
        self,
        lemond_get: mock.Mock,
        smi: mock.Mock,
        llama_cli: mock.Mock,
        vulkaninfo: mock.Mock,
        _cpu: mock.Mock,
        _mem: mock.Mock,
    ) -> None:
        hardware.collect_hardware()
        self.assertEqual(smi.call_count, 1)
        hardware.refresh_gpus()
        self.assertEqual(smi.call_count, 2)


class ChatReadyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.chat_file = Path(self.tmp.name) / "chat-ready.json"
        self.setup_file = Path(self.tmp.name) / "setup-complete"
        self.setup_file.write_text("done\n", encoding="utf-8")
        # The model cache must point at a real, writable temp dir so the
        # pre-download disk-space guard (model_store) can read disk_usage.
        self.models_dir = Path(self.tmp.name) / "models"
        self.models_dir.mkdir()
        self.patches = [
            mock.patch.object(chat_ready, "CHAT_READY_FILE", self.chat_file),
            mock.patch.object(chat_ready, "SETUP_COMPLETE_FILE", self.setup_file),
            mock.patch.object(chat_ready, "MODELS_DIR", self.models_dir),
            # No unit test may reach lemond over HTTP. Every lemond call in
            # chat_ready bails out when there is no API key, which also makes
            # the cache check answer "not on disk" so existing tests keep
            # exercising the download path. Cache-path tests patch
            # `_model_is_downloaded` directly to say otherwise.
            mock.patch.object(chat_ready, "lemonade_api_key", return_value=""),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        chat_ready._worker = None
        if chat_ready._prepare_lock.locked():
            chat_ready._prepare_lock.release()
        self.tmp.cleanup()

    def test_loaded_model_names(self) -> None:
        health = {
            "model_loaded": "Gemma-4-E2B-it-GGUF",
            "all_models_loaded": [{"model_name": "Other"}],
        }
        names = chat_ready._loaded_model_names(health)
        self.assertEqual(names, {"Gemma-4-E2B-it-GGUF", "Other"})

    @mock.patch.object(catalog, "choice_exists", return_value=True)
    @mock.patch.object(chat_ready, "_verify_completion", return_value=(True, "ok"))
    @mock.patch.object(chat_ready, "_model_is_loaded", return_value=True)
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_skips_when_model_already_loaded(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        _loaded: mock.Mock,
        _verify: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        collect.return_value = _cuda_hw()
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {
                "id": "Gemma-4-E2B-it-GGUF",
                "display_name": "Gemma 4 E2B",
            },
        }
        result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["model_id"], "Gemma-4-E2B-it-GGUF")
        self.assertEqual(result["display_name"], "Gemma 4 E2B")

    @mock.patch.object(catalog, "choice_exists", return_value=True)
    @mock.patch.object(chat_ready, "_verify_completion", return_value=(True, "ok"))
    @mock.patch.object(chat_ready, "_model_is_loaded")
    @mock.patch.object(chat_ready, "_run_lemonade_pull")
    @mock.patch.object(chat_ready, "_run_lemonade")
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_pulls_and_loads_with_pin(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        run: mock.Mock,
        _pull: mock.Mock,
        loaded: mock.Mock,
        _verify: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        collect.return_value = _cuda_hw()
        model_id = "Gemma-4-E2B-it-GGUF"
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {"id": model_id, "display_name": "Gemma 4 E2B"},
        }
        _pull.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        run_calls: list[list[str]] = []

        def _run(args: list[str], timeout: int = 0) -> mock.Mock:
            run_calls.append(list(args))
            return mock.Mock(returncode=0, stdout="", stderr="")

        def _loaded(name: str) -> bool:
            return any(c[:2] == ["load", name] for c in run_calls)

        run.side_effect = _run
        loaded.side_effect = _loaded

        result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "ready", result.get("error"))
        self.assertTrue(any(c[:2] == ["load", model_id] for c in run_calls))

    @mock.patch.object(catalog, "choice_exists", return_value=True)
    @mock.patch.object(chat_ready, "_model_is_downloaded", return_value=True)
    @mock.patch.object(chat_ready, "_verify_completion", return_value=(True, "ok"))
    @mock.patch.object(chat_ready, "_model_is_loaded")
    @mock.patch.object(chat_ready, "_run_lemonade_pull")
    @mock.patch.object(chat_ready, "_run_lemonade")
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_skips_pull_when_weights_are_cached(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        run: mock.Mock,
        pull: mock.Mock,
        loaded: mock.Mock,
        _verify: mock.Mock,
        on_disk: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        """A reboot must not announce (or perform) a download for cached weights.

        `lemonade pull` on a cached model moves no bytes but re-hashes the whole
        file, which cost ~90 s and made the portal claim a download was running.
        """
        collect.return_value = _cuda_hw()
        model_id = "Gemma-4-E2B-it-GGUF"
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {"id": model_id, "display_name": "Gemma 4 E2B"},
        }
        run_calls: list[list[str]] = []

        def _run(args: list[str], timeout: int = 0) -> mock.Mock:
            run_calls.append(list(args))
            return mock.Mock(returncode=0, stdout="", stderr="")

        def _loaded(name: str) -> bool:
            return any(c[:2] == ["load", name] for c in run_calls)

        run.side_effect = _run
        loaded.side_effect = _loaded

        seen: list[dict] = []
        original = chat_ready.write_chat_ready

        def _capture(payload: dict) -> None:
            seen.append(dict(payload))
            return original(payload)

        with mock.patch.object(chat_ready, "write_chat_ready", side_effect=_capture):
            result = chat_ready.prepare_local_chat()

        self.assertEqual(result["state"], "ready", result.get("error"))
        pull.assert_not_called()
        self.assertTrue(any(c[:2] == ["load", model_id] for c in run_calls))
        # The portal must never be told a download is running for cached weights.
        messages = [str(payload.get("message") or "") for payload in seen]
        self.assertFalse(
            [m for m in messages if m.startswith("Downloading")],
            f"download announced for a cached model: {messages}",
        )
        workflows = {str(payload.get("workflow") or "") for payload in seen}
        self.assertIn("Preparing the Large Language Model", workflows)
        self.assertTrue(
            any("already on disk" in m for m in messages),
            f"cached-model step message missing: {messages}",
        )

    @mock.patch.object(catalog, "choice_exists", return_value=True)
    @mock.patch.object(chat_ready, "_model_is_downloaded", return_value=True)
    @mock.patch.object(chat_ready, "_verify_completion", return_value=(True, "ok"))
    @mock.patch.object(chat_ready, "_model_is_loaded")
    @mock.patch.object(chat_ready, "_run_lemonade_pull")
    @mock.patch.object(chat_ready, "_run_lemonade")
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_repairs_cached_model_that_will_not_load(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        run: mock.Mock,
        pull: mock.Mock,
        loaded: mock.Mock,
        _verify: mock.Mock,
        _on_disk: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        """If the cached copy is unusable, one repair pull must recover it."""
        collect.return_value = _cuda_hw()
        model_id = "Gemma-4-E2B-it-GGUF"
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {"id": model_id, "display_name": "Gemma 4 E2B"},
        }
        repaired = {"done": False}
        run_calls: list[list[str]] = []

        def _run(args: list[str], timeout: int = 0) -> mock.Mock:
            run_calls.append(list(args))
            if args[:2] == ["load", model_id] and not repaired["done"]:
                return mock.Mock(returncode=1, stdout="", stderr="truncated snapshot")
            return mock.Mock(returncode=0, stdout="", stderr="")

        def _pull_repair(*args: object, **kwargs: object) -> mock.Mock:
            repaired["done"] = True
            return mock.Mock(returncode=0, stdout="", stderr="")

        def _loaded(name: str) -> bool:
            return repaired["done"] and name == model_id

        run.side_effect = _run
        pull.side_effect = _pull_repair
        loaded.side_effect = _loaded

        result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "ready", result.get("error"))
        self.assertEqual(pull.call_count, 1)
        loads = [c for c in run_calls if c[:2] == ["load", model_id]]
        self.assertEqual(len(loads), 2, "expected one failed load then one retry")

    @mock.patch.object(catalog, "choice_exists", return_value=True)
    @mock.patch.object(chat_ready, "_verify_completion", return_value=(True, "ok"))
    @mock.patch.object(chat_ready, "_model_is_loaded")
    @mock.patch.object(chat_ready, "_run_lemonade_pull")
    @mock.patch.object(chat_ready, "_run_lemonade")
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_pulls_when_weights_are_not_cached(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        run: mock.Mock,
        pull: mock.Mock,
        loaded: mock.Mock,
        _verify: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        """setUp pins `_model_is_downloaded` to False, so this must download."""
        collect.return_value = _cuda_hw()
        model_id = "Gemma-4-E2B-it-GGUF"
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {"id": model_id, "display_name": "Gemma 4 E2B"},
        }
        pull.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        run_calls: list[list[str]] = []

        def _run(args: list[str], timeout: int = 0) -> mock.Mock:
            run_calls.append(list(args))
            return mock.Mock(returncode=0, stdout="", stderr="")

        def _loaded(name: str) -> bool:
            return any(c[:2] == ["load", name] for c in run_calls)

        run.side_effect = _run
        loaded.side_effect = _loaded

        result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "ready", result.get("error"))
        pull.assert_called_once_with(model_id)

    @mock.patch.object(chat_ready, "lemonade_api_key", return_value="test-key")
    def test_model_is_downloaded(self, _key: mock.Mock) -> None:
        """`downloaded` from GET /v1/models/{id} drives the boot fast path."""

        def _urlopen_for(payload: bytes):
            def _open(request: object, timeout: float = 0.0) -> mock.MagicMock:
                cm = mock.MagicMock()
                cm.__enter__.return_value.read.return_value = payload
                return cm

            return _open

        with mock.patch.object(
            chat_ready,
            "urlopen",
            side_effect=_urlopen_for(b'{"id": "Gemma", "downloaded": true}'),
        ):
            self.assertTrue(chat_ready._model_is_downloaded("Gemma"))
        with mock.patch.object(
            chat_ready,
            "urlopen",
            side_effect=_urlopen_for(b'{"id": "Gemma", "downloaded": false}'),
        ):
            self.assertFalse(chat_ready._model_is_downloaded("Gemma"))
        with mock.patch.object(
            chat_ready, "urlopen", side_effect=URLError("down")
        ):
            self.assertFalse(chat_ready._model_is_downloaded("Gemma"))
        with mock.patch.object(chat_ready, "lemonade_api_key", return_value=""):
            self.assertFalse(chat_ready._model_is_downloaded("Gemma"))

    @mock.patch.object(catalog, "choice_exists", return_value=True)
    @mock.patch.object(chat_ready, "_verify_completion", return_value=(True, "ok"))
    @mock.patch.object(chat_ready, "_model_is_loaded")
    @mock.patch.object(chat_ready, "_loaded_model_names")
    @mock.patch.object(chat_ready, "_run_lemonade_pull")
    @mock.patch.object(chat_ready, "_run_lemonade")
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_unloads_other_models_before_load(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        run: mock.Mock,
        _pull: mock.Mock,
        names: mock.Mock,
        loaded: mock.Mock,
        _verify: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        """A model switch must evict the previously loaded model first."""
        collect.return_value = _cuda_hw()
        new_id = "Qwen3.6-27B-GGUF"
        old_id = "Nemotron-3-Nano-30B-A3B-GGUF"
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {"id": new_id, "display_name": "Qwen3.6 27B"},
        }
        _pull.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        run_calls: list[list[str]] = []

        def _run(args: list[str], timeout: int = 0) -> mock.Mock:
            run_calls.append(list(args))
            return mock.Mock(returncode=0, stdout="", stderr="")

        def _names(health: dict | None = None) -> set[str]:
            # The old model is resident until it is unloaded; the new one
            # only becomes loaded after the load call.
            if any(c[:2] == ["unload", old_id] for c in run_calls):
                return {new_id} if any(c[:2] == ["load", new_id] for c in run_calls) else set()
            return {old_id}

        def _loaded(name: str) -> bool:
            return name in _names()

        run.side_effect = _run
        names.side_effect = _names
        loaded.side_effect = _loaded

        result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "ready", result.get("error"))
        self.assertTrue(any(c[:2] == ["unload", old_id] for c in run_calls))
        unload_idx = next(i for i, c in enumerate(run_calls) if c[:2] == ["unload", old_id])
        load_idx = next(i for i, c in enumerate(run_calls) if c[:2] == ["load", new_id])
        self.assertLess(unload_idx, load_idx)

    @mock.patch.object(catalog, "choice_exists", return_value=True)
    @mock.patch.object(chat_ready, "_verify_completion", return_value=(True, "ok"))
    @mock.patch.object(chat_ready, "_model_is_loaded")
    @mock.patch.object(chat_ready, "_run_lemonade_pull")
    @mock.patch.object(chat_ready, "_run_lemonade")
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_download_message_is_friendly(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        run: mock.Mock,
        _pull: mock.Mock,
        loaded: mock.Mock,
        _verify: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        collect.return_value = _cuda_hw()
        model_id = "Gemma-4-E2B-it-GGUF"
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "family": "gemma",
            "selection": {
                "id": model_id,
                "display_name": "Gemma 4 E2B",
                "weights_gb": 4.1,
            },
        }
        _pull.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        run_calls: list[list[str]] = []

        def _run(args: list[str], timeout: int = 0) -> mock.Mock:
            run_calls.append(list(args))
            return mock.Mock(returncode=0, stdout="", stderr="")

        def _loaded(name: str) -> bool:
            return any(c[:2] == ["load", name] for c in run_calls)

        run.side_effect = _run
        loaded.side_effect = _loaded
        self._download_message = self._run_and_capture_download_message()

        # The download message must use the friendly family label
        # (maker + name), not the raw catalog id.
        self.assertIn("Google Gemma", self._download_message)
        self.assertNotIn(model_id, self._download_message)
        # Size is the only progress hint we can honestly give: lemond exposes no
        # download-progress API, so there is no rate or ETA to show.
        self.assertIn("~4.1 GB", self._download_message)
        self.assertNotIn("min)", self._download_message)

    @mock.patch.object(catalog, "choice_exists", return_value=True)
    @mock.patch.object(chat_ready, "_verify_completion", return_value=(False, "empty completion response"))
    @mock.patch.object(chat_ready, "_model_is_loaded", return_value=True)
    @mock.patch.object(chat_ready, "_run_lemonade_pull")
    @mock.patch.object(chat_ready, "_run_lemonade")
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_gives_up_after_repeated_verify_failures(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        _run: mock.Mock,
        _pull: mock.Mock,
        _loaded: mock.Mock,
        _verify: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        """A model that loads but crashes (backend crash loop) must stop being
        retried after a few failures instead of looping forever."""
        collect.return_value = _cuda_hw()
        model_id = "Nemotron-3-Nano-30B-A3B-GGUF"
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {"id": model_id, "display_name": "Nemotron 3 Nano"},
        }
        _pull.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        _run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        states: list[str] = []
        for _ in range(chat_ready._MAX_VERIFY_FAILURES + 1):
            result = chat_ready.prepare_local_chat(force=True)
            states.append(result["state"])
        # The last attempt must be a terminal error, not another retry.
        self.assertEqual(states[-1], "error")
        self.assertIn("keeps failing", states[-1] and result["task"])
        # Earlier attempts were transient errors too (still recoverable).
        self.assertEqual(set(states), {"error"})
        self.assertIn("keeps failing to load", result["error"])

    @mock.patch.object(catalog, "choice_exists", return_value=True)
    @mock.patch.object(chat_ready, "_model_is_loaded", return_value=False)
    @mock.patch.object(chat_ready, "_run_lemonade_pull")
    @mock.patch.object(chat_ready, "_run_lemonade")
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_give_up_gate_stops_reloading_failed_model(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        _run: mock.Mock,
        _pull: mock.Mock,
        _loaded: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        """After a model fails to load N times, subsequent prepare calls (force=False)
        must return the terminal error immediately without re-issuing lemonade load.
        This is the fix for the lemond crash loop: the CP stops re-queueing a model
        that deterministically fails (e.g. unknown architecture)."""
        collect.return_value = _cuda_hw()
        model_id = "Qwen3.6-27B-GGUF"
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {"id": model_id, "display_name": "Qwen3.6 27B"},
        }
        _pull.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        # lemonade load fails (e.g. unknown model architecture); pull succeeds.
        def _run_side_effect(args: list[str], timeout: int = 0) -> mock.Mock:
            if args[0] == "load":
                return mock.Mock(returncode=1, stdout="", stderr="unknown model architecture: 'eagle3'")
            return mock.Mock(returncode=0, stdout="", stderr="")

        _run.side_effect = _run_side_effect
        # Exhaust the failure cap (force=True bypasses the gate so we can build up the count).
        for _ in range(chat_ready._MAX_VERIFY_FAILURES):
            chat_ready.prepare_local_chat(force=True)
        # Now a non-force call (what the worker does on each poll) must NOT re-issue load.
        _run.reset_mock()
        result = chat_ready.prepare_local_chat(force=False)
        self.assertEqual(result["state"], "error")
        # The gate should short-circuit with a "not retrying" style message.
        self.assertIn("failed to load", result["task"])
        # No new lemonade load was issued — the gate short-circuited.
        load_calls = [c for c in _run.call_args_list if c[0][0] and c[0][0][0] == "load"]
        self.assertEqual(len(load_calls), 0, "give-up gate should prevent re-loading")
        # Clearing the gate (new user choice) + resetting the failure counter
        # allows a fresh retry cycle.
        chat_ready.clear_gave_up()
        with chat_ready._verify_failures_lock:
            chat_ready._verify_failures.clear()
        _run.reset_mock()
        result2 = chat_ready.prepare_local_chat(force=True)
        self.assertEqual(result2["state"], "error")
        load_calls2 = [c for c in _run.call_args_list if c[0][0] and c[0][0][0] == "load"]
        self.assertGreaterEqual(len(load_calls2), 1, "after clear_gave_up, load is re-attempted")

    def _run_and_capture_download_message(self) -> str:
        """Run prepare_local_chat, returning the persisted download message."""
        seen: list[str] = []
        original = chat_ready.write_chat_ready

        def _capture(payload: dict) -> dict:
            message = str(payload.get("message") or "")
            if message.startswith("Downloading"):
                seen.append(message)
            return original(payload)

        with mock.patch.object(chat_ready, "write_chat_ready", side_effect=_capture):
            result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "ready", result.get("error"))
        self.assertTrue(seen, "no download message was persisted")
        return seen[0]

    @mock.patch.object(chat_ready, "_model_is_loaded", return_value=False)
    @mock.patch.object(chat_ready, "ensure_chat_worker")
    def test_chat_snapshot_reconciles_stale_ready(
        self,
        ensure_worker: mock.Mock,
        _loaded: mock.Mock,
    ) -> None:
        chat_ready.write_chat_ready(
            {
                "state": "ready",
                "progress": 100,
                "model_id": "Gemma-4-E2B-it-GGUF",
                "display_name": "Gemma 4 E2B",
            }
        )
        snap = chat_ready.chat_snapshot()
        self.assertEqual(snap["state"], "preparing")
        self.assertIn("Reloading", snap["message"])
        ensure_worker.assert_called_once()

    def test_prepare_non_blocking_when_lock_held(self) -> None:
        chat_ready.write_chat_ready({"state": "preparing", "progress": 50, "message": "busy"})
        self.assertTrue(chat_ready._prepare_lock.acquire(blocking=False))
        try:
            start = time.monotonic()
            result = chat_ready.prepare_local_chat()
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 0.5)
            self.assertEqual(result["state"], "preparing")
        finally:
            chat_ready._prepare_lock.release()

    @mock.patch.object(catalog, "choice_exists", return_value=False)
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_waits_for_choice_when_none_made(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        _plan: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        # No choice file yet: the box must NOT pull a model — it should show
        # the picker (awaiting-choice) instead of auto-downloading the default.
        collect.return_value = _cuda_hw()
        result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "awaiting-choice")
        self.assertEqual(result["workflow"], "Choose a model")
        self.assertIsNone(result["model_id"])

    @mock.patch.object(chat_ready, "_verify_completion", return_value=(True, "ok"))
    @mock.patch.object(chat_ready, "_model_is_loaded")
    @mock.patch.object(chat_ready, "_run_lemonade_pull")
    @mock.patch.object(chat_ready, "_run_lemonade")
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_proceeds_when_choice_made(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        run: mock.Mock,
        _pull: mock.Mock,
        loaded: mock.Mock,
        _verify: mock.Mock,
    ) -> None:
        # An explicit choice (even "auto") unlocks the pull/load path.
        collect.return_value = _cuda_hw()
        model_id = "Gemma-4-E2B-it-GGUF"
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {"id": model_id, "display_name": "Gemma 4 E2B"},
        }
        _pull.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        run_calls: list[list[str]] = []

        def _run(args: list[str], timeout: int = 0) -> mock.Mock:
            run_calls.append(list(args))
            return mock.Mock(returncode=0, stdout="", stderr="")

        def _loaded(name: str) -> bool:
            return any(c[:2] == ["load", name] for c in run_calls)

        run.side_effect = _run
        loaded.side_effect = _loaded
        with mock.patch.object(catalog, "choice_exists", return_value=True):
            result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "ready", result.get("error"))
        self.assertTrue(any(c[:2] == ["load", model_id] for c in run_calls))

    def test_chat_snapshot_shows_rebooting_when_marker_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "reboot-pending.json"
            chat_ready.write_chat_ready({"state": "ready", "progress": 100, "model_id": "m"})
            with mock.patch.object(chat_ready, "REBOOT_PENDING_FILE", pending):
                pending.write_text("{}\n", encoding="utf-8")
                snap = chat_ready.chat_snapshot()
                self.assertEqual(snap["state"], "rebooting")
                self.assertEqual(snap["workflow"], "Rebooting")
                self.assertIn("rebooting", snap["task"].lower())
                chat_ready.clear_reboot_pending()
                self.assertFalse(pending.exists())

    def test_chat_snapshot_ignores_missing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "reboot-pending.json"
            chat_ready.write_chat_ready({"state": "ready", "progress": 100, "model_id": "m"})
            with mock.patch.object(chat_ready, "REBOOT_PENDING_FILE", pending):
                chat_ready.clear_reboot_pending()
                snap = chat_ready.chat_snapshot()
                self.assertNotEqual(snap["state"], "rebooting")

    def test_ensure_chat_worker_returns_immediately(self) -> None:
        """The worker must be backgrounded *and* must not outlive the test.

        ``ensure_chat_worker`` sleeps 2s before calling ``prepare_local_chat``,
        which is longer than this test runs. A decorator/``with`` patch is
        therefore gone by the time the thread calls in, and the daemon thread
        would run the **real** ``prepare_local_chat`` mid-suite: writing to real
        paths and stealing ``_prepare_lock`` so unrelated ChatReady tests got
        the idle snapshot (the suite's old intermittent ``'idle' != 'ready'``).
        Drain the thread before the patch is stopped.
        """
        started = threading.Event()
        release = threading.Event()
        spawned: list = []

        def _fake_prepare(**_kwargs) -> None:
            started.set()
            release.wait(10)

        def _drain() -> None:
            started.wait(10)
            release.set()
            # Join the captured handle: tearDown clears chat_ready._worker
            # before cleanups run.
            for thread in spawned:
                thread.join(timeout=5)

        patch = mock.patch.object(chat_ready, "prepare_local_chat", side_effect=_fake_prepare)
        patch.start()
        # addCleanup is LIFO, so register stop first and drain second: the
        # worker must finish while the patch is still installed.
        self.addCleanup(patch.stop)
        self.addCleanup(_drain)

        start = time.monotonic()
        chat_ready.ensure_chat_worker()
        elapsed = time.monotonic() - start
        if chat_ready._worker is not None:
            spawned.append(chat_ready._worker)
        self.assertLess(elapsed, 0.5)
        self.assertIsNotNone(chat_ready._worker)
        self.assertTrue(chat_ready._worker.is_alive())

    def test_cancel_active_pull_noop_when_no_pull(self) -> None:
        """cancel_active_pull must be safe to call when no pull is in flight."""
        chat_ready.cancel_active_pull()  # should not raise

    def test_cancel_active_pull_kills_process(self) -> None:
        """cancel_active_pull must terminate the tracked Popen and set the flag."""
        proc = subprocess.Popen(["sleep", "60"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with chat_ready._pull_lock:
                chat_ready._pull_process = proc
            chat_ready.cancel_active_pull()
            # The process should be dead (SIGTERM → exit).
            self.assertIsNotNone(proc.poll())
            self.assertTrue(chat_ready._pull_cancelled.is_set())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            chat_ready._pull_cancelled.clear()

    def test_was_pull_cancelled_reflects_flag(self) -> None:
        chat_ready._pull_cancelled.clear()
        self.assertFalse(chat_ready._was_pull_cancelled())
        chat_ready._pull_cancelled.set()
        self.assertTrue(chat_ready._was_pull_cancelled())
        chat_ready._pull_cancelled.clear()

    @mock.patch.object(catalog, "choice_exists", return_value=True)
    @mock.patch.object(chat_ready, "_verify_completion", return_value=(True, "ok"))
    @mock.patch.object(chat_ready, "_model_is_loaded")
    @mock.patch.object(chat_ready, "_run_lemonade_pull")
    @mock.patch.object(chat_ready, "_run_lemonade")
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_prepare_skips_load_when_choice_changed_during_pull(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        run: mock.Mock,
        pull: mock.Mock,
        loaded: mock.Mock,
        _verify: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        """If the user switches model while the pull is in flight and the pull
        finishes before we can kill it, the worker must skip the load."""
        collect.return_value = _cuda_hw()
        old_id = "Gemma-4-E2B-it-GGUF"
        new_id = "Qwen3.5-4B-GGUF"
        # First plan call (at the top of prepare_local_chat) returns the old model.
        # _choice_changed re-plans and gets the new model → mismatch → skip load.
        plan.side_effect = [
            {"state": "selected", "reason": "ok", "selection": {"id": old_id, "display_name": "Gemma 4 E2B"}},
            {"state": "selected", "reason": "ok", "selection": {"id": new_id, "display_name": "Qwen 3.5 4B"}},
        ]
        pull.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        loaded.return_value = False

        result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "error")
        self.assertIn("choice changed", result["error"])
        # The load command must NOT have been issued.
        for call in run.call_args_list:
            args = call[0][0]
            self.assertNotEqual(args[0], "load", "load must be skipped when choice changes")


class _FakeLemondClient:
    """Stand-in for lemond_client.LemonadeClient in unit tests.

    Mirrors the four methods model_store uses: system_info(), model_files(),
    loaded_model_names(), delete_model(). lemond's API is keyed by the
    **catalog id** (not the on-disk dir name), so sizes/loaded are given by
    catalog id and ``dir_to_id`` maps each on-disk directory to its catalog id
    (models without an entry fall back to the filesystem, as in production).
    """

    def __init__(
        self,
        *,
        total: int,
        free: int,
        sizes: Optional[dict[str, int]] = None,
        loaded: Optional[set[str]] = None,
        delete_fail: Optional[set[str]] = None,
        api_down: bool = False,
    ) -> None:
        self.total = total
        self.free = free
        self._sizes = sizes or {}
        self._loaded = loaded or set()
        self._delete_fail = delete_fail or set()
        self.api_down = api_down
        self.deleted: list[str] = []
        self.models_dir: Optional[Path] = None

    def system_info(self) -> Optional[dict]:
        if self.api_down:
            return None
        return {"model_storage": {"total_bytes": self.total, "free_bytes": self.free}}

    def model_files(self, model_id: str) -> Optional[list]:
        if self.api_down:
            return None
        if model_id in self._sizes:
            return [{"name": "weights.gguf", "size_bytes": self._sizes[model_id]}]
        return None  # fall back to the filesystem walk

    def loaded_model_names(self) -> set:
        if self.api_down:
            return set()
        return set(self._loaded)

    def delete_model(self, model_id: str) -> bool:
        if self.api_down or model_id in self._delete_fail:
            return False
        self.deleted.append(model_id)
        return True


class ModelStoreTests(unittest.TestCase):
    """Disk-space maintenance for the model cache (LRU eviction + reserve)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.models_dir = Path(self.tmp.name) / "models"
        self.models_dir.mkdir()
        self.total = 100 * 1024 ** 3  # 100 GiB filesystem
        self.client = None  # type: ignore[assignment]
        self._prev_catalog = model_store._catalog_override

    def tearDown(self) -> None:
        model_store._catalog_override = self._prev_catalog
        self.tmp.cleanup()

    def _make_model(self, name: str, size_bytes: int, mtime: float) -> Path:
        d = self.models_dir / name
        d.mkdir()
        # Write a file of the requested size (sparse-friendly).
        f = d / "weights.gguf"
        with open(f, "wb") as fh:
            fh.truncate(size_bytes)
        os.utime(d, (mtime, mtime))
        os.utime(f, (mtime, mtime))
        return d

    def _patch_disk(self, free: int) -> None:
        model_store._free_bytes = lambda path: (self.total, free)

    def _synthetic_catalog(self, ids: list[str]) -> None:
        """Inject a catalog whose dir names match the temp fixtures.

        Production resolves on-disk dir → catalog id via the real catalog
        (``_catalog_id_for_dir``); the tests use synthetic dir names
        (``models--org--<n>``) that don't exist in the real catalog, so we
        override ``model_store._catalog_override`` with a matching catalog.
        """
        model_store._catalog_override = {
            "models": [
                {"id": n, "checkpoint": f"org/{n}:Q4_K_M"} for n in ids
            ]
        }

    def _fake_client(self, free: int, **kwargs) -> "_FakeLemondClient":
        client = _FakeLemondClient(total=self.total, free=free, **kwargs)
        client.models_dir = self.models_dir
        self.client = client
        return client

    def _run(self, **kwargs):
        return model_store.ensure_model_space(self.models_dir, client=self.client, **kwargs)

    def test_enough_space_no_eviction(self) -> None:
        self._make_model("Old", 10 * 1024 ** 3, 1000.0)
        self._make_model("New", 10 * 1024 ** 3, 2000.0)
        # Plenty of free space (90 GiB free, need 5 GiB).
        self._fake_client(90 * 1024 ** 3)
        result = self._run(needed_bytes=5 * 1024 ** 3, target_name="New")
        self.assertTrue(result["ok"])
        self.assertEqual(result["evicted"], [])
        self.assertEqual(self.client.deleted, [])
        self.assertTrue((self.models_dir / "Old").exists())
        self.assertTrue((self.models_dir / "New").exists())

    def test_tight_space_evicts_oldest_keeps_most_recent(self) -> None:
        # Oldest first; the most-recent prior model must be preserved. Directories
        # use the on-disk models--org--<n> shape; the client API is keyed by the
        # catalog id (the <n> part).
        self._make_model("models--org--Old1", 30 * 1024 ** 3, 1000.0)
        self._make_model("models--org--Old2", 30 * 1024 ** 3, 2000.0)
        self._make_model("models--org--MostRecent", 30 * 1024 ** 3, 3000.0)
        # Free space is low: need 25 GiB but only 20 GiB free (after 10 GiB
        # reserve, effective free is 10 GiB) → must evict to make room.
        self._synthetic_catalog(["Old1", "Old2", "MostRecent"])
        self._fake_client(20 * 1024 ** 3)
        result = self._run(needed_bytes=25 * 1024 ** 3, target_name="NewModel")
        self.assertTrue(result["ok"])
        # Oldest evicted first via the API (by catalog id); the most-recent prior
        # model is kept.
        self.assertIn("models--org--Old1", result["evicted"])
        self.assertIn("Old1", self.client.deleted)
        self.assertTrue(
            (self.models_dir / "models--org--MostRecent").exists(),
            "most-recent prior must be kept",
        )

    def test_loaded_model_is_never_evicted(self) -> None:
        # The oldest model is currently loaded in lemond (reported by catalog
        # id). It must be skipped even though it is the LRU candidate; the
        # next-oldest is evicted.
        self._make_model("models--org--Loaded", 30 * 1024 ** 3, 1000.0)
        self._make_model("models--org--Old2", 30 * 1024 ** 3, 2000.0)
        self._make_model("models--org--MostRecent", 30 * 1024 ** 3, 3000.0)
        self._synthetic_catalog(["Loaded", "Old2", "MostRecent"])
        self._fake_client(20 * 1024 ** 3, loaded={"Loaded"})
        result = self._run(needed_bytes=25 * 1024 ** 3, target_name="NewModel")
        self.assertTrue(result["ok"])
        self.assertNotIn("models--org--Loaded", result["evicted"])
        self.assertNotIn("Loaded", self.client.deleted)
        self.assertIn("models--org--Old2", result["evicted"])
        self.assertTrue(
            (self.models_dir / "models--org--Loaded").exists(),
            "loaded model must be kept",
        )

    def test_dir_name_casing_drift_still_protects_the_target(self) -> None:
        """The name on disk is minted upstream, not by our catalog.

        Found for real: the catalog carried ``gemma-4-12B`` while lemond's
        registry (and therefore the cache directory) uses ``gemma-4-12b``. With
        exact comparison the target's own weights were invisible to the guard,
        so it would evict the model it was preparing to download -- and the
        target sorts oldest here, so it is the first candidate.
        """
        target_dir = "models--org--gemma-4-12b-it-GGUF"
        self._make_model(target_dir, 30 * 1024 ** 3, 1000.0)
        self._make_model("models--org--Old2", 30 * 1024 ** 3, 2000.0)
        self._make_model("models--org--MostRecent", 30 * 1024 ** 3, 3000.0)
        model_store._catalog_override = {
            "models": [
                {"id": "Gemma-4-12B-it-GGUF",
                 "checkpoint": "org/gemma-4-12B-it-GGUF:Q4_K_M"},
                {"id": "Old2", "checkpoint": "org/Old2:Q4_K_M"},
                {"id": "MostRecent", "checkpoint": "org/MostRecent:Q4_K_M"},
            ]
        }
        self._fake_client(20 * 1024 ** 3)
        result = self._run(needed_bytes=25 * 1024 ** 3, target_name="Gemma-4-12B-it-GGUF")
        self.assertTrue(result["ok"])
        self.assertTrue((self.models_dir / target_dir).exists(),
                        "our own target must survive the guard")
        self.assertNotIn(target_dir, result["evicted"])
        self.assertIn("models--org--Old2", result["evicted"])

    def test_cannot_fit_refuses_with_message(self) -> None:
        # Only one model exists and it is the most-recent prior (protected).
        # Evicting it would leave no fallback, so we must refuse.
        self._make_model("OnlyModel", 90 * 1024 ** 3, 1000.0)
        # Almost no free space; the single protected model cannot be evicted.
        self._fake_client(5 * 1024 ** 3)
        with self.assertRaises(RuntimeError) as ctx:
            self._run(needed_bytes=20 * 1024 ** 3, target_name="NewModel")
        self.assertIn("Insufficient disk space", str(ctx.exception))
        # The protected model must still be on disk and not deleted.
        self.assertEqual(self.client.deleted, [])
        self.assertTrue((self.models_dir / "OnlyModel").exists())

    def test_first_download_empty_dir(self) -> None:
        # No models yet; enough free space → proceed without error.
        self._fake_client(80 * 1024 ** 3)
        result = self._run(needed_bytes=10 * 1024 ** 3, target_name="First")
        self.assertTrue(result["ok"])
        self.assertEqual(result["evicted"], [])

    def test_first_download_too_big_refuses(self) -> None:
        # Empty dir but the single new model alone won't fit with the reserve.
        self._fake_client(15 * 1024 ** 3)  # 10 GiB reserve → 5 GiB effective
        with self.assertRaises(RuntimeError) as ctx:
            self._run(needed_bytes=50 * 1024 ** 3, target_name="Big")
        self.assertIn("Insufficient disk space", str(ctx.exception))

    def test_disk_usage_failure_refuses(self) -> None:
        # API reports no usable storage AND the filesystem fallback reports
        # (0, 0) → cannot verify → refuse.
        model_store._free_bytes = lambda path: (0, 0)
        self._fake_client(0, api_down=True)
        with self.assertRaises(RuntimeError) as ctx:
            self._run(needed_bytes=10 * 1024 ** 3, target_name="X")
        self.assertIn("Cannot verify free disk space", str(ctx.exception))

    def test_respects_ten_percent_reserve(self) -> None:
        # Free space is just above the high-water mark but below the reserve
        # boundary for the requested size → must refuse rather than dip below
        # the 10% reserve.
        self._make_model("Old", 50 * 1024 ** 3, 1000.0)
        # 15 GiB free: reserve is 10 GiB, so effective free is 5 GiB.
        # Requesting 20 GiB cannot fit within the reserve.
        self._fake_client(15 * 1024 ** 3)
        with self.assertRaises(RuntimeError):
            self._run(needed_bytes=20 * 1024 ** 3, target_name="New")

    def test_api_down_falls_back_to_filesystem(self) -> None:
        # lemond is unreachable: sizes come from the on-disk walk, free space
        # from shutil.disk_usage (patched), and deletion is a raw rmtree that
        # actually removes the directory.
        self._make_model("Old1", 30 * 1024 ** 3, 1000.0)
        self._make_model("Old2", 30 * 1024 ** 3, 2000.0)
        self._make_model("MostRecent", 30 * 1024 ** 3, 3000.0)
        self._patch_disk(20 * 1024 ** 3)
        self._fake_client(20 * 1024 ** 3, api_down=True)
        result = self._run(needed_bytes=25 * 1024 ** 3, target_name="NewModel")
        self.assertTrue(result["ok"])
        self.assertIn("Old1", result["evicted"])
        # The raw rmtree fallback physically removed the directory.
        self.assertFalse((self.models_dir / "Old1").exists())
        self.assertTrue((self.models_dir / "MostRecent").exists())

    def test_no_client_uses_pure_filesystem(self) -> None:
        # client=None (SDK absent): everything from the filesystem, deletion
        # via rmtree.
        self._make_model("Old1", 30 * 1024 ** 3, 1000.0)
        self._make_model("MostRecent", 30 * 1024 ** 3, 3000.0)
        self._patch_disk(20 * 1024 ** 3)
        self.client = None
        result = self._run(needed_bytes=25 * 1024 ** 3, target_name="NewModel")
        self.assertTrue(result["ok"])
        self.assertIn("Old1", result["evicted"])
        self.assertFalse((self.models_dir / "Old1").exists())
        self.assertTrue((self.models_dir / "MostRecent").exists())

    def test_target_on_disk_is_protected(self) -> None:
        # Regression (Bug 1): the target's *own* on-disk directory must be
        # protected even when it is the oldest. target_name is a catalog id;
        # its dir (models--org--Old) is present and is the LRU candidate, but it
        # is the model we are about to (re)download, so it must not be evicted.
        self._make_model("models--org--Old", 30 * 1024 ** 3, 1000.0)
        self._make_model("models--org--Mid", 30 * 1024 ** 3, 2000.0)
        self._make_model("models--org--New", 30 * 1024 ** 3, 3000.0)
        self._synthetic_catalog(["Old", "Mid", "New"])
        self._fake_client(20 * 1024 ** 3)
        # Target is "Old" (present, oldest). The most-recent prior is "New".
        # Evicting "Mid" frees room; "Old" (target) and "New" (fallback) stay.
        result = self._run(needed_bytes=25 * 1024 ** 3, target_name="Old")
        self.assertTrue(result["ok"])
        self.assertNotIn("models--org--Old", result["evicted"])
        self.assertIn("models--org--Mid", result["evicted"])
        self.assertTrue((self.models_dir / "models--org--Old").exists())
        self.assertTrue((self.models_dir / "models--org--New").exists())

    def test_api_delete_uses_catalog_id_not_dir(self) -> None:
        # Regression (Bug 2): lemond's API is keyed by the catalog id, not the
        # on-disk dir name. The client must be asked to delete by catalog id
        # ("Old1"), never by the dir name ("models--org--Old1").
        self._make_model("models--org--Old1", 30 * 1024 ** 3, 1000.0)
        self._make_model("models--org--Old2", 30 * 1024 ** 3, 2000.0)
        self._make_model("models--org--MostRecent", 30 * 1024 ** 3, 3000.0)
        self._synthetic_catalog(["Old1", "Old2", "MostRecent"])
        self._fake_client(20 * 1024 ** 3)
        result = self._run(needed_bytes=25 * 1024 ** 3, target_name="NewModel")
        self.assertTrue(result["ok"])
        self.assertEqual(self.client.deleted, ["Old1"])
        for d in self.client.deleted:
            self.assertFalse(
                d.startswith("models--"),
                "delete must be keyed by catalog id, not the on-disk dir name",
            )

    def test_loaded_by_catalog_id_is_never_evicted(self) -> None:
        # Regression (Bug 1 loaded-match): lemond reports loaded models by
        # catalog id; the guard must translate that to the on-disk dir so a
        # resident model is not evicted. "Loaded" is loaded and oldest, so it
        # is the LRU candidate but must be skipped.
        self._make_model("models--org--Loaded", 30 * 1024 ** 3, 1000.0)
        self._make_model("models--org--Old2", 30 * 1024 ** 3, 2000.0)
        self._make_model("models--org--MostRecent", 30 * 1024 ** 3, 3000.0)
        self._synthetic_catalog(["Loaded", "Old2", "MostRecent"])
        self._fake_client(20 * 1024 ** 3, loaded={"Loaded"})
        result = self._run(needed_bytes=25 * 1024 ** 3, target_name="NewModel")
        self.assertTrue(result["ok"])
        self.assertNotIn("models--org--Loaded", result["evicted"])
        self.assertIn("models--org--Old2", result["evicted"])
        self.assertTrue((self.models_dir / "models--org--Loaded").exists())

    def test_api_delete_failure_skips_model_when_lemond_alive(self) -> None:
        # Regression (rmtree-under-running-server): lemond is ALIVE (api_up) but
        # refuses to delete the LRU candidate (e.g. model busy / in a
        # transitional state). We must NOT fall through to a raw rmtree — that
        # would orphan lemond's user_models.json and leave a phantom entry in
        # ``lemonade list``. The model is skipped and GC continues with the
        # next-oldest model, which the API accepts.
        self._make_model("models--org--Old1", 30 * 1024 ** 3, 1000.0)
        self._make_model("models--org--Old2", 30 * 1024 ** 3, 2000.0)
        self._make_model("models--org--MostRecent", 30 * 1024 ** 3, 3000.0)
        self._synthetic_catalog(["Old1", "Old2", "MostRecent"])
        client = self._fake_client(20 * 1024 ** 3, delete_fail={"Old1"})
        result = self._run(needed_bytes=25 * 1024 ** 3, target_name="NewModel")
        self.assertTrue(result["ok"])
        # Old1: API refused → skipped, directory must still exist untouched.
        self.assertNotIn("models--org--Old1", result["evicted"])
        self.assertNotIn("Old1", client.deleted)
        self.assertTrue(
            (self.models_dir / "models--org--Old1").exists(),
            "refused delete must not rmtree the directory",
        )
        # GC continues: Old2 is evicted via the API, MostRecent is kept.
        self.assertIn("models--org--Old2", result["evicted"])
        self.assertIn("Old2", client.deleted)
        self.assertTrue((self.models_dir / "models--org--MostRecent").exists())

    def test_api_delete_failure_all_refused_refuses_download(self) -> None:
        # lemond is alive but refuses every eviction → we cannot reclaim space
        # and must refuse the download rather than rmtree anything.
        self._make_model("models--org--Old1", 30 * 1024 ** 3, 1000.0)
        self._make_model("models--org--Old2", 30 * 1024 ** 3, 2000.0)
        self._make_model("models--org--MostRecent", 30 * 1024 ** 3, 3000.0)
        self._synthetic_catalog(["Old1", "Old2", "MostRecent"])
        client = self._fake_client(20 * 1024 ** 3, delete_fail={"Old1", "Old2"})
        with self.assertRaises(RuntimeError) as ctx:
            self._run(needed_bytes=25 * 1024 ** 3, target_name="NewModel")
        self.assertIn("Insufficient disk space", str(ctx.exception))
        self.assertEqual(client.deleted, [])
        # Nothing was rmtree'd: every directory is still on disk.
        for d in ("models--org--Old1", "models--org--Old2", "models--org--MostRecent"):
            self.assertTrue((self.models_dir / d).exists(), f"{d} must survive")


class ModelDirNameTests(unittest.TestCase):
    """catalog.model_dir_name: catalog id → on-disk models--org--repo dir."""

    def test_known_models(self) -> None:
        self.assertEqual(
            catalog.model_dir_name("Gemma-4-E2B-it-GGUF"),
            "models--unsloth--gemma-4-E2B-it-GGUF",
        )
        self.assertEqual(
            catalog.model_dir_name("Gemma-4-12B-it-GGUF"),
            "models--unsloth--gemma-4-12b-it-GGUF",
        )
        self.assertEqual(
            catalog.model_dir_name("Qwen3.5-9B-GGUF"),
            "models--unsloth--Qwen3.5-9B-GGUF",
        )
        self.assertEqual(
            catalog.model_dir_name("Nemotron-3-Nano-30B-A3B-GGUF"),
            "models--nvidia--Nemotron-3-Nano-30B-A3B-GGUF",
        )
        self.assertEqual(
            catalog.model_dir_name("LFM2.5-8B-A1B"),
            "models--LiquidAI--LFM2.5-8B-A1B-GGUF",
        )
        self.assertEqual(
            catalog.model_dir_name("LFM2.5-1.2B-Instruct-GGUF"),
            "models--LiquidAI--LFM2.5-1.2B-Instruct-GGUF",
        )

    def test_every_catalog_model_resolves_to_a_dir(self) -> None:
        # The disk-space guard relies on this for every catalog entry; a model
        # without a resolvable checkpoint would silently fall back to the
        # unsafe filesystem path.
        cat = catalog.load_catalog()
        for m in cat.get("models") or []:
            if isinstance(m, dict):
                self.assertIsNotNone(
                    catalog.model_dir_name(str(m.get("id")), cat),
                    f"model {m.get('id')} has no resolvable checkpoint",
                )

    def test_unknown_id_returns_none(self) -> None:
        self.assertIsNone(catalog.model_dir_name("Not-A-Model"))


class StubHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(
            (stub.HOST, stub.PORT),
            stub.Handler,
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, bytes]:
        req = Request(f"http://{stub.HOST}:{stub.PORT}{path}", headers=headers or {})
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def test_healthz(self) -> None:
        code, body = self._get("/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(body.strip(), b"ok")

    def test_client_disconnect_during_response_is_quiet(self) -> None:
        """A client that drops mid-response must not spam tracebacks."""
        with mock.patch.object(stub.Handler, "log_message") as log:
            conn = socket.create_connection((stub.HOST, stub.PORT))
            try:
                conn.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                conn.shutdown(socket.SHUT_RDWR)
            finally:
                conn.close()
            deadline = time.time() + 5
            while time.time() < deadline:
                calls = [c for c in log.call_args_list if "disconnect" in str(c)]
                if calls:
                    break
                time.sleep(0.05)
        self.assertTrue(
            any("disconnect" in str(c) for c in log.call_args_list),
            f"expected a quiet disconnect log, got {log.call_args_list[:5]}",
        )

    @mock.patch.object(hardware, "collect_hardware")
    @mock.patch.object(hardware, "persist_hardware")
    def test_api_plan(self, _persist: mock.Mock, collect: mock.Mock) -> None:
        collect.return_value = _cuda_hw()
        code, body = self._get("/api/plan")
        self.assertEqual(code, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["state"], "selected")

    def _post(self, path: str, payload: dict, headers: dict | None = None) -> tuple[int, bytes]:
        req = Request(
            f"http://{stub.HOST}:{stub.PORT}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    @mock.patch.object(hardware, "collect_hardware")
    @mock.patch.object(hardware, "persist_hardware")
    def test_api_models_get(self, _persist: mock.Mock, collect: mock.Mock) -> None:
        collect.return_value = _cuda_hw(vram=12.0, ram=16.0)
        code, body = self._get("/_lemonseed/api/models")
        self.assertEqual(code, 200)
        data = json.loads(body.decode())
        self.assertEqual(len(data["families"]), 4)
        self.assertEqual(data["plan"]["state"], "selected")

    def test_api_models_estimate_was_removed(self) -> None:
        """The speed-estimate endpoint is gone: lemond exposes no download
        progress, so the probe could only guess (and pointed at a dead URL)."""
        code, _body = self._get("/_lemonseed/api/models/estimate")
        self.assertEqual(code, 404)

    @mock.patch.object(catalog, "write_model_choice")
    @mock.patch.object(chat_ready, "ensure_chat_worker")
    def test_api_models_post_requires_admin(self, _worker: mock.Mock, write: mock.Mock) -> None:
        code, _body = self._post("/_lemonseed/api/models", {"family": "qwen"})
        self.assertEqual(code, 403)
        write.assert_not_called()

    @mock.patch.object(catalog, "write_model_choice")
    @mock.patch.object(chat_ready, "ensure_chat_worker")
    def test_api_models_post_admin_rejects_unknown_family(
        self, _worker: mock.Mock, write: mock.Mock
    ) -> None:
        code, _body = self._post(
            "/_lemonseed/api/models", {"family": "bogus"}, headers={"X-Forwarded-Groups": "admin"}
        )
        self.assertEqual(code, 400)
        write.assert_not_called()

    @mock.patch.object(catalog, "write_model_choice")
    @mock.patch.object(chat_ready, "ensure_chat_worker")
    def test_api_models_post_admin_saves_choice(
        self, worker: mock.Mock, write: mock.Mock
    ) -> None:
        write.return_value = {"family": "qwen", "model_id": None}
        code, body = self._post(
            "/_lemonseed/api/models", {"family": "qwen"}, headers={"X-Forwarded-Groups": "admin"}
        )
        self.assertEqual(code, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["choice"]["family"], "qwen")
        write.assert_called_once_with("qwen", None)
        worker.assert_called_once()

    @mock.patch.object(catalog, "write_model_choice")
    @mock.patch.object(chat_ready, "ensure_chat_worker")
    def test_api_models_post_admin_saves_explicit_model(
        self, worker: mock.Mock, write: mock.Mock
    ) -> None:
        write.return_value = {
            "family": "gemma",
            "model_id": "Gemma-4-12B-it-GGUF",
        }
        code, body = self._post(
            "/_lemonseed/api/models",
            {"family": "gemma", "model_id": "Gemma-4-12B-it-GGUF"},
            headers={"X-Forwarded-Groups": "admin"},
        )
        self.assertEqual(code, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["choice"]["model_id"], "Gemma-4-12B-it-GGUF")
        write.assert_called_once_with("gemma", "Gemma-4-12B-it-GGUF")
        worker.assert_called_once()

    @mock.patch.object(catalog, "write_model_choice")
    @mock.patch.object(chat_ready, "ensure_chat_worker")
    def test_api_models_post_admin_rejects_unknown_model(
        self, _worker: mock.Mock, write: mock.Mock
    ) -> None:
        code, _body = self._post(
            "/_lemonseed/api/models",
            {"family": "gemma", "model_id": "Not-A-Model"},
            headers={"X-Forwarded-Groups": "admin"},
        )
        self.assertEqual(code, 400)
        write.assert_not_called()

    @mock.patch.object(stub, "setup_complete", return_value=True)
    @mock.patch.object(auth, "reset_marker_present", return_value=False)
    @mock.patch.object(auth, "admin_bootstrap_required", return_value=False)
    def test_root_serves_shell_when_setup_complete(
        self,
        _bootstrap: mock.Mock,
        _reset: mock.Mock,
        _complete: mock.Mock,
    ) -> None:
        code, body = self._get("/")
        self.assertEqual(code, 200)
        html = body.decode()
        self.assertIn("app-shell", html)
        # The shell must bail out to /login when its session expires, instead
        # of polling forever behind a dead session (nested-shell bug).
        self.assertIn("window.location.replace('/login?rd='", html)

    @mock.patch.object(stub, "boot_snapshot")
    def test_api_status_role(self, boot: mock.Mock) -> None:
        boot.return_value = {
            "version": "test",
            "startup_ready": False,
            "lemond": {"state": "starting"},
            "docker": {"state": "ready"},
            "units": {},
        }
        code, body = self._get("/api/status")
        self.assertEqual(code, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["role"], "control-plane")
        self.assertEqual(data["status"], "ok")

    @mock.patch.object(stub.data_services, "ensure_data_worker")
    @mock.patch.object(stub.data_services, "ensure_provision_worker")
    @mock.patch.object(stub.data_services, "data_snapshot")
    def test_api_data(
        self, snap: mock.Mock, _worker: mock.Mock, _data_worker: mock.Mock
    ) -> None:
        snap.return_value = {
            "postgres": {"state": "starting", "host": "127.0.0.1:5432"},
            "valkey": {"state": "ready", "host": "127.0.0.1:6379"},
            "provisioning": {"state": "idle"},
        }
        code, body = self._get("/api/data")
        self.assertEqual(code, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["valkey"]["state"], "ready")
        _worker.assert_called_once()
        _data_worker.assert_called_once()

    @mock.patch.object(stub.modules, "ensure_module_worker")
    @mock.patch.object(stub.modules, "modules_snapshot")
    @mock.patch.object(stub.data_services, "ensure_data_worker")
    @mock.patch.object(stub.data_services, "ensure_provision_worker")
    def test_api_modules(
        self,
        _prov: mock.Mock,
        _data_worker: mock.Mock,
        snap: mock.Mock,
        _worker: mock.Mock,
    ) -> None:
        snap.return_value = {
            "open_webui": {
                "state": "ready",
                "path": "/chat/",
                "port": 18080,
                "http_ready": True,
            }
        }
        code, body = self._get("/api/modules")
        self.assertEqual(code, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["open_webui"]["path"], "/chat/")
        _worker.assert_called_once()


OWUI_EXPORT_FIXTURE = {
    "openai.enable": True,
    "openai.api_base_urls": ["http://host.docker.internal/_lemonseed/lemond/v1"],
    "openai.api_keys": ["sk-PLAINTEXT"],
    "openai.api_configs": {"http://host.docker.internal/_lemonseed/lemond/v1": {"valves": {}}},
    "ollama.enable": False,
    "web.search.enable": True,
    "vector_db.chunks_per_collection": {"docs": 10},
}


class OwuiConfigViewTests(unittest.TestCase):
    """GET /_lemonseed/api/owui/config — admin-only ownership view (4A).

    The handler is a fetch-and-serialize shim over owui_portal.build_view, so
    these tests are about the gate, the degradation paths and the payload never
    carrying a credential. Classification math lives in test_owui_portal.py.

    Own server fixture: test classes run one at a time, so binding the stub's
    port again after StubHttpTests has torn its server down is fine.
    """

    ADMIN = {"X-Forwarded-Groups": "admin"}

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer((stub.HOST, stub.PORT), stub.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, bytes]:
        req = Request(f"http://{stub.HOST}:{stub.PORT}{path}", headers=headers or {})
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.read()
        except HTTPError as exc:
            return exc.code, exc.read()

    def _status(self) -> dict:
        return {
            "ok": True, "state": "ok", "manifest_version": 1,
            "policy_hash": "sha256:abc", "owui_version": "0.11.0",
            "last_reconciled": "2026-10-06T04:00:00+00:00", "last_trigger": ["at_boot"],
            "last_error": None, "manifest_error": None, "key_ready": True,
            "lkg_at": "2026-10-06T04:00:00+00:00",
            "drift": {"ok": 20, "fixed": 0, "pending": 0, "error": 0, "drift": 0,
                      "entries": []},
        }

    @mock.patch.object(stub.modules, "owui_config_export")
    def test_requires_admin(self, export: mock.Mock) -> None:
        code, _body = self._get("/_lemonseed/api/owui/config")
        self.assertEqual(code, 403)
        # A member must not even cause a service sign-in to Open WebUI.
        export.assert_not_called()

    @mock.patch.object(stub.owui_policy, "state_summary")
    @mock.patch.object(stub.modules, "owui_config_export")
    def test_admin_gets_ownership_view(self, export: mock.Mock, status: mock.Mock) -> None:
        export.return_value = {"ok": True, "data": dict(OWUI_EXPORT_FIXTURE), "error": None}
        status.return_value = self._status()
        code, body = self._get("/_lemonseed/api/owui/config", headers=self.ADMIN)
        self.assertEqual(code, 200)
        view = json.loads(body.decode())
        self.assertTrue(view["ok"])
        self.assertTrue(view["live_available"])
        self.assertEqual(view["policy"]["manifest_version"], 1)
        manifest = stub.owui_policy.load_manifest()
        self.assertEqual(
            view["counts"]["managed"],
            sum(1 for entry in manifest.settings.values() if entry.mode == "managed"),
        )
        sections = {s["section"]: s for s in view["sections"]}
        self.assertEqual(sections["openai"]["rollup"], "ours")
        self.assertEqual(sections["vector_db"]["rollup"], "yours")
        rows = {r["key"]: r for s in view["sections"] for r in s["entries"]}
        self.assertEqual(rows["web.search.enable"]["classification"], "managed")
        self.assertEqual(rows["vector_db.chunks_per_collection"]["classification"], "yours")
        self.assertEqual({e["kind"] for e in view["entities"]}, {"models", "users"})
        self.assertTrue(view["guards"])
        self.assertTrue(all(g["enforced"] is False for g in view["guards"]))
        self.assertNotIn("sk-PLAINTEXT", body.decode(), "payload carried a plaintext key")

    @mock.patch.object(stub.owui_policy, "state_summary")
    @mock.patch.object(stub.modules, "owui_config_export")
    def test_export_failure_still_renders_intent(self, export: mock.Mock,
                                                 status: mock.Mock) -> None:
        export.return_value = {"ok": False, "data": None,
                               "error": "open webui is not ready"}
        status.return_value = self._status()
        code, body = self._get("/_lemonseed/api/owui/config", headers=self.ADMIN)
        self.assertEqual(code, 200)
        view = json.loads(body.decode())
        self.assertFalse(view["live_available"])
        self.assertEqual(view["export_error"], "open webui is not ready")
        # Intent-only: every manifest key renders, none are "yours", nothing is
        # claimed present (the panel says so via live_available).
        self.assertTrue(view["sections"])
        self.assertEqual(view["counts"]["yours"], 0)
        rows = [r for s in view["sections"] for r in s["entries"]]
        self.assertTrue(all(not r["present"] for r in rows))

    @mock.patch.object(stub.owui_policy, "state_summary")
    @mock.patch.object(stub.modules, "owui_config_export")
    def test_bad_manifest_degrades_to_yours(self, export: mock.Mock,
                                            status: mock.Mock) -> None:
        export.return_value = {"ok": True, "data": dict(OWUI_EXPORT_FIXTURE), "error": None}
        status.return_value = self._status()
        with mock.patch.object(stub.owui_policy, "load_manifest",
                               side_effect=stub.owui_policy.ManifestError("nope")):
            code, body = self._get("/_lemonseed/api/owui/config", headers=self.ADMIN)
        self.assertEqual(code, 200)
        view = json.loads(body.decode())
        self.assertEqual(view["counts"]["managed"], 0)
        self.assertEqual(view["policy"]["manifest_error"], "nope")

    @mock.patch.object(stub.owui_policy, "state_summary")
    @mock.patch.object(stub.modules, "owui_config_export")
    def test_view_builder_failure_is_a_clean_502(self, export: mock.Mock,
                                                 status: mock.Mock) -> None:
        export.return_value = {"ok": True, "data": {"openai.api_keys": ["sk-x"]},
                               "error": None}
        status.return_value = self._status()
        with mock.patch.object(stub.owui_portal, "build_view",
                               side_effect=RuntimeError("boom")):
            code, body = self._get("/_lemonseed/api/owui/config", headers=self.ADMIN)
        self.assertEqual(code, 502)
        self.assertIn("config view failed", json.loads(body.decode())["error"])


class OwuiAdminActionTests(unittest.TestCase):
    """POST /_lemonseed/api/owui/{reconcile,reset-config} — Phase 4B surface.

    These two endpoints can rewrite (and, for reset, delete) the Open WebUI
    config, so the tests care about exactly three things: the admin gate runs
    before any work, the status code tells the portal which branch it is on,
    and the destructive path cannot be reached without the typed confirmation.
    """

    ADMIN = {"X-Forwarded-Groups": "admin"}
    MEMBER = {"X-Forwarded-Groups": "member"}

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer((stub.HOST, stub.PORT), stub.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _post(self, path: str, payload: dict | None = None,
              headers: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload or {}).encode()
        req = Request(
            f"http://{stub.HOST}:{stub.PORT}{path}",
            data=body,
            headers={**(headers or {}), "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except HTTPError as exc:
            raw = exc.read()
            try:
                return exc.code, json.loads(raw.decode() or "{}")
            except json.JSONDecodeError:
                return exc.code, {"raw": raw.decode(errors="replace")}

    @mock.patch.object(stub.modules, "request_policy_reconcile")
    def test_reconcile_requires_admin(self, reconcile: mock.Mock) -> None:
        for headers in ({}, self.MEMBER):
            code, _body = self._post("/_lemonseed/api/owui/reconcile", {}, headers)
            self.assertEqual(code, 403)
        reconcile.assert_not_called()

    @mock.patch.object(stub.modules, "request_policy_reconcile",
                       return_value={"ok": True, "reason": None, "policy_hash": "sha256:x",
                                     "drift": {"ok": 4, "fixed": 1}, "last_error": None})
    def test_reconcile_ok_is_200(self, _rec: mock.Mock) -> None:
        code, body = self._post("/_lemonseed/api/owui/reconcile", {}, self.ADMIN)
        self.assertEqual(code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["drift"], {"ok": 4, "fixed": 1})

    @mock.patch.object(stub.modules, "request_policy_reconcile",
                       return_value={"ok": False, "reason": "in flight"})
    def test_reconcile_contention_is_409(self, _rec: mock.Mock) -> None:
        code, body = self._post("/_lemonseed/api/owui/reconcile", {}, self.ADMIN)
        self.assertEqual(code, 409)
        self.assertEqual(body["reason"], "in flight")

    @mock.patch.object(stub.modules, "request_policy_reconcile",
                       return_value={"ok": False, "reason": "not ready"})
    def test_reconcile_not_ready_is_503(self, _rec: mock.Mock) -> None:
        code, body = self._post("/_lemonseed/api/owui/reconcile", {}, self.ADMIN)
        self.assertEqual(code, 503)
        self.assertEqual(body["reason"], "not ready")

    @mock.patch.object(stub.modules, "reset_owui_config")
    def test_reset_requires_admin(self, reset: mock.Mock) -> None:
        for headers in ({}, self.MEMBER):
            code, _body = self._post(
                "/_lemonseed/api/owui/reset-config",
                {"confirm": stub.modules.OWUI_RESET_CONFIRM}, headers)
            self.assertEqual(code, 403)
        reset.assert_not_called()

    @mock.patch.object(stub.modules, "reset_owui_config", return_value={"accepted": True,
                                                                       "reason": None})
    def test_reset_accepted_is_202(self, reset: mock.Mock) -> None:
        code, _body = self._post(
            "/_lemonseed/api/owui/reset-config",
            {"confirm": stub.modules.OWUI_RESET_CONFIRM}, self.ADMIN)
        self.assertEqual(code, 202)
        # The confirm text is forwarded verbatim; the CP is the authority on it.
        reset.assert_called_once_with(stub.modules.OWUI_RESET_CONFIRM)

    @mock.patch.object(stub.modules, "reset_owui_config",
                       return_value={"accepted": False, "reason": "confirmation text did not match"})
    def test_reset_without_the_phrase_is_400(self, reset: mock.Mock) -> None:
        code, body = self._post("/_lemonseed/api/owui/reset-config",
                               {"confirm": "yes"}, self.ADMIN)
        self.assertEqual(code, 400)
        self.assertEqual(body["confirm_text"], stub.modules.OWUI_RESET_CONFIRM)

    @mock.patch.object(stub.modules, "reset_owui_config",
                       return_value={"accepted": False, "reason": "a change is already in progress"})
    def test_reset_contention_is_409(self, _reset: mock.Mock) -> None:
        code, _body = self._post(
            "/_lemonseed/api/owui/reset-config",
            {"confirm": stub.modules.OWUI_RESET_CONFIRM}, self.ADMIN)
        self.assertEqual(code, 409)

    @mock.patch.object(stub.modules, "reset_owui_config",
                       return_value={"accepted": False, "reason": "not ready"})
    def test_reset_not_ready_is_503(self, _reset: mock.Mock) -> None:
        code, _body = self._post(
            "/_lemonseed/api/owui/reset-config",
            {"confirm": stub.modules.OWUI_RESET_CONFIRM}, self.ADMIN)
        self.assertEqual(code, 503)

    @mock.patch.object(stub.modules, "reset_owui_config")
    def test_reset_rejects_a_non_json_body(self, reset: mock.Mock) -> None:
        req = Request(f"http://{stub.HOST}:{stub.PORT}/_lemonseed/api/owui/reset-config",
                      data=b"confirm=please",
                      headers={**self.ADMIN, "Content-Type": "application/x-www-form-urlencoded"},
                      method="POST")
        try:
            with urlopen(req, timeout=5) as resp:
                code = resp.status
        except HTTPError as exc:
            code = exc.code
        self.assertEqual(code, 400)
        reset.assert_not_called()


class OwuiConfigPanelTests(unittest.TestCase):
    """The settings page markup the view renders into."""

    SNAP = {
        "version": "test",
        "chat": {"state": "ready", "display_name": "Lemonseed AI"},
        "hardware": {"gpus": []},
        "modules": {"open_webui": {"state": "ready"}},
    }
    # What the engine reports once a boot reconcile has succeeded. Kept out of
    # SNAP so the markup tests also prove the panel degrades without it.
    POLICY = {
        "ok": True, "state": "ok", "owui_version": "0.11.0",
        "last_reconciled": "2026-10-09T05:05:00+00:00", "last_error": None,
        "drift": {"ok": 20, "fixed": 0, "pending": 0, "error": 0},
    }

    @classmethod
    def snap(cls, policy: dict | None = None) -> dict:
        """A snapshot with the engine's policy state injected."""
        snap = {"version": "test",
                "chat": {"state": "ready", "display_name": "Lemonseed AI"},
                "hardware": {"gpus": []},
                "modules": {"open_webui": {"state": "ready",
                                           "owui_policy": dict(policy or cls.POLICY)}}}
        return snap

    def setUp(self) -> None:
        # page_settings_content asks docker for the container's own status line.
        # A developer machine (or CI runner) that happens to run a container
        # called lemonseed-owui-18080 must not change these markup assertions,
        # so answer "no docker" unless a test opts in.
        docker = mock.patch.object(stub.modules, "owui_container_status", return_value=None)
        docker.start()
        self.addCleanup(docker.stop)

    def test_settings_page_hosts_the_panel_and_its_script(self) -> None:
        html = stub.page_settings_content(dict(self.SNAP)).decode()
        self.assertIn('id="owui-config"', html)
        for element in ("owui-strip", "owui-state", "owui-title", "owui-note",
                        "owui-config-state", "owui-refresh", "owui-apply",
                        "owui-reset", "owui-reset-status"):
            self.assertIn(f'id="{element}"', html)
        self.assertIn("loadOwuiConfig", html)
        # The reset confirm phrase is server-rendered from the same constant the
        # CP checks, so the two cannot drift apart.
        self.assertIn(f'data-confirm="{stub.modules.OWUI_RESET_CONFIRM}"', html)
        for endpoint in ("/_lemonseed/api/owui/reconcile",
                         "/_lemonseed/api/owui/reset-config"):
            self.assertIn(endpoint, html)
        self.assertIn("/_lemonseed/api/owui/config", html)

    def test_chat_is_one_panel_with_status_and_controls(self) -> None:
        """One Chat box, two subsections (bare-metal review).

        The page used to carry an empty "Chat" panel next to a separate "Chat
        interface settings" panel, which read as two unrelated things.
        """
        html = stub.page_settings_content(dict(self.SNAP)).decode()
        self.assertEqual(html.count("<h2>Chat</h2>"), 1)
        self.assertNotIn("Chat interface settings", html)
        section = html.split('id="owui-config"', 1)[1].split("</section>", 1)[0]
        order = [section.index("<h3 class=\"subhead\">Status</h3>"),
                 section.index('id="owui-state"'),
                 section.index('id="owui-config-state"'),
                 section.index("<h3 class=\"subhead\">Controls</h3>"),
                 section.index('id="owui-apply"')]
        self.assertEqual(order, sorted(order), "Status, then its rows, then Controls")
        # The iframe path row was developer trivia and read like a broken link.
        self.assertNotIn("/?embed=1", html)
        self.assertNotIn('>path<', html)

    @mock.patch.object(stub.modules, "owui_container_status",
                       return_value=("Up 59 minutes (healthy)", "ghcr.io/open-webui/open-webui:v0.11.0"))
    def test_status_row_uses_docker_words(self, _status: mock.Mock) -> None:
        html = stub.page_settings_content(self.snap()).decode()
        self.assertIn('id="owui-state">Up 59 minutes (healthy)<', html)
        self.assertIn('class="state ok" id="owui-state"', html)
        # Bare-metal review: name the software and its version in the row that
        # used to say only "open webui".
        self.assertIn('>Open WebUI v0.11.0<', html)

    @mock.patch.object(stub.modules, "owui_container_status",
                       return_value=("Exited (137) 3 minutes ago", "image"))
    def test_status_row_marks_a_stopped_container_bad(self, _status: mock.Mock) -> None:
        html = stub.page_settings_content(dict(self.SNAP)).decode()
        self.assertIn('id="owui-state">Exited (137) 3 minutes ago<', html)
        self.assertIn('class="state bad" id="owui-state"', html)

    def test_status_row_falls_back_to_the_lifecycle_word(self) -> None:
        """Without docker (dev machine) show the CP's state, never an uptime."""
        html = stub.page_settings_content(dict(self.SNAP)).decode()
        self.assertIn('id="owui-state">ready<', html)
        # No version to show either: the row keeps its plain label rather than
        # inventing one.
        self.assertIn('id="owui-title">open webui<', html)

    def test_version_label_falls_back_to_the_image_tag(self) -> None:
        """Before the first reconcile the engine has no version; the pinned
        image tag still identifies what the box is running."""
        snap = self.snap({"ok": False, "state": "idle", "owui_version": None,
                          "last_reconciled": None, "last_error": None})
        snap["modules"]["open_webui"]["image"] = "ghcr.io/open-webui/open-webui:v0.11.0"
        html = stub.page_settings_content(snap).decode()
        self.assertIn('>Open WebUI v0.11.0<', html)

    def test_configuration_row_reports_the_reconcile_time(self) -> None:
        """The pill became a row (bare-metal review): when, not just whether."""
        html = stub.page_settings_content(self.snap()).decode()
        self.assertIn("Lemonseed configuration", html)
        self.assertIn('class="state ok" id="owui-config-state">updated '
                      '2026-10-09 05:05 UTC<', html)
        self.assertNotIn("Lemonseed settings applied", html)

    def test_configuration_row_marks_a_failed_reconcile(self) -> None:
        policy = dict(self.POLICY, ok=False,
                      last_error="installed OWUI '0.10.2' < manifest min_version '0.11.0'")
        html = stub.page_settings_content(self.snap(policy)).decode()
        self.assertIn('id="owui-config-state"', html)
        self.assertIn('>needs attention</span>', html)
        self.assertIn('class="state bad" id="owui-config-state"', html)
        # The reason has to be reachable somewhere, and hover is the only
        # place it fits without putting a stack trace on a settings page.
        self.assertIn('title="installed OWUI &#x27;0.10.2&#x27; &lt; manifest', html)

    def test_configuration_row_without_a_reconcile_is_not_ok(self) -> None:
        """Never imply settings were applied when nothing has run yet."""
        html = stub.page_settings_content(dict(self.SNAP)).decode()
        self.assertIn('id="owui-config-state">not applied yet<', html)
        self.assertIn('class="state bad" id="owui-config-state"', html)

    def test_utc_stamps_have_one_format(self) -> None:
        """One format for both the server rows and the panel JS."""
        self.assertEqual(stub._fmt_utc("2026-10-09T05:05:00+00:00"),
                         "2026-10-09 05:05 UTC")
        self.assertEqual(stub._fmt_utc("2026-10-09T05:05:00Z"),
                         "2026-10-09 05:05 UTC")
        # A non-UTC offset is normalized, not mislabeled.
        self.assertEqual(stub._fmt_utc("2026-10-09T22:05:00-04:00"),
                         "2026-10-10 02:05 UTC")
        self.assertEqual(stub._fmt_utc(""), "")
        # Unparseable input is shortened rather than dropped: a timestamp that
        # looks odd still beats a blank row.
        self.assertEqual(stub._fmt_utc("yesterday-ish"), "yesterday-ish")

    def test_version_label_helper(self) -> None:
        self.assertEqual(stub._owui_version_label("0.11.0", None), "v0.11.0")
        self.assertEqual(stub._owui_version_label("v0.11.0", None), "v0.11.0")
        self.assertEqual(
            stub._owui_version_label(None, "ghcr.io/open-webui/open-webui:v0.11.0"),
            "v0.11.0")
        # A registry host:port must never be mistaken for a version.
        self.assertEqual(stub._owui_version_label(None, "localhost:5000/open-webui"), "")
        self.assertEqual(stub._owui_version_label(None, None), "")

    def test_controls_copy_names_the_engine_and_both_actions(self) -> None:
        """Reviewed copy: what Lemonseed does, and what each button does."""
        html = stub.page_settings_content(dict(self.SNAP)).decode()
        controls = html.split('Controls</h3>', 1)[1]
        self.assertIn(
            "Lemonseed configures the chat interface (Open WebUI) to work with the "
            "Lemonade AI engine. Here you can trigger Lemonseed to reapply the "
            "managed configuration parameters or reset the Open WebUI configuration "
            "database to defaults.", controls)
        # "Refresh" alone did not say what it refreshes.
        self.assertIn('>Refresh status</button>', controls)

    def test_api_panel_is_titled(self) -> None:
        """The raw-endpoints disclosure needs a home admins can recognize; it
        is also where OpenAI-compatible access will be toggled."""
        html = stub.page_settings_content(dict(self.SNAP)).decode()
        self.assertEqual(html.count("<h2>API</h2>"), 1)
        self.assertIn('cursor:pointer;color:var(--muted)">System API Data</summary>', html)
        self.assertNotIn("Diagnostics", html)
        for endpoint in ("/api/hardware", "/api/status"):
            self.assertIn(endpoint, html)

    def test_ownership_detail_is_gone_and_the_dead_link_is_gone(self) -> None:
        """Bare-metal review: the per-key view is diagnostics, not UI.

        A home admin needs two buttons and one honest status line. The key tree
        and entity cards are gone entirely (developers read the manifest, not
        the portal), and so is the collapsed Advanced details box that replaced
        it -- version, image and reconcile time moved into the Status rows,
        where they are actually read.
        """
        html = stub.page_settings_content(dict(self.SNAP)).decode()
        section = html.split('id="owui-config"', 1)[1].split("</section>", 1)[0]
        for detail in ('id="owui-tree"', 'id="owui-cards"', "Enforcement at the gateway",
                       'class="owui-advanced"', "Advanced details", 'id="owui-tech"'):
            self.assertNotIn(detail, section)
        self.assertNotIn("owui-open-admin", html)
        self.assertNotIn("Open Open WebUI settings", html)
        # Reset progress is a banner the admin cannot miss, not a muted hint.
        self.assertIn('class="owui-status" id="owui-reset-status" hidden', html)

    def test_problem_strip_is_hidden_until_there_is_a_problem(self) -> None:
        """An empty flex box still occupies a line, so the strip needs hidden."""
        html = stub.page_settings_content(dict(self.SNAP)).decode()
        self.assertIn('<div class="owui-strip" id="owui-strip" hidden></div>', html)
        self.assertIn(".owui-strip[hidden] { display: none; }", stub.OWUI_CONFIG_CSS)
        # Only problems get a pill, and they are always rendered bad.
        self.assertNotIn("owui-pill ok", stub.OWUI_CONFIG_JS)
        self.assertIn("strip.hidden = !strip.firstChild", stub.OWUI_CONFIG_JS)

    def test_panel_js_does_not_render_the_ownership_tree(self) -> None:
        """Dead JS is worse than none: the payload still carries the tree, so
        the panel must not grow a renderer back by accident."""
        self.assertNotIn("renderOwuiTree", stub.OWUI_CONFIG_JS)
        self.assertNotIn("renderOwuiEntities", stub.OWUI_CONFIG_JS)
        self.assertNotIn("owui-cards", stub.OWUI_CONFIG_JS)
        # Nothing may navigate the Open WebUI frame from the panel anymore; the
        # shell-side postMessage bridge went away with it (open-redirect risk).
        self.assertNotIn("postMessage", stub.OWUI_CONFIG_JS)
        shell = stub.page_shell({"version": "test"}, view="chat").decode()
        self.assertNotIn("owui-admin", shell)
        self.assertNotIn("OWUI_ADMIN_ROUTES", shell)

    def test_quiet_note_does_not_leave_a_gap(self) -> None:
        """Status and Controls must not be separated by an empty paragraph."""
        self.assertIn("note.hidden = !text", stub.OWUI_CONFIG_JS)

    def test_reset_holds_the_interstitial_and_drops_the_cached_chat(self) -> None:
        """A reset stops Open WebUI; the loaded SPA must go with it (finding 9)."""
        html = stub.page_shell({"version": "test"}, view="chat").decode()
        self.assertIn("function owuiResetting", html)
        self.assertIn("function retireChatFrame", html)
        self.assertIn("if (owuiResetting(owui)) retireChatFrame();", html)
        self.assertIn("Rebuilding chat settings", html)
        # Terminal states must not pin the interstitial open forever.
        self.assertIn("reset.state !== 'ready'", html)
        self.assertIn("reset.state !== 'error'", html)


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
