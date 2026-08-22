"""Hardware inventory for M5 catalog planning."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from urllib.request import Request, urlopen

LEMOND_ENV_FILE = Path("/var/lib/lemonseed/lemonade/lemond.env")
HARDWARE_CACHE = Path("/run/appliance/hardware.json")
MODELS_DIR = Path("/var/lib/lemonseed/models")


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def lemonade_api_key() -> str:
    raw = _read_text(LEMOND_ENV_FILE)
    if not raw:
        return ""
    for line in raw.splitlines():
        if line.startswith("LEMONADE_API_KEY="):
            return line.removeprefix("LEMONADE_API_KEY=")
    return ""


def _lemond_get(path: str, timeout: float = 3.0) -> Optional[dict[str, Any]]:
    key = lemonade_api_key()
    if not key:
        return None
    try:
        req = Request(
            f"http://127.0.0.1:13305{path}",
            headers={"Authorization": f"Bearer {key}"},
        )
        with urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def _cpu_info() -> dict[str, Any]:
    name = "unknown"
    cores = 0
    threads = 0
    try:
        out = subprocess.check_output(["lscpu"], text=True, stderr=subprocess.DEVNULL, timeout=5)
        for line in out.splitlines():
            if line.startswith("Model name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("CPU(s):") and "NUMA" not in line:
                try:
                    threads = int(line.split(":", 1)[1].strip().split()[0])
                except ValueError:
                    pass
            elif line.startswith("Core(s) per socket:"):
                try:
                    per = int(line.split(":", 1)[1].strip())
                    socks = 1
                    for l2 in out.splitlines():
                        if l2.startswith("Socket(s):"):
                            socks = int(l2.split(":", 1)[1].strip())
                            break
                    cores = per * socks
                except ValueError:
                    pass
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    if not cores and threads:
        cores = threads
    return {"name": name, "cores": cores, "threads": threads}


def _mem_total_gb() -> float:
    try:
        raw = Path("/proc/meminfo").read_text(encoding="utf-8")
        for line in raw.splitlines():
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                return round(kb / (1024 * 1024), 2)
    except (OSError, ValueError):
        pass
    return 0.0


def _nvidia_gpus_smi() -> list[dict[str, Any]]:
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=index,name,memory.total,driver_version",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    gpus: list[dict[str, Any]] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            idx = int(parts[0])
            vram_mib = float(parts[2])
        except ValueError:
            continue
        gpus.append(
            {
                "index": idx,
                "name": parts[1],
                "vram_gb": round(vram_mib / 1024.0, 2),
                "driver_version": parts[3],
                "vendor": "nvidia",
            }
        )
    return gpus


def _gpus_from_system_info(info: dict[str, Any]) -> list[dict[str, Any]]:
    devices = info.get("devices") or {}
    nvidia = devices.get("nvidia_gpu") or []
    out: list[dict[str, Any]] = []
    if isinstance(nvidia, list):
        for gpu in nvidia:
            if not isinstance(gpu, dict):
                continue
            out.append(
                {
                    "index": int(gpu.get("index") or 0),
                    "name": str(gpu.get("name") or "NVIDIA GPU"),
                    "vram_gb": float(gpu.get("vram_gb") or 0),
                    "driver_version": str(gpu.get("driver_version") or ""),
                    "family": str(gpu.get("family") or ""),
                    "compute_capability": str(gpu.get("compute_capability") or ""),
                    "vendor": "nvidia",
                    "available": bool(gpu.get("available", True)),
                }
            )
    return out


def _disk_free(path: Path) -> dict[str, int]:
    try:
        usage = shutil.disk_usage(path if path.exists() else path.parent)
        return {
            "path": str(path),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        }
    except OSError:
        return {"path": str(path), "total_bytes": 0, "used_bytes": 0, "free_bytes": 0}


def collect_hardware() -> dict[str, Any]:
    """Build the Lemonseed hardware report; proxy lemond system-info when healthy."""
    system_info = _lemond_get("/v1/system-info", timeout=5.0)
    healthy = isinstance(system_info, dict) and bool(system_info)
    info = system_info if healthy else {}

    gpus = _gpus_from_system_info(info) or _nvidia_gpus_smi()
    cpu = _cpu_info()
    # Prefer lemond CPU fields when present.
    dev_cpu = (info.get("devices") or {}).get("cpu") or {}
    if isinstance(dev_cpu, dict) and dev_cpu.get("available"):
        cpu = {
            "name": str(dev_cpu.get("name") or cpu["name"]),
            "cores": int(dev_cpu.get("cores") or cpu["cores"] or 0),
            "threads": int(dev_cpu.get("threads") or cpu["threads"] or 0),
            "family": str(dev_cpu.get("family") or ""),
        }

    lemond: dict[str, Any] = {"healthy": healthy}
    if healthy:
        # Lemonade owns this schema — pass it through rather than inventing fields.
        lemond["system_info"] = info

    return {
        "cpu": cpu,
        "ram_gb": _mem_total_gb(),
        "gpus": gpus,
        "models_storage": _disk_free(MODELS_DIR),
        "lemond": lemond,
    }


def persist_hardware(report: dict[str, Any]) -> None:
    try:
        HARDWARE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = HARDWARE_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        tmp.replace(HARDWARE_CACHE)
    except OSError as exc:
        print(f"hardware persist: {exc}", flush=True)


def max_gpu_vram_gb(report: dict[str, Any]) -> float:
    gpus = report.get("gpus") or []
    best = 0.0
    for gpu in gpus:
        if isinstance(gpu, dict):
            best = max(best, float(gpu.get("vram_gb") or 0))
    return best


def largest_gpu_index(report: dict[str, Any]) -> Optional[int]:
    """Index of the GPU with the most VRAM, or None if no GPUs."""
    gpus = report.get("gpus") or []
    best_idx: Optional[int] = None
    best_vram = -1.0
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        vram = float(gpu.get("vram_gb") or 0)
        if vram > best_vram:
            best_vram = vram
            try:
                best_idx = int(gpu.get("index"))
            except (TypeError, ValueError):
                best_idx = 0
    return best_idx
