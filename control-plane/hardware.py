"""Hardware inventory for M5 catalog planning."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen

LEMOND_ENV_FILE = Path("/var/lib/lemonseed/lemonade/lemond.env")
HARDWARE_CACHE = Path("/run/appliance/hardware.json")
MODELS_DIR = Path("/var/lib/lemonseed/models")

# GPU inventory is static for the lifetime of a boot — GPUs cannot appear or
# disappear without a reboot. We therefore probe the expensive external tools
# (nvidia-smi, llama-cli, vulkaninfo) exactly once per boot and serve every
# later collect_hardware() call from the cache. Without this, the portal's
# 5s /api/status poll + settings page reload would re-run nvidia-smi on an AMD
# box (where it fails and spams the console) on every poll.
_GPU_LOCK = threading.Lock()
_GPU_CACHE: list[dict[str, Any]] = []
_GPU_COLLECTED = False


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
                # Round to the nearest whole GiB: MemTotal is always below the
                # installed DIMM size (kernel reservation, PCIe/VA space, iGPU
                # GTT carve-out), and 8 GiB boxes report ~7.6–7.7. Nearest-GB
                # keeps fit floors honest without over-crediting.
                return float(round(kb / (1024 * 1024)))
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


def _amd_gpus_llama_cli() -> list[dict[str, Any]]:
    """Enumerate Vulkan GPU devices via `llama-cli --list-devices`.

    Unlike vulkaninfo, this reports the real device memory size (GTT on AMD
    APUs, VRAM on dGPUs). Uses the Vulkan llama-cpp binary shipped with the
    lemond backends. Returns [] when the binary is absent or finds no device.
    """
    cli = Path("/etc/lemonseed/lemond-backends/llamacpp-vulkan-cli")
    if not cli.exists():
        return []
    try:
        out = subprocess.check_output(
            [str(cli), "--list-devices"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    gpus: list[dict[str, Any]] = []
    for line in out.splitlines():
        # Format: "Vulkan0: AMD Radeon 8060S Graphics (RADV STRIX_HALO) (56210 MiB, 50007 MiB free)"
        s = line.strip()
        if not s.startswith("Vulkan"):
            continue
        # Extract name (between first ": " and last " (") and total MiB (first parenthesized number).
        if ": " not in s or " (" not in s:
            continue
        rest = s.partition(": ")[2]
        name = rest.rsplit(" (", 1)[0]
        # Find the first parenthesized group containing "MiB".
        m = re.search(r"\((\d+)\s+MiB", rest)
        if not m:
            continue
        total_mib = float(m.group(1))
        gpus.append(
            {
                "index": len(gpus),
                "name": name,
                "vram_gb": round(total_mib / 1024.0, 2),
                "driver_version": "",
                "vendor": "amd",
                "available": True,
                "device_type": "",
            }
        )
    return gpus


def _amd_gpus_vulkaninfo() -> list[dict[str, Any]]:
    """Enumerate AMD (and other) Vulkan GPU devices via `vulkaninfo`.

    Used when lemond's system-info has no AMD field (Lemonade 11.5 reports
    NVIDIA-only). Returns [] when the tool is absent or finds no GPU device.
    VRAM is not exposed by vulkaninfo, so it is reported as 0; the planner
    treats that as "present but size unknown" (see plan_model).

    Filters out CPU-type devices (e.g. llvmpipe software renderer) — only
    INTEGRATED_GPU and DISCRETE_GPU are real GPUs.
    """
    try:
        out = subprocess.check_output(
            ["vulkaninfo", "--summary"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    gpus: list[dict[str, Any]] = []
    current: Optional[dict[str, Any]] = None
    for line in out.splitlines():
        s = line.strip()
        # vulkaninfo --summary uses "GPU0:", "GPU1:", … as device headers.
        # Each header starts a new device; properties follow as "key = value".
        if s.startswith("GPU") and s.endswith(":"):
            if current is not None:
                _maybe_append_gpu(gpus, current)
            current = {
                "index": len(gpus),
                "name": "",
                "vram_gb": 0,
                "driver_version": "",
                "vendor": "amd",
                "available": True,
                "device_type": "",
            }
            continue
        if current is None or "=" not in s:
            continue
        key, _, value = s.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "deviceName":
            current["name"] = value
        elif key == "deviceType":
            current["device_type"] = value
    # Finalize the last device.
    if current is not None:
        _maybe_append_gpu(gpus, current)
    # Re-index after filtering (indices may have gaps).
    for i, gpu in enumerate(gpus):
        gpu["index"] = i
    return gpus


def _maybe_append_gpu(gpus: list[dict[str, Any]], gpu: dict[str, Any]) -> None:
    """Append a vulkaninfo device only if it is a real GPU (not CPU/llvmpipe)."""
    dtype = gpu.get("device_type", "")
    # PHYSICAL_DEVICE_TYPE_CPU is the software renderer (llvmpipe) — not a GPU.
    # PHYSICAL_DEVICE_TYPE_VIRTUAL can be a real GPU in some configurations.
    if dtype == "PHYSICAL_DEVICE_TYPE_CPU":
        return
    gpus.append(gpu)


def _gpus_from_system_info(info: dict[str, Any]) -> list[dict[str, Any]]:
    devices = info.get("devices") or {}
    nvidia = devices.get("nvidia_gpu") or []
    out: list[dict[str, Any]] = []
    if isinstance(nvidia, list):
        for gpu in nvidia:
            if not isinstance(gpu, dict):
                continue
            # Skip unavailable GPUs (e.g. lemond reports a placeholder entry
            # with available=false when no NVIDIA GPU is present).
            if not gpu.get("available", True):
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
                    "available": True,
                }
            )
    return out


def _collect_gpus() -> list[dict[str, Any]]:
    """Probe external tools once per boot and cache the GPU inventory.

    Safe to call from any thread: the first caller does the (slow) probe and
    every later caller returns the cached result. lemond system-info is still
    fetched fresh each time (it is a cheap localhost HTTP call and reflects
    live model/backend state), but the GPU *inventory* is boot-static.

    An empty result is NOT cached: at first boot the GPU driver (nvidia-smi,
    Vulkan ICD) may not be ready when the CP starts. Caching an empty list
    would permanently hide the GPU. We re-probe until a GPU is found or the
    system has been up long enough that the driver will not appear late.
    """
    global _GPU_CACHE, _GPU_COLLECTED
    with _GPU_LOCK:
        if _GPU_COLLECTED:
            return list(_GPU_CACHE)
        gpus: list[dict[str, Any]] = []
        system_info = _lemond_get("/v1/system-info", timeout=5.0)
        healthy = isinstance(system_info, dict) and bool(system_info)
        info = system_info if healthy else {}
        gpus = _gpus_from_system_info(info) or _nvidia_gpus_smi()
        # lemond 11.5's system-info reports NVIDIA only; add AMD Vulkan devices
        # so an AMD box is not mistaken for "no GPU". Prefer llama-cli (reports
        # real GTT/VRAM) over vulkaninfo (no memory size). De-dupe by name
        # (the same physical GPU may be reported by both nvidia-smi and the
        # Vulkan probe with different vendor labels).
        seen = {g.get("name") for g in gpus}
        for gpu in _amd_gpus_llama_cli() or _amd_gpus_vulkaninfo():
            if gpu.get("name") not in seen:
                gpus.append(gpu)
                seen.add(gpu.get("name"))
        _GPU_CACHE = gpus
        # Only cache a non-empty result. An empty result means the driver may
        # not be ready yet (first boot race); re-probe on the next call.
        if gpus:
            _GPU_COLLECTED = True
        return list(gpus)


def refresh_gpus() -> list[dict[str, Any]]:
    """Force a fresh GPU probe (used once at CP startup after boot)."""
    global _GPU_CACHE, _GPU_COLLECTED
    with _GPU_LOCK:
        _GPU_COLLECTED = False
    return _collect_gpus()


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
    """Build the Lemonseed hardware report; proxy lemond system-info when healthy.

    GPU inventory is cached for the boot (see _collect_gpus); only lemond
    health, CPU, RAM, and disk are re-read each call.
    """
    system_info = _lemond_get("/v1/system-info", timeout=5.0)
    healthy = isinstance(system_info, dict) and bool(system_info)
    info = system_info if healthy else {}

    gpus = _collect_gpus()
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
