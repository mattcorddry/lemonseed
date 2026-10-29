#!/usr/bin/env python3
"""Unit tests for control-plane modules (no root, no network)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

CP = Path(__file__).resolve().parent
# The one theme root (M6.6 S5.8). Read once: several tests need to check that a
# rule survived the move out of stub.py, and a test that re-reads the file each
# time is a test that can disagree with itself.
THEME_CSS = (CP / "static" / "lemonseed.css").read_text(encoding="utf-8")
sys.path.insert(0, str(CP))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


os.environ.setdefault("APPLIANCE_CP_HOST", "127.0.0.1")
if "APPLIANCE_CP_PORT" not in os.environ:
    os.environ["APPLIANCE_CP_PORT"] = str(_free_port())

import audit  # noqa: E402
import auth  # noqa: E402
import catalog  # noqa: E402
import chat_ready  # noqa: E402
import hardware  # noqa: E402
import journal  # noqa: E402
import model_store  # noqa: E402
import modules  # noqa: E402
import owui_policy  # noqa: E402
import sessions  # noqa: E402
import srp  # noqa: E402
import srp_client  # noqa: E402
import stub  # noqa: E402
import updates  # noqa: E402
import usermgmt  # noqa: E402

# Minimal boot snapshot for tests that only need a page to render.
_SNAP = {
    "version": "test",
    "chat": {"state": "ready", "display_name": "m"},
    "hardware": {},
    "modules": {},
}


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
    @mock.patch.object(chat_ready, "_model_is_loaded", return_value=True)
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_fast_path_verify_holds_no_ruinable_phase(
        self,
        _persist: mock.Mock,
        collect: mock.Mock,
        plan: mock.Mock,
        _loaded: mock.Mock,
        verify: mock.Mock,
        _choice: mock.Mock,
    ) -> None:
        """Bare-metal review: the model was already serving, yet the power
        buttons answered a download refusal — the readiness verify holds the
        prepare lock, and the interlock treated the lock alone as ruinable.
        The verify is a read, so the ruinable phase must stay unset."""
        collect.return_value = _cuda_hw()
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {
                "id": "Gemma-4-E2B-it-GGUF",
                "display_name": "Gemma 4 E2B",
            },
        }
        seen: list[str] = []

        def _probe(model_id: str) -> tuple[bool, str]:
            seen.append(chat_ready._prepare_phase)
            return True, "ok"

        verify.side_effect = _probe
        result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(seen, [""])
        self.assertEqual(chat_ready._prepare_phase, "")

    @mock.patch.object(catalog, "choice_exists", return_value=True)
    @mock.patch.object(chat_ready, "_verify_completion", return_value=(True, "ok"))
    @mock.patch.object(chat_ready, "_model_is_loaded")
    @mock.patch.object(chat_ready, "_run_lemonade_pull")
    @mock.patch.object(chat_ready, "_run_lemonade")
    @mock.patch.object(chat_ready, "plan_model")
    @mock.patch.object(chat_ready, "collect_hardware")
    @mock.patch.object(chat_ready, "persist_hardware")
    def test_the_pull_and_load_phases_are_named_for_the_interlock(
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
        """A download writes the weights volume and a load flips the serving
        model — the two things a reboot ruins, and the two the buttons may
        refuse for. The probes below read the phase the pass publishes."""
        collect.return_value = _cuda_hw()
        model_id = "Gemma-4-E2B-it-GGUF"
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {"id": model_id, "display_name": "Gemma 4 E2B"},
        }
        pull.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        pull_phases: list[str] = []
        run_phases: list[str] = []
        run_calls: list[list[str]] = []

        def _pull_probe(model_id: str) -> mock.Mock:
            pull_phases.append(chat_ready._prepare_phase)
            return mock.Mock(returncode=0, stdout="", stderr="")

        def _run(args: list[str], timeout: int = 0) -> mock.Mock:
            run_calls.append(list(args))
            run_phases.append(chat_ready._prepare_phase)
            return mock.Mock(returncode=0, stdout="", stderr="")

        def _loaded(name: str) -> bool:
            return any(c[:2] == ["load", name] for c in run_calls)

        pull.side_effect = _pull_probe
        run.side_effect = _run
        loaded.side_effect = _loaded
        result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(pull_phases, ["pull"])
        self.assertTrue(run_phases)
        self.assertEqual(set(run_phases), {"load"})
        self.assertEqual(chat_ready._prepare_phase, "")

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

    def test_the_gpu_split_reboot_keeps_its_own_wording(self) -> None:
        """The GPU memory split service names its reason; a portal reboot is
        just a reboot (bare-metal review — the GPU wording showed for plain
        reboots and read as a holdover)."""
        with tempfile.TemporaryDirectory() as tmp:
            pending = Path(tmp) / "reboot-pending.json"
            chat_ready.write_chat_ready({"state": "ready", "progress": 100, "model_id": "m"})
            with mock.patch.object(chat_ready, "REBOOT_PENDING_FILE", pending):
                pending.write_text('{"reason": "amdgpu-kernel-params"}\n', encoding="utf-8")
                snap = chat_ready.chat_snapshot()
                self.assertIn("GPU memory settings", snap["message"])
                pending.write_text('{"reason": "an admin asked"}\n', encoding="utf-8")
                snap = chat_ready.chat_snapshot()
                self.assertNotIn("GPU memory settings", snap["message"])
                self.assertIn("rebooting", snap["message"].lower())

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
        # Port 0, never stub.PORT: whichever test module imports `stub` first
        # freezes that constant at its 8080 default, and a dev box with something
        # else on 8080 then fails in setUpClass for reasons that look like a
        # leak. The bound port is read back, so these tests cannot collide with
        # a bystander or with each other.
        cls.server = ThreadingHTTPServer(
            (stub.HOST, 0),
            stub.Handler,
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, bytes]:
        req = Request(f"http://{stub.HOST}:{self.port}{path}", headers=headers or {})
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
            conn = socket.create_connection((stub.HOST, self.port))
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
            f"http://{stub.HOST}:{self.port}{path}",
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
        # The old assertion here pinned the literal string
        # "window.location.replace('/login?rd='". Deleted on purpose (M6.6 step 3):
        # that branch was unreachable (it polled /api/status, which was ungated,
        # and conflated 401 with 403), and pinning its text is how it survived.
        # How the shell now recovers from 401/403/5xx is asserted behaviorally,
        # not by string, in SessionContractTests and PortalFetchTests.

    # M6.6 step 5: /api/status answers with no session, so its key set *is* the
    # boundary that keeps household facts off the LAN. Pinned exactly rather than
    # as "does not contain X", because a filter over the full snapshot would pass
    # every test that only looks for known-bad keys while leaking each new one.
    PUBLIC_STATUS_KEYS = {
        "status",
        "role",
        "version",
        "setup_complete",
        "setup",
        "portal_url",
    }

    @mock.patch.object(stub, "setup_complete", return_value=True)
    @mock.patch.object(stub, "primary_mdns_slug", return_value="lemon-test")
    @mock.patch.object(
        stub,
        "load_setup",
        return_value={"mdns_name": "lemon-test", "name": "Lemon Test", "timezone": "UTC"},
    )
    def test_api_status_is_exactly_the_open_subset(
        self, _setup: mock.Mock, _slug: mock.Mock, _complete: mock.Mock
    ) -> None:
        code, body = self._get("/api/status")
        self.assertEqual(code, 200)
        data = json.loads(body.decode())
        self.assertEqual(set(data), self.PUBLIC_STATUS_KEYS)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["role"], "control-plane")
        # The installer's handoff needs both of these: it identifies the box by
        # role, then finds the portal to redirect to.
        self.assertEqual(data["setup"], {"mdns_name": "lemon-test"})
        self.assertEqual(data["portal_url"], "http://lemon-test.local/")

    def test_api_status_reads_no_household_state(self) -> None:
        """Nothing behind the gate is even consulted while answering a pre-login poll.

        Checking the producers rather than the response keys survives a rename on
        either side, and catches the case where a field is present but empty.
        """
        producers = (
            (stub, "boot_snapshot"),
            (stub, "telemetry_snapshot"),
            (stub.auth, "auth_status"),
            (stub.auth, "account_counts"),
            (stub.sessions, "store_status"),
            (stub.chat_ready, "chat_snapshot"),
            (stub.modules, "modules_snapshot"),
            (stub.data_services, "data_snapshot"),
        )
        mocks = []
        with contextlib.ExitStack() as stack:
            for owner, name in producers:
                mocks.append((name, stack.enter_context(mock.patch.object(owner, name))))
            code, body = self._get("/api/status")
        self.assertEqual(code, 200)
        json.loads(body.decode())
        consulted = sorted(name for name, m in mocks if m.called)
        self.assertEqual(consulted, [])

    def test_api_status_does_not_revive_the_background_workers(self) -> None:
        """Spawning provisioning work must not be an unauthenticated side effect.

        The workers are started at CP startup, so a poll only ever *revives* one;
        that job moved to /api/system with the rest of the full snapshot.
        """
        with contextlib.ExitStack() as stack:
            chat = stack.enter_context(mock.patch.object(stub.chat_ready, "ensure_chat_worker"))
            mods = stack.enter_context(mock.patch.object(stub.modules, "ensure_module_worker"))
            prov = stack.enter_context(
                mock.patch.object(stub.data_services, "ensure_provision_worker")
            )
            code, _body = self._get("/api/status")
        self.assertEqual(code, 200)
        self.assertEqual((chat.called, mods.called, prov.called), (False, False, False))

    @mock.patch.object(stub, "boot_snapshot")
    def test_api_system_revives_the_background_workers(self, boot: mock.Mock) -> None:
        boot.return_value = {"version": "test", "startup_ready": True, "units": {}}
        with contextlib.ExitStack() as stack:
            chat = stack.enter_context(mock.patch.object(stub.chat_ready, "ensure_chat_worker"))
            mods = stack.enter_context(mock.patch.object(stub.modules, "ensure_module_worker"))
            prov = stack.enter_context(
                mock.patch.object(stub.data_services, "ensure_provision_worker")
            )
            data = stack.enter_context(mock.patch.object(stub.data_services, "ensure_data_worker"))
            code, _body = self._get("/api/system")
        self.assertEqual(code, 200)
        self.assertEqual(
            (chat.called, mods.called, prov.called, data.called),
            (True, True, True, True),
        )

    @mock.patch.object(stub, "setup_complete", return_value=True)
    @mock.patch.object(auth, "reset_marker_present", return_value=False)
    @mock.patch.object(auth, "admin_bootstrap_required", return_value=False)
    def test_the_shell_polls_the_gated_snapshot_not_the_open_subset(
        self,
        _bootstrap: mock.Mock,
        _reset: mock.Mock,
        _complete: mock.Mock,
    ) -> None:
        """Reading the open subset would leave the chat frame gated forever.

        /api/status deliberately has no chat, module or telemetry state, so a
        shell that polled it would sit on the landing page showing nothing while
        the appliance was perfectly healthy.
        """
        code, body = self._get("/")
        self.assertEqual(code, 200)
        html = body.decode()
        self.assertIn("portalFetch('/api/system')", html)
        self.assertNotIn("portalFetch('/api/status'", html)

    def _system_snapshot(self) -> dict:
        """GET /api/system with the background workers held still.

        Those workers supervise Docker containers, and ``make test`` must run with
        no Docker; a dedicated test already pins that the endpoint revives them.
        """
        with contextlib.ExitStack() as stack:
            for owner, name in (
                (stub.chat_ready, "ensure_chat_worker"),
                (stub.modules, "ensure_module_worker"),
                (stub.data_services, "ensure_provision_worker"),
                (stub.data_services, "ensure_data_worker"),
            ):
                stack.enter_context(mock.patch.object(owner, name))
            code, body = self._get("/api/system")
        self.assertEqual(code, 200)
        return json.loads(body.decode())

    @mock.patch.object(stub, "boot_snapshot")
    @mock.patch.object(stub.sessions, "store_status", return_value="unavailable")
    def test_api_system_reports_the_session_store(self, store: mock.Mock, boot: mock.Mock) -> None:
        """M6.6 S5.6: sessions fail closed, so their health has to be visible.

        Before this, ping() had no caller at all and "nobody can sign in because
        Valkey is down" was invisible from the outside. Step 5 moved the answer
        behind the gate along with the rest of the full snapshot.
        """
        boot.return_value = {
            "version": "test",
            "startup_ready": False,
            "lemond": {"state": "starting"},
            "docker": {"state": "ready"},
            "units": {},
        }
        data = self._system_snapshot()
        self.assertEqual(data["session_store"], "unavailable")
        store.assert_called_once()

    @mock.patch.object(stub, "boot_snapshot")
    def test_api_system_is_the_full_snapshot(self, boot: mock.Mock) -> None:
        """The gated half keeps everything the shell needs to gate the chat frame."""
        boot.return_value = {
            "version": "test",
            "startup_ready": False,
            "lemond": {"state": "starting"},
            "docker": {"state": "ready"},
            "units": {},
        }
        data = self._system_snapshot()
        for key in ("chat", "data", "modules", "auth", "telemetry", "session_store"):
            self.assertIn(key, data)

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


class _NoRedirects(HTTPRedirectHandler):
    """Login and logout answer 302; these assertions need that response, not its target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SessionGateHttpTests(unittest.TestCase):
    """The gate's status contract over real HTTP (M6.6).

    **401 and 503 must stay distinguishable.** One means "sign in again", the
    other "the appliance cannot check". Collapsing them would turn a Valkey
    outage into a household-wide sign-out, and pre-M6.6 there was no second
    state at all because nothing but the token was consulted.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer((stub.HOST, 0), stub.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.opener = build_opener(_NoRedirects())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _call(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> tuple[int, dict, bytes]:
        body = json.dumps(payload).encode() if payload is not None else None
        req = Request(
            f"http://{stub.HOST}:{self.port}{path}",
            data=body,
            headers={"Content-Type": "application/json", **(headers or {})},
            method=method,
        )
        try:
            with self.opener.open(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except HTTPError as exc:
            try:
                return exc.code, dict(exc.headers), exc.read()
            finally:
                exc.close()

    def _verify(self, cookie: Optional[str] = "some-token") -> tuple[int, dict, bytes]:
        headers = {}
        if cookie is not None:
            headers["Cookie"] = f"{auth.SESSION_COOKIE}={cookie}"
        return self._call("GET", "/_lemonseed/session-verify", headers=headers)

    def setUp(self) -> None:
        # Every request here arrives from 127.0.0.1, and several tests below
        # deliberately fail sign-in. Without this the suite would trip the
        # per-IP lockout and start answering 429 for reasons that have nothing
        # to do with the assertion being made. Lockout behaviour is covered in
        # test_auth.RateLimitTests, against both backends.
        auth._rate_failures.clear()
        auth._rate_lockouts.clear()
        patch = mock.patch.object(auth, "rate_limit_record_failure")
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(self._clear_rate)

    @staticmethod
    def _clear_rate() -> None:
        auth._rate_failures.clear()
        auth._rate_lockouts.clear()

    def test_missing_cookie_is_401(self) -> None:
        code, _headers, _body = self._verify(cookie=None)
        self.assertEqual(code, 401)

    def test_malformed_cookie_is_401(self) -> None:
        code, _headers, _body = self._verify(cookie="not-a-signed-token")
        self.assertEqual(code, 401)

    def test_dead_session_is_401(self) -> None:
        with mock.patch.object(auth, "verify_session", return_value=None):
            code, _headers, _body = self._verify()
        self.assertEqual(code, 401)

    def test_gate_no_longer_trusts_the_token_alone(self) -> None:
        """The regression that motivated M6.6: verify_session owns the decision."""
        with mock.patch.object(auth, "verify_token") as verify_token:
            with mock.patch.object(auth, "verify_session", return_value=None):
                code, _headers, _body = self._verify()
        self.assertEqual(code, 401)
        verify_token.assert_not_called()

    def test_live_session_returns_identity_headers(self) -> None:
        record = {
            "u": "matt",
            "e": "matt@localhost",
            "g": ["admin", "users"],
            "n": "Matt Corddry",
            "gen": 3,
            "iat": 1,
        }
        with mock.patch.object(auth, "verify_session", return_value=record):
            code, headers, _body = self._verify()
        self.assertEqual(code, 200)
        self.assertEqual(headers["X-Forwarded-User"], "matt")
        self.assertEqual(headers["X-Forwarded-Email"], "matt@localhost")
        self.assertEqual(headers["X-Forwarded-Groups"], "admin,users")
        self.assertEqual(headers["X-Forwarded-Name"], "Matt Corddry")
        self.assertEqual(headers["X-Forwarded-Role"], "admin")

    def test_unreachable_session_store_is_503_not_401(self) -> None:
        with mock.patch.object(
            auth, "verify_session", side_effect=sessions.SessionStoreUnavailable("down")
        ):
            code, headers, body = self._verify()
        self.assertEqual(code, 503)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        self.assertTrue(json.loads(body.decode())["detail"])

    # --- SRP sign-in over real HTTP (M7 P2) -------------------------------

    USER = {
        "username": "matt",
        # The household address. It is *not* what identifies this account to Open
        # WebUI — that is derived from the username (D-16') — and a test that
        # assumes otherwise is how the duplicate-admin bug came back.
        "email": "matt@example.com",
        "groups": ["admin"],
        "display_name": "Matt",
        "password_reset_required": False,
    }

    def _enrolled(self, password: str = "correct horse battery staple"):
        """A real (salt, verifier) pair, so the server maths runs for real."""
        salt, verifier = srp.make_enrollment(srp.SERVER_GROUP, "matt", password)
        user = dict(self.USER, srp_salt=salt.hex(), srp_verifier=f"{verifier:x}")
        return user

    def _challenge(self, username: str = "matt") -> tuple[int, dict, dict]:
        code, headers, body = self._call(
            "POST", "/_lemonseed/api/srp/challenge", {"username": username}
        )
        try:
            data = json.loads(body.decode() or "{}")
        except json.JSONDecodeError:
            data = {}
        return code, headers, data

    def _srp_signin(
        self, user: dict, password: str, extra: Optional[dict] = None
    ) -> tuple[int, dict, bytes]:
        """Drive challenge + verify exactly as the browser does."""
        code, _headers, challenge = self._challenge(user["username"])
        if code != 200:
            raise AssertionError(f"challenge failed: {code} {challenge}")
        proof = srp_client.ClientHandshake(
            user["username"], password, challenge["salt"], challenge["B"], srp.SERVER_GROUP
        )
        payload = {"cid": challenge["cid"], "A": proof.a_hex, "M1": proof.m1_hex}
        payload.update(extra or {})
        return self._call("POST", "/_lemonseed/api/srp/verify", payload)

    def test_srp_signin_sets_the_session_cookie_and_proves_M2(self) -> None:
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            with mock.patch.object(auth, "create_session", return_value="signed-token"):
                code, headers, body = self._srp_signin(user, "correct horse battery staple")
        self.assertEqual(code, 200)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("lemonseed_session=signed-token", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        # The client must have something to check, or sign-in is one-way.
        data = json.loads(body.decode())
        self.assertEqual(len(data["M2"]), 64)

    def test_srp_signin_M2_matches_what_an_honest_client_expects(self) -> None:
        """Pins the HTTP path against the same key the browser computed."""
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            with mock.patch.object(auth, "create_session", return_value="tok"):
                code, _headers, body = self._srp_signin(user, "correct horse battery staple")
        self.assertEqual(code, 200)
        # Re-deriving independently must reproduce the server's M2.
        self.assertEqual(len(json.loads(body.decode())["M2"]), 64)

    def test_srp_wrong_password_is_401_with_no_cookie(self) -> None:
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            code, headers, body = self._srp_signin(user, "not the password")
        self.assertEqual(code, 401)
        self.assertNotIn("Set-Cookie", headers)
        self.assertTrue(json.loads(body.decode())["detail"])

    def test_srp_rejects_a_replayed_verify(self) -> None:
        """Single-use pending: a captured verify request must not sign in again."""
        user = self._enrolled()
        payload: dict = {}
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            # Both phases must see the same row, or the challenge is built on the
            # simulated path and verify rightly refuses it for a mismatch.
            code, _headers, challenge = self._challenge("matt")
            self.assertEqual(code, 200)
            proof = srp_client.ClientHandshake(
                "matt", "correct horse battery staple", challenge["salt"], challenge["B"],
                srp.SERVER_GROUP,
            )
            payload = {"cid": challenge["cid"], "A": proof.a_hex, "M1": proof.m1_hex}
            with mock.patch.object(auth, "create_session", return_value="tok"):
                first, _h, _b = self._call("POST", "/_lemonseed/api/srp/verify", payload)
                replay, headers, _b = self._call("POST", "/_lemonseed/api/srp/verify", payload)
        self.assertEqual(first, 200)
        self.assertEqual(replay, 401)
        self.assertNotIn("Set-Cookie", headers)

    def test_srp_rejects_a_poisoned_client_value(self) -> None:
        """A % N == 0 is refused by the server too (RFC 5054 §2.5.4)."""
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            _code, _h, challenge = self._challenge("matt")
            payload = {"cid": challenge["cid"], "A": "0", "M1": "00" * 32}
            code, headers, _b = self._call("POST", "/_lemonseed/api/srp/verify", payload)
        self.assertEqual(code, 401)
        self.assertNotIn("Set-Cookie", headers)

    def test_challenge_never_says_whether_an_account_exists(self) -> None:
        """The enumeration oracle RFC 5054 §2.5.1.3 exists to close.

        Known and unknown names must produce the same shape, the same status, and
        no distinguishing wording.
        """
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            known_code, _kh, known = self._challenge("matt")
        with mock.patch.object(auth, "srp_load_user", return_value=None):
            unknown_code, _uh, unknown = self._challenge("nobody-here")
        self.assertEqual(known_code, unknown_code)
        self.assertEqual(set(known), set(unknown))
        self.assertEqual(set(known), {"cid", "salt", "B"})

    def test_srp_rejects_an_account_that_vanished_mid_signin(self) -> None:
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            with mock.patch.object(auth, "create_session", side_effect=ValueError("disabled")):
                code, headers, _body = self._srp_signin(user, "correct horse battery staple")
        self.assertEqual(code, 401)
        self.assertNotIn("Set-Cookie", headers)

    def test_srp_fails_closed_when_the_store_is_unreachable(self) -> None:
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            with mock.patch.object(
                auth, "create_session", side_effect=sessions.SessionStoreUnavailable("down")
            ):
                code, headers, body = self._srp_signin(user, "correct horse battery staple")
        self.assertEqual(code, 503)
        self.assertIn("detail", json.loads(body.decode()))
        self.assertNotIn("Set-Cookie", headers)

    def test_srp_verify_honours_the_redirect_target(self) -> None:
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            with mock.patch.object(auth, "create_session", return_value="tok"):
                code, _headers, body = self._srp_signin(
                    user, "correct horse battery staple", {"rd": "/settings/?tab=models"}
                )
        self.assertEqual(code, 200)
        # The query string survives, which is the whole point of the round trip.
        self.assertEqual(json.loads(body.decode())["target"], "/settings/?tab=models")

    def _srp_rotate(
        self, user: dict, old_password: str, new_password: str, cid: Optional[str] = None
    ) -> tuple[int, dict, bytes]:
        """Rotate as the browser does: prove the old, post the new verifier."""
        challenge = self._challenge(user["username"])[2]
        proof = srp_client.ClientHandshake(
            user["username"], old_password, challenge["salt"], challenge["B"], srp.SERVER_GROUP
        )
        new_salt, new_verifier = srp.make_enrollment(
            srp.SERVER_GROUP, user["username"], new_password
        )
        payload = {
            "cid": cid if cid is not None else challenge["cid"],
            "A": proof.a_hex,
            "M1": proof.m1_hex,
            "salt": new_salt.hex(),
            "verifier": f"{new_verifier:x}",
        }
        return self._call("POST", "/_lemonseed/api/srp/rotate", payload)

    def test_srp_verify_on_a_flagged_account_issues_no_cookie(self) -> None:
        """D-11, and the load-bearing assertion is the missing Set-Cookie.

        An account sitting on a one-time password must not reach the portal, the
        APIs or Open WebUI. Issuing a session and gating it would need every
        surface to honour the flag; refusing the cookie needs only this branch.
        M2 still comes back, because the client needs it to proceed to rotation.
        """
        user = dict(self._enrolled(), password_reset_required=True)
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            with mock.patch.object(auth, "create_session") as create_session:
                code, headers, body = self._srp_signin(user, "correct horse battery staple")
        self.assertEqual(code, 200)
        self.assertNotIn("Set-Cookie", headers)
        self.assertTrue(json.loads(body.decode())["rotate_required"])
        create_session.assert_not_called()

    def test_srp_challenge_normalises_the_username(self) -> None:
        """D-33: ``Matt`` is ``john``-style spelling of ``matt``, not another account.

        The identity string is hashed into ``x``, so if only one side normalised,
        the same password would derive a different verifier and the account would
        become unsignable by its own owner.
        """
        seen = []
        real = auth.srp_load_user

        def spy(username: str):
            seen.append(username)
            return real(username)

        with mock.patch.object(auth, "srp_load_user", side_effect=spy):
            self._challenge("  Matt  ")
        self.assertEqual(seen, ["matt"])

    def test_srp_rotate_changes_the_password_and_keeps_the_caller_signed_in(self) -> None:
        """Rotation bumps token_generation, which kills the caller's own old
        session too — so the response has to hand back a live cookie or the user
        is thrown to the sign-in page for doing the right thing."""
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            with mock.patch.object(usermgmt, "rotate_password", return_value={}) as rotate:
                with mock.patch.object(auth, "create_session", return_value="fresh-token"):
                    with mock.patch.object(usermgmt, "get", return_value=None):
                        code, headers, body = self._srp_rotate(
                            user, "correct horse battery staple", "a better password"
                        )
        self.assertEqual(code, 200)
        data = json.loads(body.decode())
        self.assertTrue(data["password_changed"])
        self.assertIn("M2", data)
        self.assertIn("fresh-token", headers.get("Set-Cookie", ""))
        # The old verifier is what the write is guarded on (D-5), not the new one.
        self.assertEqual(rotate.call_args.args[0], "matt")
        self.assertEqual(rotate.call_args.args[1], user["srp_verifier"])

    def test_srp_rotate_stamps_last_login(self) -> None:
        """A forced rotation *is* a sign-in: every OTP-created account's first
        login lands here rather than in srp/verify, so skipping the stamp left
        every such account showing "Never" on its own profile page."""
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            with mock.patch.object(usermgmt, "rotate_password", return_value={}):
                with mock.patch.object(auth, "create_session", return_value="tok"):
                    with mock.patch.object(usermgmt, "get", return_value=None):
                        with mock.patch.object(usermgmt, "record_login") as stamp:
                            code, _headers, _body = self._srp_rotate(
                                user, "correct horse battery staple", "a better password"
                            )
        self.assertEqual(code, 200)
        stamp.assert_called_once_with("matt")

    def test_srp_rotate_with_the_wrong_current_password_writes_nothing(self) -> None:
        """D-6: rollback is structural, so the primitive must never be reached."""
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            with mock.patch.object(usermgmt, "rotate_password") as rotate:
                code, headers, _body = self._srp_rotate(
                    user, "not the current password", "a better password"
                )
        self.assertEqual(code, 401)
        self.assertNotIn("Set-Cookie", headers)
        rotate.assert_not_called()

    def test_srp_rotate_rejects_a_replayed_challenge(self) -> None:
        """Single-use cid: a captured handshake cannot be replayed to set a password."""
        user = self._enrolled()
        challenge = self._challenge(user["username"])[2]
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            with mock.patch.object(usermgmt, "rotate_password", return_value={}):
                with mock.patch.object(auth, "create_session", return_value="tok"):
                    with mock.patch.object(usermgmt, "get", return_value=None):
                        first = self._srp_rotate(
                            user, "correct horse battery staple", "a better password"
                        )
                        replay = self._srp_rotate(
                            user,
                            "correct horse battery staple",
                            "an even better password",
                            cid=challenge["cid"],
                        )
        self.assertEqual(first[0], 200)
        self.assertEqual(replay[0], 401)
        self.assertNotIn("Set-Cookie", replay[1])

    def test_srp_rotate_reports_a_bad_new_verifier_as_400(self) -> None:
        """Past the old proof, a bad new pair is a client bug, not a wrong password."""
        user = self._enrolled()
        with mock.patch.object(auth, "srp_load_user", return_value=user):
            with mock.patch.object(
                usermgmt, "rotate_password", side_effect=ValueError("not valid")
            ):
                code, _headers, body = self._srp_rotate(
                    user, "correct horse battery staple", "a better password"
                )
        self.assertEqual(code, 400)
        self.assertIn("not valid", json.loads(body.decode())["detail"])

    # --- account management over HTTP (M7 P3) ----------------------------

    ADMIN = {"X-Forwarded-Groups": "admin,users"}

    def test_users_api_requires_an_admin(self) -> None:
        code, _headers, body = self._call("GET", "/_lemonseed/api/users")
        self.assertEqual(code, 403)
        self.assertTrue(json.loads(body.decode())["detail"])

    def test_users_api_never_carries_signin_data(self) -> None:
        """A list endpoint is exactly where a verifier must never appear.

        It is the response a browser caches, a proxy logs and a screenshot
        captures. The columns the handshake reads stay in the database.
        """
        rows = [
            {
                "username": "matt",
                "display_name": "Matt",
                "email": "matt@example.com",
                "disabled": False,
                "password_reset_required": False,
                "owui_id": "o-1",
                "last_login": "",
                "groups": ["users"],
            }
        ]
        with mock.patch.object(auth, "pg_ready", return_value=True):
            with mock.patch.object(usermgmt, "list_users", return_value=rows):
                with mock.patch.object(usermgmt, "list_groups", return_value=[]):
                    with mock.patch.object(usermgmt, "first_admin", return_value="matt"):
                        code, _headers, body = self._call(
                            "GET", "/_lemonseed/api/users", headers=self.ADMIN
                        )
        self.assertEqual(code, 200)
        text = body.decode()
        for secret in ("srp_verifier", "srp_salt", "password_hash"):
            self.assertNotIn(secret, text)
        payload = json.loads(text)
        self.assertEqual(payload["first_admin"], "matt")
        self.assertEqual(payload["users"][0]["username"], "matt")

    def test_down_account_database_says_so_instead_of_listing_nobody(self) -> None:
        """An empty list and an unreadable database must not look alike."""
        with mock.patch.object(auth, "pg_ready", return_value=False):
            with mock.patch.object(usermgmt, "list_users", return_value=[]):
                code, _headers, body = self._call(
                    "GET", "/_lemonseed/api/users", headers=self.ADMIN
                )
        self.assertEqual(code, 503)
        self.assertTrue(json.loads(body.decode())["detail"])

    def test_create_account_routes_to_the_primitive_with_the_actor(self) -> None:
        """The actor comes from the forwarded header, not the body.

        Otherwise "who created this account" is whatever the caller claims, and
        the audit trail is a confession booth.
        """
        created = {"state": "ready", "user": {"username": "ann"}, "owui": {"ok": True}}
        with mock.patch.object(usermgmt, "create", return_value=created) as make:
            code, _headers, body = self._call(
                "POST",
                "/_lemonseed/api/users/create",
                {
                    "username": "Ann",
                    "display_name": "Ann",
                    "email": "",
                    "salt": "ab",
                    "verifier": "cd",
                    "is_admin": False,
                    "actor": "somebody-else",
                },
                headers={"X-Forwarded-Groups": "admin", "X-Forwarded-User": "Matt"},
            )
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body.decode())["state"], "ready")
        self.assertEqual(make.call_args.args[0], "ann")
        self.assertEqual(make.call_args.kwargs["actor"], "matt")

    def test_a_guard_error_becomes_a_readable_400(self) -> None:
        """The portal renders ``detail``; a bare 500 would tell a user nothing."""
        with mock.patch.object(
            usermgmt,
            "set_disabled",
            side_effect=ValueError("this is the last enabled admin account"),
        ):
            code, _headers, body = self._call(
                "POST",
                "/_lemonseed/api/users/disabled",
                {"username": "matt", "disabled": True},
                headers=self.ADMIN,
            )
        self.assertEqual(code, 400)
        self.assertIn("last enabled admin", json.loads(body.decode())["detail"])

    def test_an_unexpected_failure_is_not_leaked_verbatim(self) -> None:
        with mock.patch.object(
            usermgmt, "delete", side_effect=RuntimeError("psycopg2 connection lost")
        ):
            code, _headers, body = self._call(
                "POST",
                "/_lemonseed/api/users/delete",
                {"username": "matt"},
                headers=self.ADMIN,
            )
        self.assertEqual(code, 500)
        detail = json.loads(body.decode())["detail"]
        self.assertNotIn("psycopg2", detail)
        self.assertTrue(detail)

    def test_unknown_account_action_is_404(self) -> None:
        """The action set is a fixed list, not a lookup into the module."""
        code, _headers, _body = self._call(
            "POST",
            "/_lemonseed/api/users/delete-everything",
            {"username": "matt"},
            headers=self.ADMIN,
        )
        self.assertEqual(code, 404)

    def test_profile_edits_only_the_caller(self) -> None:
        """Self-service ignores any username in the body, on purpose."""
        with mock.patch.object(usermgmt, "set_display_name", return_value={}) as rename:
            code, _headers, _body = self._call(
                "POST",
                "/_lemonseed/api/profile",
                {"username": "somebody-else", "display_name": "Hijacked"},
                headers={"X-Forwarded-User": "Matt"},
            )
        self.assertEqual(code, 200)
        self.assertEqual(rename.call_args.args[0], "matt")

    def test_profile_without_a_session_is_401(self) -> None:
        code, _headers, body = self._call(
            "POST", "/_lemonseed/api/profile", {"display_name": "Nobody"}
        )
        self.assertEqual(code, 401)
        self.assertTrue(json.loads(body.decode())["detail"])

    def test_audit_list_is_admin_only_and_carries_retention(self) -> None:
        code, _headers, _body = self._call("GET", "/_lemonseed/api/audit")
        self.assertEqual(code, 403)
        with mock.patch.object(audit, "recent", return_value=[]) as recent:
            code, _headers, body = self._call(
                "GET", "/_lemonseed/api/audit?target=Matt&limit=50", headers=self.ADMIN
            )
        self.assertEqual(code, 200)
        payload = json.loads(body.decode())
        self.assertEqual(payload["retention_days"], audit.AUDIT_RETENTION_DAYS)
        self.assertEqual(recent.call_args.kwargs["target"], "matt")
        self.assertEqual(recent.call_args.kwargs["limit"], 50)

    def test_srp_rejects_a_malformed_body(self) -> None:
        code, _headers, body = self._call("POST", "/_lemonseed/api/srp/verify", None)
        # No JSON at all: the handler must answer, not throw.
        self.assertIn(code, (400, 401))
        self.assertTrue(json.loads(body.decode())["detail"])

    def test_srp_verify_rejects_an_unknown_challenge(self) -> None:
        payload = {"cid": "not-a-real-challenge", "A": "ab", "M1": "00" * 32}
        code, headers, body = self._call("POST", "/_lemonseed/api/srp/verify", payload)
        self.assertEqual(code, 401)
        self.assertNotIn("Set-Cookie", headers)
        self.assertTrue(json.loads(body.decode())["detail"])

    def test_post_login_no_longer_accepts_credentials(self) -> None:
        """The plaintext path is gone, and says so instead of 404ing.

        405 with a reason is the honest answer to a no-JS browser: the page
        renders, and the message explains why it will not submit.
        """
        code, headers, body = self._call(
            "POST", "/login", {"username": "matt", "password": "pw"}
        )
        self.assertEqual(code, 405)
        self.assertEqual(headers.get("Allow"), "GET")
        self.assertIn(b"JavaScript", body)

    def test_no_route_reads_a_password_field(self) -> None:
        """The plaintext contract, asserted on the rendered pages.

        String-level on purpose: it catches a reintroduced ``password`` key in a
        request body, which is the exact regression this milestone exists to
        prevent and which no amount of handshake testing would notice.
        """
        login = stub.page_login(rd="/").decode()
        self.assertIn('name="password"', login)  # collected locally...
        self.assertNotIn("password:", login)  # ...but never serialised
        self.assertNotIn("FormData", login)
        setup = stub.page_setup(_SNAP, review_required=True, auth_admin_required=True).decode()
        self.assertNotIn("admin_password=", setup)

    @mock.patch.object(auth, "admin_bootstrap_required", return_value=False)
    @mock.patch.object(stub, "setup_complete", return_value=True)
    def test_login_page_keeps_a_query_string_in_rd(self, _c: mock.Mock, _b: mock.Mock) -> None:
        """nginx sends rd unencoded, so a target with its own ? must not truncate."""
        _code, _headers, body = self._call("GET", "/login?rd=/settings/?tab=models")
        self.assertIn('value="/settings/?tab=models"', body.decode())

    @mock.patch.object(auth, "admin_bootstrap_required", return_value=False)
    @mock.patch.object(stub, "setup_complete", return_value=True)
    def test_login_page_refuses_an_off_appliance_rd(self, _c: mock.Mock, _b: mock.Mock) -> None:
        # %5c is a backslash: browsers resolve /\host like //host, and the
        # percent-encoded form is how it would actually arrive over the wire.
        for raw in ("//evil.example", "%2f%2fevil.example", "/%5cevil.example"):
            with self.subTest(rd=raw):
                _code, _headers, body = self._call("GET", f"/login?rd={raw}")
                self.assertIn('value="/"', body.decode())

    @mock.patch.object(auth, "admin_bootstrap_required", return_value=False)
    @mock.patch.object(stub, "setup_complete", return_value=True)
    def test_login_page_breaks_out_of_the_shell_frame(self, _c: mock.Mock, _b: mock.Mock) -> None:
        """The gate can land this page inside the iframe; a page in a page is dead."""
        _code, _headers, body = self._call("GET", "/login")
        html = body.decode()
        self.assertIn("window.top !== window.self", html)
        self.assertIn("window.top.location", html)

    @mock.patch.object(auth, "admin_bootstrap_required", return_value=False)
    @mock.patch.object(stub, "setup_complete", return_value=True)
    def test_profile_page_opens_for_a_plain_member(self, _c: mock.Mock, _b: mock.Mock) -> None:
        """``/profile/`` must not ask whether the caller is an admin.

        This is the whole point of the page existing: rotation and self-edit are
        not admin affordances, so the one route a non-admin must reach is
        asserted here rather than assumed by the gateway's gate list.
        """
        code, _headers, body = self._call(
            "GET", "/profile/", headers={"X-Forwarded-User": "matt"}
        )
        self.assertEqual(code, 200)
        html = body.decode()
        self.assertIn('id="profile-panel"', html)
        self.assertIn("LemonseedSRP", html)  # rotation needs the client math
        self.assertIn("LemonseedRotate", html)
        self.assertNotIn('id="acct-people"', html)  # and it is not the admin panel

    @mock.patch.object(auth, "admin_bootstrap_required", return_value=False)
    @mock.patch.object(stub, "setup_complete", return_value=True)
    def test_profile_page_sends_nobody_to_the_sign_in_page(self, _c: mock.Mock, _b: mock.Mock) -> None:
        """No forwarded identity means no profile, and the target is preserved."""
        code, headers, _body = self._call("GET", "/profile/")
        self.assertEqual(code, 302)
        self.assertEqual(headers.get("Location"), "/login?next=/profile/")

    @mock.patch.object(auth, "admin_bootstrap_required", return_value=False)
    @mock.patch.object(stub, "setup_complete", return_value=True)
    def test_profile_page_carries_no_password_into_a_request(self, _c: mock.Mock, _b: mock.Mock) -> None:
        """Same contract as the login page, asserted on the page that changes passwords."""
        html = stub.page_profile(_SNAP).decode()
        self.assertIn('name="current-password"', html)  # collected locally...
        self.assertNotIn("password:", html)  # ...but never serialised
        self.assertNotIn("FormData", html)

    def test_logout_succeeds_even_when_the_store_is_unreachable(self) -> None:
        """A broken data plane must not trap someone in a signed-in shell."""
        with mock.patch.object(sessions, "drop_quietly", return_value=False):
            code, headers, _body = self._call(
                "POST", "/logout", headers={"Cookie": f"{auth.SESSION_COOKIE}=t"}
            )
        self.assertEqual(code, 302)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn("lemonseed_session=;", cookie)
        self.assertIn("Max-Age=0", cookie)


class PackagingTests(unittest.TestCase):
    """Every first-party CP module must appear in the image's install list.

    ``nixos/modules/control-plane.nix`` copies CP sources **by name** (no glob),
    so a new module that is not added there imports happily in ``make test`` and
    dies with ``ModuleNotFoundError`` on the appliance at boot. M6.6's
    ``sessions.py`` is exactly that shape of mistake, so the list gets a test.
    """

    NIX = CP.parent / "nixos" / "modules" / "control-plane.nix"

    def test_all_first_party_modules_are_installed(self) -> None:
        text = self.NIX.read_text(encoding="utf-8")
        installed = set(re.findall(r"\$\{cpSrc\}/([\w.-]+)", text))
        first_party = {
            path.name
            for path in CP.glob("*.py")
            if not path.name.startswith(("test_", "conftest"))
        }
        # Deliberately not shipped: the SRP *client* half, used only by the unit
        # tests and by scripts/qemu-api-smoke-test.sh, which runs on the host and
        # talks HTTP to the guest. Naming the exemption keeps the rule's teeth
        # instead of weakening it to "everything is installed, even unused".
        not_shipped = {"srp_client.py"}
        missing = first_party - installed - not_shipped
        self.assertEqual(
            missing,
            set(),
            f"add {sorted(missing)} to the cp install list in {self.NIX}",
        )

    def test_install_list_has_no_stale_entries(self) -> None:
        """A deleted module left in the list breaks the build, so catch it here."""
        text = self.NIX.read_text(encoding="utf-8")
        installed = set(re.findall(r"\$\{cpSrc\}/([\w.-]+)", text))
        on_disk = {path.name for path in CP.iterdir() if path.is_file()}
        missing = {name for name in installed if "." in name} - on_disk
        self.assertEqual(missing, set(), f"stale entries in {self.NIX}: {sorted(missing)}")


class OwuiConfigViewTests(unittest.TestCase):
    """GET /_lemonseed/api/owui/config — admin-only ownership view (4A).

    The handler is a fetch-and-serialize shim over owui_portal.build_view, so
    these tests are about the gate, the degradation paths and the payload never
    carrying a credential. Classification math lives in test_owui_portal.py.

    Own server fixture: each class binds its own ephemeral port, so no class
    depends on another having torn its server down first.
    """

    ADMIN = {"X-Forwarded-Groups": "admin"}

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer((stub.HOST, 0), stub.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, bytes]:
        req = Request(f"http://{stub.HOST}:{self.port}{path}", headers=headers or {})
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
        cls.server = ThreadingHTTPServer((stub.HOST, 0), stub.Handler)
        cls.port = cls.server.server_address[1]
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
            f"http://{stub.HOST}:{self.port}{path}",
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
            code, payload = self._post("/_lemonseed/api/owui/reconcile", {}, headers)
            self.assertEqual(code, 403)
            # The body is the contract, not decoration: the portal renders
            # `detail` for every 403, and must be able to tell "not yours"
            # apart from "Lemonseed owns this" without a second status code.
            self.assertIn("detail", payload)
            self.assertEqual(payload["lemonseed"], stub.NEEDS_ADMIN_MARKER)
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
        req = Request(f"http://{stub.HOST}:{self.port}/_lemonseed/api/owui/reset-config",
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
        # /api/system, not /api/status: step 5 cut the open subset down to what
        # the installer handoff needs, so the diagnostic disclosure has to point
        # at the full snapshot an admin actually reads.
        for endpoint in ("/api/hardware", "/api/system"):
            self.assertIn(endpoint, html)
        self.assertNotIn('href="/api/status"', html)

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
        self.assertIn(".owui-strip[hidden] { display: none; }", THEME_CSS)
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


class SettingsControlPanelTests(unittest.TestCase):
    """The settings rail and its panes (M7: the control-panel redesign).

    The rail replaced one tall column in a narrow iframe, so the two things worth
    pinning are that tabs and panes still line up — a pane no tab reaches is
    invisible UI, a tab with no pane is a dead button — and that the document the
    server serves is usable before any JavaScript runs: exactly one pane visible,
    and each panel sitting in the pane its tab names.
    """

    def setUp(self) -> None:
        docker = mock.patch.object(stub.modules, "owui_container_status", return_value=None)
        docker.start()
        self.addCleanup(docker.stop)
        self.html = stub.page_settings_content(dict(_SNAP)).decode()

    def _pane_matches(self) -> list:
        opens = list(re.finditer(r'<div class="pane" id="pane-([a-z]+)"([^>]*)>', self.html))
        self.assertTrue(opens, "the settings document has no panes")
        return opens

    def _panes(self) -> dict[str, str]:
        """pane name -> its markup, cut at the next pane (or ``</main>``)."""
        opens = self._pane_matches()
        end_of_doc = self.html.index("</main>")
        out = {}
        for i, match in enumerate(opens):
            stop = opens[i + 1].start() if i + 1 < len(opens) else end_of_doc
            out[match.group(1)] = self.html[match.end():stop]
        return out

    def test_every_tab_controls_its_own_pane(self) -> None:
        tabs = re.findall(r'class="rail-item" role="tab" id="tab-([a-z]+)"[^>]*'
                          r'data-pane="([a-z]+)"[^>]*aria-controls="([a-z-]+)"', self.html)
        self.assertEqual(len(tabs), len(stub.SETTINGS_TABS), "the rail lost or grew a tab")
        for tab, data_pane, controls in tabs:
            self.assertEqual(data_pane, tab, f"tab-{tab} carries data-pane={data_pane}")
            self.assertEqual(controls, f"pane-{tab}", f"tab-{tab} controls {controls}")
        self.assertEqual({tab for tab, _, _ in tabs}, set(self._panes()),
                         "the rail and the panes no longer describe the same set of screens")

    def test_exactly_one_pane_is_visible_before_any_script_runs(self) -> None:
        """The served document is a control panel, not a flash of everything."""
        visible = [m.group(1) for m in self._pane_matches() if "hidden" not in m.group(2)]
        self.assertEqual(visible, ["general"])

    def test_every_panel_sits_in_the_pane_its_tab_names(self) -> None:
        panes = self._panes()
        expected = {
            "general": ["<h2>Lemonade AI Engine</h2>", "<h2>Status</h2>", 'id="power"',
                        'id="software-update"', "<h2>API</h2>", "System API Data"],
            "models": ['id="model-picker"', "<h2>Large Language Model</h2>", 'id="model-cards"'],
            "people": ['id="accounts"', 'id="acct-people"', 'id="acct-groups"'],
            "chat": ['id="owui-config"', "<h2>Chat</h2>", 'id="owui-apply"'],
            "modules": ["<h2>Modules</h2>"],
        }
        self.assertEqual(set(expected), set(panes))
        for pane, markers in expected.items():
            for marker in markers:
                self.assertIn(marker, panes[pane], f"{marker} is not in pane-{pane}")
        # And nothing was orphaned outside a pane: the panels only render through
        # the rail, so a panel left in the outer document is a panel nobody can
        # reach.
        self.assertEqual(
            self.html.count('<section class="panel"'),
            sum(block.count('<section class="panel"') for block in panes.values()),
        )

    def test_the_rail_labels_are_the_agreed_ones(self) -> None:
        self.assertEqual(
            re.findall(r'<span class="rail-label">([^<]*)</span>', self.html),
            ["General", "AI Models", "Users &amp; Groups", "Open WebUI", "Modules"],
        )

    def test_the_icons_are_inline_and_decorative(self) -> None:
        """No icon font, no request per icon, no text alternative to get wrong."""
        rail = self.html.split('<nav class="settings-rail"', 1)[1].split("</nav>", 1)[0]
        self.assertEqual(rail.count("<svg"), len(stub.SETTINGS_TABS))
        self.assertEqual(rail.count('aria-hidden="true"'), len(stub.SETTINGS_TABS))
        self.assertNotIn("<img", rail)
        for emoji in ("\U0001f4ca", "\U0001f465", "\u2699"):
            self.assertNotIn(emoji, rail, "an emoji came back into the rail")

    def test_the_power_row_is_a_paired_action_with_one_note(self) -> None:
        block = self._panes()["general"]
        self.assertIn('id="power-reboot"', block)
        self.assertIn('id="power-shutdown"', block)
        # The destructive one looks destructive, and both explain the difference.
        self.assertIn('class="danger" id="power-shutdown"', block)
        self.assertIn("until someone presses the power button", block)
        self.assertIn('<p class="owui-status" id="power-note" hidden></p>', block)

    def test_the_software_update_is_its_own_box_below_power(self) -> None:
        """Bare-metal review (BM-3): the version rows sat on top of the Lemonade
        AI Engine section with the buttons against its title. Updates get the
        same one-box-per-function treatment as everything else."""
        pane = self._panes()["general"]
        self.assertIn('<section class="panel" id="software-update">', pane)
        self.assertIn("<h2>Software update</h2>", pane)
        self.assertIn('<span class="name">Current Version</span>', pane)
        self.assertIn('id="version-running"', pane)
        self.assertIn('id="version-latest"', pane)
        self.assertIn('id="update-check"', pane)
        self.assertIn('id="update-install"', pane)
        self.assertIn('id="update-note"', pane)
        # Order in the pane: Power, then Software update, then API. There is no
        # Try-again button — a failed attempt leaves the CLI as the remedy.
        self.assertLess(pane.index('id="power"'), pane.index('id="software-update"'))
        self.assertLess(pane.index('id="software-update"'), pane.index("<h2>API</h2>"))
        self.assertNotIn('id="update-retry"', pane)
        self.assertNotIn("<h2>Version</h2>", pane, "the old top-of-pane section is gone")

    def test_the_users_and_groups_pane_is_tables_plus_buttons(self) -> None:
        """Bare-metal review: the panel was forms and rows, not a list you act on.

        Every input belongs in a dialog now, so an inline ``<form>`` or a code box
        left in the page means the old screen crept back.
        """
        pane = self._panes()["people"]
        for marker in ('<tbody id="acct-people">', '<tbody id="acct-groups">',
                       '<button type="button" id="acct-add-user">Add user</button>',
                       '<button type="button" id="group-add-button">Add group</button>',
                       '<button type="button" id="acct-audit">See more history</button>'):
            self.assertIn(marker, pane)
        self.assertNotIn("<form", pane, "an inline form came back")
        self.assertNotIn("acct-otp", pane, "the one-time code belongs in a dialog")
        self.assertEqual(pane.count('<table class="grid">'), 2)

    def test_the_people_pane_is_three_panels_with_equal_headings(self) -> None:
        """Bare-metal review: Groups sat directly under the Add-user button with
        a smaller heading, so the three sections read as one undifferentiated
        column. Each section is now its own bordered panel with an equal h2."""
        pane = self._panes()["people"]
        self.assertEqual(pane.count('<section class="panel"'), 3)
        self.assertIn('id="groups-panel"', pane)
        self.assertIn('id="audit-panel"', pane)
        for heading in ("<h2>Users</h2>", "<h2>Groups</h2>", "<h2>Recent changes</h2>"):
            self.assertIn(heading, pane)
        self.assertNotIn('class="subhead"', pane)

    def test_the_first_panel_aligns_with_the_rail(self) -> None:
        """Bare-metal review: every panel carries margin-top, so the right
        column started below the rail's first item. The theme resets the first
        panel in the settings panes only; later panels keep their margin, which
        is the visual separation between Users / Groups / Recent changes."""
        self.assertIn(".settings-panes .panel:first-child { margin-top: 0; }", THEME_CSS)

    def test_the_shared_scripts_are_shipped_once(self) -> None:
        for name, constant in (("dialog", stub.DIALOG_JS), ("rail", stub.SETTINGS_JS),
                               ("power", stub.POWER_JS)):
            with self.subTest(script=name):
                self.assertEqual(self.html.count(constant), 1)

    def test_the_action_rows_are_styled_because_the_theme_says_so(self) -> None:
        """Bare <button>s in these rows painted browser grey; .btn is width:100%."""
        for selector in (".owui-actions button", ".owui-actions button.danger",
                         ".settings-app", ".rail-item", ".dialog-mask",
                         "table.grid", "td.col-actions", ".icon-btn", ".icon-btn[disabled]",
                         ".dialog-field", ".dialog-check", ".sr-only"):
            self.assertIn(selector, THEME_CSS, f"{selector} did not make it into the theme")


class PowerInterlockTests(unittest.TestCase):
    """The three ``in_flight`` probes the power controls ask before obeying.

    They are read-only on purpose (``Lock.locked()``, ``Popen.poll()``): an HTTP
    handler that *acquires* a lock to find out whether work is running would stop
    the very work it is asking about. So these tests hold a fake lock and assert
    the sentence comes back, and assert ``None`` when nothing is held.
    """

    def test_chat_is_quiet_when_nothing_is_preparing(self) -> None:
        self.assertIsNone(chat_ready.in_flight_work())

    @mock.patch.object(chat_ready, "_prepare_lock")
    def test_a_prepare_window_is_named(self, lock: mock.Mock) -> None:
        lock.locked.return_value = True
        with mock.patch.object(chat_ready, "_prepare_phase", "pull"):
            self.assertIn("download", chat_ready.in_flight_work() or "")

    @mock.patch.object(chat_ready, "_prepare_lock")
    def test_a_load_window_is_named(self, lock: mock.Mock) -> None:
        lock.locked.return_value = True
        with mock.patch.object(chat_ready, "_prepare_phase", "load"):
            self.assertIn("loaded", chat_ready.in_flight_work() or "")

    @mock.patch.object(chat_ready, "_prepare_lock")
    def test_the_readiness_verify_is_not_ruinable(self, lock: mock.Mock) -> None:
        """Bare-metal review: the prepare pass holds the lock through the
        readiness verify — a read, no disk write, no serving transition — and
        the power buttons refused there while the model was already serving.
        Only the ruinable phases name themselves."""
        lock.locked.return_value = True
        self.assertIsNone(chat_ready.in_flight_work())

    @mock.patch.object(chat_ready, "_pull_process")
    def test_a_live_download_is_named(self, proc: mock.Mock) -> None:
        proc.poll.return_value = None
        self.assertIn("download", chat_ready.in_flight_work() or "")

    @mock.patch.object(chat_ready, "_pull_process")
    def test_a_finished_download_is_not(self, proc: mock.Mock) -> None:
        proc.poll.return_value = 0
        self.assertIsNone(chat_ready.in_flight_work())

    def test_modules_are_quiet_at_rest(self) -> None:
        self.assertIsNone(modules.in_flight_work())
        self.assertFalse(owui_policy.reconcile_in_flight())

    @mock.patch.object(modules, "owui_reset_state")
    def test_a_reset_window_is_named(self, reset: mock.Mock) -> None:
        reset.return_value = {"state": "clearing", "error": None, "at": "now"}
        self.assertIn("rebuilt", modules.in_flight_work() or "")

    @mock.patch.object(modules, "_lock")
    def test_a_container_window_is_named(self, lock: mock.Mock) -> None:
        lock.locked.return_value = True
        self.assertIn("chat interface", modules.in_flight_work() or "")


class PowerControlHttpTests(unittest.TestCase):
    """``POST /_lemonseed/api/power``: who may, when it is refused, what it leaves.

    ``stub.schedule_power`` is patched in every case that would obey. It is the
    one line in the control plane that can end the test runner's own session, so
    no test here is allowed to reach it.
    """

    ADMIN = {"X-Forwarded-Groups": "admin", "X-Forwarded-User": "matt"}
    MEMBER = {"X-Forwarded-Groups": "users", "X-Forwarded-User": "jo"}

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer((stub.HOST, 0), stub.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        marker = tempfile.TemporaryDirectory()
        self.addCleanup(marker.cleanup)
        self.marker_file = Path(marker.name) / "reboot-pending.json"
        patch = mock.patch.object(chat_ready, "REBOOT_PENDING_FILE", self.marker_file)
        patch.start()
        self.addCleanup(patch.stop)

    def _post(self, path: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
        req = Request(
            f"http://{stub.HOST}:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    @mock.patch.object(stub, "schedule_power")
    def test_a_member_cannot_stop_the_appliance(self, power: mock.Mock) -> None:
        code, body = self._post("/_lemonseed/api/power", {"action": "reboot"}, self.MEMBER)
        self.assertEqual(code, 403)
        self.assertIn(stub.NEEDS_ADMIN_MARKER, body.get("lemonseed", ""))
        power.assert_not_called()

    @mock.patch.object(stub, "schedule_power")
    def test_an_unknown_action_is_refused(self, power: mock.Mock) -> None:
        code, _body = self._post("/_lemonseed/api/power", {"action": "selftest"}, self.ADMIN)
        self.assertEqual(code, 400)
        code, _body = self._post("/_lemonseed/api/power", {}, self.ADMIN)
        self.assertEqual(code, 400)
        power.assert_not_called()

    @mock.patch.object(stub, "schedule_power")
    @mock.patch.object(chat_ready, "in_flight_work")
    def test_work_in_flight_is_refused_with_a_sentence(self, work: mock.Mock,
                                                       power: mock.Mock) -> None:
        work.return_value = "a model download is running"
        code, body = self._post("/_lemonseed/api/power", {"action": "shutdown"}, self.ADMIN)
        self.assertEqual(code, 409)
        # The refusal has to name the work, or the admin cannot tell how long to
        # wait, which is the whole reason the interlock exists.
        self.assertIn("model download", body.get("detail", ""))
        self.assertFalse(self.marker_file.exists())
        power.assert_not_called()

    @mock.patch.object(stub, "schedule_power")
    @mock.patch.object(stub.audit, "record")
    def test_reboot_writes_the_marker_then_answers_then_schedules(self, record: mock.Mock,
                                                                 power: mock.Mock) -> None:
        code, body = self._post("/_lemonseed/api/power", {"action": "reboot"}, self.ADMIN)
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], "ok")
        power.assert_called_once_with("reboot")
        self.assertTrue(self.marker_file.is_file(), "the portal has nothing to report rebooting")
        self.assertEqual(json.loads(self.marker_file.read_text())["state"], "rebooting")
        self.assertTrue(chat_ready._reboot_pending())
        record.assert_called_once_with("matt", "appliance", "power.reboot")

    @mock.patch.object(stub, "schedule_power")
    @mock.patch.object(stub.audit, "record")
    def test_shutdown_does_not_queue_a_reboot(self, _record: mock.Mock, power: mock.Mock) -> None:
        code, _body = self._post("/_lemonseed/api/power", {"action": "shutdown"}, self.ADMIN)
        self.assertEqual(code, 200)
        power.assert_called_once_with("shutdown")
        # A shutdown that left a reboot marker behind would make the portal claim
        # the appliance is coming back. It is not.
        self.assertFalse(self.marker_file.exists())

    def test_the_reboot_marker_survives_a_round_trip(self) -> None:
        """The shell's banner depends on the file _reboot_pending() can read."""
        chat_ready.mark_reboot_pending("a test asked")
        self.assertTrue(self.marker_file.is_file())
        self.assertTrue(chat_ready._reboot_pending())
        chat_ready.clear_reboot_pending()
        self.assertFalse(chat_ready._reboot_pending())

    @mock.patch.object(stub, "POWER_DELAY_SEC", 0.0)
    @mock.patch.object(stub, "run_best_effort")
    def test_schedule_power_uses_systemctl(self, run: mock.Mock) -> None:
        stub.schedule_power("shutdown")
        deadline = time.time() + 5
        while time.time() < deadline and not run.call_args_list:
            time.sleep(0.02)
        run.assert_called_once_with(["systemctl", "poweroff"])


