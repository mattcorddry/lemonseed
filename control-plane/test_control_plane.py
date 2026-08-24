#!/usr/bin/env python3
"""Unit tests for control-plane modules (no root, no network)."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

CP = Path(__file__).resolve().parent
sys.path.insert(0, str(CP))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


os.environ.setdefault("APPLIANCE_CP_HOST", "127.0.0.1")
if "APPLIANCE_CP_PORT" not in os.environ:
    os.environ["APPLIANCE_CP_PORT"] = str(_free_port())

import catalog  # noqa: E402
import chat_ready  # noqa: E402
import hardware  # noqa: E402
import stub  # noqa: E402


def _cuda_hw(vram: float = 6.0, ram: float = 7.65) -> dict:
    return {
        "ram_gb": ram,
        "gpus": [{"index": 0, "name": "GTX 1660 SUPER", "vram_gb": vram}],
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
    def test_selects_e2b_for_prototype_sizing(self) -> None:
        plan = catalog.plan_model(_cuda_hw())
        self.assertEqual(plan["state"], "selected")
        self.assertEqual(plan["selection"]["id"], "Gemma-4-E2B-it-GGUF")
        self.assertIn("6", plan["reason"])

    def test_not_ready_without_gpu(self) -> None:
        plan = catalog.plan_model({"ram_gb": 16, "gpus": [], "lemond": {}})
        self.assertEqual(plan["state"], "not-ready")
        self.assertIn("No NVIDIA GPU", plan["reason"])

    def test_not_ready_when_ram_below_floor(self) -> None:
        plan = catalog.plan_model(_cuda_hw(ram=4.0))
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


class HardwareTests(unittest.TestCase):
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


class ChatReadyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.chat_file = Path(self.tmp.name) / "chat-ready.json"
        self.setup_file = Path(self.tmp.name) / "setup-complete"
        self.setup_file.write_text("done\n", encoding="utf-8")
        self.patches = [
            mock.patch.object(chat_ready, "CHAT_READY_FILE", self.chat_file),
            mock.patch.object(chat_ready, "SETUP_COMPLETE_FILE", self.setup_file),
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
    ) -> None:
        collect.return_value = _cuda_hw()
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {"id": "Gemma-4-E2B-it-GGUF", "display_name": "Gemma 4 E2B"},
        }
        result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["model_id"], "Gemma-4-E2B-it-GGUF")

    @mock.patch.object(chat_ready, "_model_is_loaded")
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
        loaded: mock.Mock,
    ) -> None:
        collect.return_value = _cuda_hw()
        plan.return_value = {
            "state": "selected",
            "reason": "ok",
            "selection": {"id": "Gemma-4-E2B-it-GGUF", "display_name": "Gemma 4 E2B"},
        }
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        loaded.side_effect = [False, True]

        result = chat_ready.prepare_local_chat()
        self.assertEqual(result["state"], "ready")
        run.assert_any_call(
            ["load", "Gemma-4-E2B-it-GGUF", "--llamacpp", "cuda", "--pinned"],
            timeout=600,
        )

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

    @mock.patch.object(chat_ready, "prepare_local_chat")
    def test_ensure_chat_worker_returns_immediately(self, prepare: mock.Mock) -> None:
        prepare.side_effect = lambda **_: time.sleep(30)
        start = time.monotonic()
        chat_ready.ensure_chat_worker()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5)
        self.assertIsNotNone(chat_ready._worker)
        self.assertTrue(chat_ready._worker.is_alive())
        chat_ready._worker.join(timeout=1)


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
        cls.thread.join(timeout=5)

    def _get(self, path: str) -> tuple[int, bytes]:
        with urlopen(f"http://{stub.HOST}:{stub.PORT}{path}", timeout=5) as resp:
            return resp.status, resp.read()

    def test_healthz(self) -> None:
        code, body = self._get("/healthz")
        self.assertEqual(code, 200)
        self.assertEqual(body.strip(), b"ok")

    @mock.patch.object(hardware, "collect_hardware")
    @mock.patch.object(hardware, "persist_hardware")
    def test_api_plan(self, _persist: mock.Mock, collect: mock.Mock) -> None:
        collect.return_value = _cuda_hw()
        code, body = self._get("/api/plan")
        self.assertEqual(code, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["state"], "selected")

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

    @mock.patch.object(stub.data_services, "ensure_provision_worker")
    @mock.patch.object(stub.data_services, "data_snapshot")
    def test_api_data(self, snap: mock.Mock, _worker: mock.Mock) -> None:
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


if __name__ == "__main__":
    raise SystemExit(unittest.main(verbosity=2))
