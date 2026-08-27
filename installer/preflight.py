"""Installer host preflight: refuse machines that cannot usefully run Lemonseed."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional

# Product floors (see DESIGN.md). Disk raised from the old 16 GiB soft floor.
MIN_DISK_BYTES = 20 * 1024**3
NOMINAL_RAM_GB = 8
# x86 boxes labeled "8 GB" often report ~7.6–7.8 GiB MemTotal (BIOS/GPU reservations).
RAM_OVERHEAD_FRACTION = 0.10
MIN_MEMTOTAL_KB = int(NOMINAL_RAM_GB * (1024**3) * (1.0 - RAM_OVERHEAD_FRACTION) / 1024)
MIN_MEMTOTAL_GB = round(MIN_MEMTOTAL_KB / (1024 * 1024), 2)
MIN_CPU_CORES = 4

# GPU VRAM is checked on the installed appliance (drivers + lemond), not on the
# lean installer ISO — we do not ship NVIDIA drivers on the live USB.


def memtotal_display_gb(total_kb: int) -> float:
    """Round MemTotal for home-user copy (one decimal place)."""
    return round(total_kb / (1024 * 1024), 1)


def meminfo_gb() -> dict[str, float]:
    total_kb = 0
    available_kb = 0
    try:
        raw = Path("/proc/meminfo").read_text(encoding="utf-8")
    except OSError:
        return {"total_gb": 0.0, "available_gb": 0.0, "total_kb": 0}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            kb = int(parts[1])
        except ValueError:
            continue
        if line.startswith("MemTotal:"):
            total_kb = kb
        elif line.startswith("MemAvailable:"):
            available_kb = kb
    total_gb = round(total_kb / (1024 * 1024), 2)
    available_gb = round((available_kb or total_kb) / (1024 * 1024), 2)
    return {"total_gb": total_gb, "available_gb": available_gb, "total_kb": total_kb}


def cpu_cores() -> dict[str, Any]:
    name = "unknown"
    cores = 0
    threads = 0
    try:
        out = subprocess.check_output(
            ["lscpu"], text=True, stderr=subprocess.DEVNULL, timeout=5
        )
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


def evaluate_host(
    *,
    ram_total_gb: Optional[float] = None,
    ram_total_kb: Optional[int] = None,
    ram_available_gb: Optional[float] = None,
    cores: Optional[int] = None,
) -> dict[str, Any]:
    """Return ok + human reasons. Args are overridable for unit tests."""
    mem = meminfo_gb()
    cpu = cpu_cores()
    if ram_total_kb is not None:
        total_kb = int(ram_total_kb)
    elif ram_total_gb is not None:
        total_kb = int(round(float(ram_total_gb) * (1024 * 1024)))
    else:
        total_kb = int(mem["total_kb"])
    total = round(total_kb / (1024 * 1024), 2)
    display_gb = memtotal_display_gb(total_kb)
    available = float(
        ram_available_gb if ram_available_gb is not None else mem["available_gb"]
    )
    ncores = int(cores if cores is not None else cpu["cores"] or 0)

    ram_ok = total_kb >= MIN_MEMTOTAL_KB
    cores_ok = ncores >= MIN_CPU_CORES

    reasons: list[str] = []
    if not ram_ok:
        reasons.append(
            f"Lemonseed requires {NOMINAL_RAM_GB} GB of memory, but this machine "
            f"has about {display_gb:.1f} GB. Please install additional memory and try again."
        )
    if not cores_ok:
        reasons.append(
            f"Lemonseed requires at least {MIN_CPU_CORES} processor cores, but this "
            f"machine has {ncores}. Please try again on a computer with a more capable "
            f"processor."
        )

    return {
        "ok": ram_ok and cores_ok,
        "reasons": reasons,
        "ram_ok": ram_ok,
        "cores_ok": cores_ok,
        "ram_total_gb": total,
        "ram_display_gb": display_gb,
        "ram_total_kb": total_kb,
        "ram_available_gb": available,
        "cpu_cores": ncores,
        "cpu_name": cpu.get("name"),
        "min_disk_bytes": MIN_DISK_BYTES,
        "nominal_ram_gb": NOMINAL_RAM_GB,
        "min_memtotal_kb": MIN_MEMTOTAL_KB,
        "min_memtotal_gb": MIN_MEMTOTAL_GB,
        "min_cpu_cores": MIN_CPU_CORES,
    }


def require_host_ready() -> dict[str, Any]:
    """Raise InstallError-compatible message string via caller."""
    from install import InstallError

    result = evaluate_host()
    if not result["ok"]:
        raise InstallError(" ".join(result["reasons"]) or "Host does not meet minimum requirements")
    return result