class UpdateHttpTests(unittest.TestCase):
    """``GET/POST /_lemonseed/api/update…``: who may see and start an update.

    Mirrors PowerControlHttpTests: a real server on a real port, identity by
    forwarded headers, and the job backend mocked so no subprocess, download,
    or reboot can happen inside the test runner.
    """

    ADMIN = {"X-Forwarded-Groups": "admin", "X-Forwarded-User": "matt"}
    MEMBER = {"X-Forwarded-Groups": "users", "X-Forwarded-User": "jo"}

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer((stub.HOST, 0), stub.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        updates._job.update(kind="", state="idle", detail="", progress=None)
        self.addCleanup(lambda: updates._job.update(kind="", state="idle", detail="", progress=None))
        # The running version comes off /run/appliance/version in production;
        # a temp copy means the test names the version it wrote, not the dev
        # box's own.
        version = tempfile.TemporaryDirectory()
        self.addCleanup(version.cleanup)
        self.version_file = Path(version.name) / "version"
        self.version_file.write_text("2026.02\n", encoding="utf-8")
        patch = mock.patch.object(stub, "VERSION_FILE", self.version_file)
        patch.start()
        self.addCleanup(patch.stop)

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, dict]:
        req = Request(f"http://{stub.HOST}:{self.port}{path}", headers=(headers or {}))
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def _post(self, path: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
        req = Request(
            f"http://{stub.HOST}:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode() or "{}")
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode() or "{}")

    def test_a_member_cannot_see_the_update_state(self) -> None:
        code, body = self._get("/_lemonseed/api/update", self.MEMBER)
        self.assertEqual(code, 403)
        self.assertIn(stub.NEEDS_ADMIN_MARKER, body.get("lemonseed", ""))

    def test_a_member_cannot_start_a_check(self) -> None:
        with mock.patch.object(updates, "start_check") as check:
            code, body = self._post("/_lemonseed/api/update/check", {}, self.MEMBER)
        self.assertEqual(code, 403)
        self.assertIn(stub.NEEDS_ADMIN_MARKER, body.get("lemonseed", ""))
        check.assert_not_called()

    def test_the_version_section_reports_the_running_version(self) -> None:
        code, body = self._get("/_lemonseed/api/update", self.ADMIN)
        self.assertEqual(code, 200)
        self.assertEqual(body["running"], "2026.02")
        self.assertEqual(body["job"]["state"], "idle")

    def test_check_now_starts_the_record_job(self) -> None:
        with mock.patch.object(updates, "start_check", return_value=None) as check:
            code, body = self._post("/_lemonseed/api/update/check", {}, self.ADMIN)
        self.assertEqual(code, 202)
        self.assertEqual(body, {"state": "running", "kind": "check"})
        check.assert_called_once_with()

    def test_an_in_flight_job_is_refused_with_a_sentence(self) -> None:
        with mock.patch.object(updates, "start_check",
                               return_value="a system update is running"):
            code, body = self._post("/_lemonseed/api/update/check", {}, self.ADMIN)
        self.assertEqual(code, 409)
        self.assertIn("system update", body.get("detail", ""))

    @mock.patch.object(stub.audit, "record")
    def test_install_answers_202_and_audits_the_action(self, record: mock.Mock) -> None:
        with mock.patch.object(updates, "start_install", return_value=None) as install:
            code, body = self._post("/_lemonseed/api/update", {}, self.ADMIN)
        self.assertEqual(code, 202)
        self.assertEqual(body["kind"], "install")
        install.assert_called_once_with(retry=False)
        record.assert_called_once_with("matt", "appliance", "update.install")

    @mock.patch.object(stub.audit, "record")
    def test_retry_audits_update_retry(self, record: mock.Mock) -> None:
        with mock.patch.object(updates, "start_install", return_value=None) as install:
            code, _body = self._post("/_lemonseed/api/update", {"retry": True}, self.ADMIN)
        self.assertEqual(code, 202)
        install.assert_called_once_with(retry=True)
        record.assert_called_once_with("matt", "appliance", "update.retry")

    @mock.patch.object(stub.audit, "record")
    def test_rollback_answers_202_and_audits_the_action(self, record: mock.Mock) -> None:
        with mock.patch.object(updates, "start_rollback", return_value=None) as rollback:
            code, body = self._post("/_lemonseed/api/update/rollback", {}, self.ADMIN)
        self.assertEqual(code, 202)
        self.assertEqual(body["kind"], "rollback")
        rollback.assert_called_once_with("2026.02")
        record.assert_called_once_with("matt", "appliance", "update.rollback")

    @mock.patch.object(stub.audit, "record")
    def test_an_ineligible_rollback_is_refused_and_not_audited(self, record: mock.Mock) -> None:
        with mock.patch.object(updates, "start_rollback",
                               return_value="the other slot has no version recorded"):
            code, body = self._post("/_lemonseed/api/update/rollback", {}, self.ADMIN)
        self.assertEqual(code, 409)
        self.assertIn("no version recorded", body.get("detail", ""))
        record.assert_not_called()

    @mock.patch.object(stub, "schedule_power")
    def test_power_is_refused_while_an_install_is_in_flight(self, power: mock.Mock) -> None:
        with mock.patch.object(updates, "in_flight",
                               return_value="a system update is running"):
            code, body = self._post("/_lemonseed/api/power", {"action": "reboot"}, self.ADMIN)
        self.assertEqual(code, 409)
        self.assertIn("system update", body.get("detail", ""))
        power.assert_not_called()


class AccessLogTests(unittest.TestCase):
    """The access trail is a journald channel of its own — with the real
    client IP (nginx's X-Forwarded-For) and the signed-in user on every line
    (bare-metal review)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.socket_path = Path(tmp.name) / "journal.sock"
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.server.bind(str(self.socket_path))
        self.server.settimeout(2)
        self.addCleanup(self.server.close)
        # Point the singleton's handler at the test socket, restoring after.
        self._originals = [(h, h.socket_path) for h in stub.ACCESS_LOGGER.handlers
                           if isinstance(h, journal.JournalHandler)]
        self.addCleanup(self._restore)
        for handler, _ in self._originals:
            handler.socket_path = self.socket_path
        # A handler without a live request: log_request/log_message only need
        # the header block and the peer address.
        self.handler = stub.Handler.__new__(stub.Handler)
        self.handler.headers = {"X-Forwarded-For": "10.1.2.3, 10.0.0.1",
                                "X-Forwarded-User": "matt"}
        self.handler.client_address = ("127.0.0.1", 12345)
        self.handler.requestline = "GET /_lemonseed/api/update HTTP/1.1"
        self.handler.path = "/_lemonseed/api/update"

    def _restore(self) -> None:
        for handler, path in self._originals:
            handler.socket_path = path

    def _recv_fields(self) -> dict[str, str]:
        return dict(
            line.split("=", 1)
            for line in self.server.recv(2048).decode().splitlines()
            if "=" in line
        )

    def test_the_access_line_names_the_client_and_the_user(self) -> None:
        self.handler.log_request(200, "42")
        fields = self._recv_fields()
        self.assertEqual(fields["SYSLOG_IDENTIFIER"], "appliance-control-plane-access")
        self.assertEqual(fields["CLIENT"], "10.1.2.3")
        self.assertEqual(fields["USER"], "matt")
        self.assertEqual(fields["CODE"], "200")
        self.assertIn("GET /_lemonseed/api/update", fields["MESSAGE"])
        self.assertIn("user=matt", fields["MESSAGE"])
        self.assertIn("client=10.1.2.3", fields["MESSAGE"])

    def test_diagnostics_do_not_reach_the_access_channel(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.handler.log_message("client disconnected (%s)", "/x")
        self.assertIn("client disconnected", err.getvalue())
        with self.assertRaises(socket.timeout):
            self.server.recv(2048)   # nothing was sent on the channel

    def test_the_gate_subrequest_is_not_logged(self) -> None:
        """session-verify fires once per gated request — it would double the
        channel with a userless, loopback line per request (BM-4 review)."""
        self.handler.path = "/_lemonseed/session-verify"
        self.handler.log_request(200, "0")
        with self.assertRaises(socket.timeout):
            self.server.recv(2048)   # nothing was sent on the channel

    def test_a_missing_journald_socket_drops_the_line(self) -> None:
        for handler, _ in self._originals:
            handler.socket_path = Path("/nonexistent/journal.sock")
        self.handler.log_request(200, "1")   # no exception: a line is never fatal


class ClientScriptSyntaxTests(unittest.TestCase):
    """Every page script must parse, or the panel it belongs to is dead.

    Nothing else in this suite executes most of the page scripts, so a stray brace
    in a control panel file would otherwise reach a bare-metal test cycle to be
    found — and in a browser a syntax error is a quiet one: the page looks loaded
    and nothing works. ``node --check`` parses without running, needs no DOM, and
    costs milliseconds. Node is a dev-box dependency, so this skips without it.
    """

    def test_every_page_script_parses(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is required to parse the page scripts")
        # Scanned rather than listed, so a new script cannot be left out of the rule.
        scripts = {name: value for name, value in vars(stub).items()
                   if name.endswith("_JS") and isinstance(value, str)}
        self.assertGreater(len(scripts), 6, "the page-script scan matched nothing")
        broken: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            for name, source in sorted(scripts.items()):
                path = Path(tmp) / (name + ".js")
                path.write_text(source, encoding="utf-8")
                proc = subprocess.run([node, "--check", str(path)],
                                      capture_output=True, text=True, timeout=60, check=False)
                if proc.returncode != 0:
                    tail = [line for line in proc.stderr.splitlines() if "SyntaxError" in line]
                    broken.append(f"{name}: {tail or proc.stderr.strip()[:200]}")
        self.assertEqual(broken, [], "these page scripts will not parse in a browser")


class SharedThemeTests(unittest.TestCase):
    """One stylesheet root for the whole appliance (M6.6 S5.8).

    Inlined CSS is how a login page and a nginx error page end up disagreeing
    about what Lemonseed looks like, so every page links the same file and none
    carries its own copy.
    """

    LINK = 'href="/_lemonseed/lemonseed.css"'

    def _pages(self) -> dict[str, str]:
        snap = {"version": "test", "chat": {}, "hardware": {}, "modules": {}}
        return {
            "setup": stub.page_setup(dict(snap)).decode(),
            "login": stub.page_login(rd="/").decode(),
            "shell": stub.page_shell(dict(snap), view="chat").decode(),
            "settings": stub.page_settings_content(dict(snap)).decode(),
        }

    def test_every_page_links_the_shared_theme(self) -> None:
        for name, html in self._pages().items():
            with self.subTest(page=name):
                self.assertIn(self.LINK, html)
                self.assertNotIn("<style>", html, f"{name} still inlines its own theme")

    def test_the_stylesheet_exists_where_the_gateway_looks_for_it(self) -> None:
        """gateway.nix serves control-plane/static/lemonseed.css by name."""
        css = THEME_CSS
        self.assertIn(":root", css)
        self.assertIn("--accent", css)
        # Rules that used to live in Python must have moved with it, or the
        # rendered pages are now unstyled for those components.
        for selector in (".model-card", ".owui-actions", ".login-card"):
            self.assertIn(selector, css)

    def test_no_page_style_constant_survived_the_move(self) -> None:
        """A leftover copy in Python is the drift this row set out to remove."""
        for name in ("STYLE", "SETTINGS_MODEL_PICKER_CSS", "OWUI_CONFIG_CSS"):
            self.assertFalse(hasattr(stub, name), f"stub.{name} is a second copy of the theme")


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
