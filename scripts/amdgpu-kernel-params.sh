#!/usr/bin/env bash
# lemonseed-amdgpu-kernel-params: calculate APU iGPU shared memory split.
#
# Reads PHYSICAL total RAM from DMI (SMBIOS) via lshw — NOT /proc/meminfo,
# which reports CPU-addressable memory (already reduced by the GPU allocation).
# Allocates (physical_total - 16GB) to the GPU and writes a GRUB stub
# (set kernel_params='ttm.pages_limit=… ttm.page_pool_size=…') to
# the ESP; GRUB sources it on every boot. Reboots if the value changed and
# is not already in effect (LEMONSEED_APPLY_REBOOT=1, set by the appliance
# service; the installer writes the params before first boot, so no reboot).
#
# Only acts when:
#   - An AMD iGPU (INTEGRATED_GPU) is detected via vulkaninfo (appliance)
#    or PCI ID 1002:1586 via lspci (installer — no Vulkan ICD there)
#   - Physical total RAM >= 32GB (smaller systems keep the default split)
#
# Manual override: set LEMONSEED_AMDGPU_SHARED_MEM_GB env var or write a
# value to /etc/lemonseed/amdgpu-shared-mem-override to pin a specific GB.
#
# ESP is mounted on demand (PARTLABEL=ESP → /run/lemonseed-esp), same
# pattern as lemonseed-ab.sh.

set -euo pipefail

# When invoked from the installer's systemd service (or the appliance's
# oneshot unit), the inherited PATH may be minimal and missing /run/current-system/sw/bin
# (where NixOS installs lspci, lshw, jq, etc). Append it explicitly so the
# detection tools are found regardless of the caller's environment.
[[ "$PATH" == */run/current-system/sw/bin* ]] || export PATH="$PATH:/run/current-system/sw/bin"

CPU_RESERVE_GB=16
MIN_TOTAL_GB=32
ESP_MOUNT="/run/lemonseed-esp"
ESP_AUTO=0

cleanup() {
  if [[ "${ESP_AUTO}" -eq 1 && -d "${ESP_MOUNT}" ]]; then
    umount "${ESP_MOUNT}" 2>/dev/null || true
    rmdir "${ESP_MOUNT}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

ensure_esp() {
  mkdir -p "${ESP_MOUNT}"
  if ! mountpoint -q "${ESP_MOUNT}"; then
    if [[ ! -e /dev/disk/by-partlabel/ESP ]]; then
      echo "amdgpu-kernel-params: ESP partition not found; skipping" >&2
      return 1
    fi
    mount -t vfat -o rw /dev/disk/by-partlabel/ESP "${ESP_MOUNT}"
    ESP_AUTO=1
  fi
  return 0
}

# --- Detect Strix Halo iGPU ---
# The ttm.pages_limit / page_pool_size kernel params only make sense for
# Strix Halo APUs (Ryzen AI Max 395/385) where the iGPU shares system RAM.
# Device ID 1002:1586 = Strix Halo [Radeon 8050S / 8060S Graphics].
#
# Primary: vulkaninfo (appliance — full Vulkan stack available).
# Fallback: lspci (installer — no Vulkan ICD, but PCI device ID is enough).
has_amd_igpu() {
  if command -v vulkaninfo >/dev/null 2>&1; then
    if vulkaninfo --summary 2>/dev/null | grep -q "PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU"; then
      return 0
    fi
  else
    echo "amdgpu-kernel-params: WARNING: vulkaninfo not on PATH" >&2
  fi
  if command -v lspci >/dev/null 2>&1; then
    if lspci -n 2>/dev/null | grep -q '1002:1586'; then
      return 0
    fi
  else
    echo "amdgpu-kernel-params: WARNING: lspci not on PATH" >&2
  fi
  return 1
}

# --- Read physical total RAM from DMI (SMBIOS) via lshw ---
# /proc/meminfo is wrong here: it reports CPU-addressable memory, which is
# already reduced by the current GPU allocation. DMI reports the actual
# installed memory regardless of the CPU/GPU split.
#
# lshw -class memory -json emits a JSON array; the "System Memory" node
# carries the total installed size in bytes. We extract it with jq — no
# per-DIMM summing or unit conversion needed.
physical_total_gb() {
  if ! command -v lshw >/dev/null 2>&1; then
    echo "amdgpu-kernel-params: lshw not available; cannot read physical RAM" >&2
    return 1
  fi

  local total_bytes
  total_bytes=$(lshw -class memory -json 2>/dev/null \
    | jq '[.[] | select(.description == "System Memory") | .size] | first // empty')

  if [[ -z "$total_bytes" || "$total_bytes" == "null" ]]; then
    echo "amdgpu-kernel-params: lshw reported no System Memory; skipping" >&2
    return 1
  fi

  echo $(( total_bytes / 1024 / 1024 / 1024 ))
}

# --- Manual override check ---
manual_override() {
  if [[ -n "${LEMONSEED_AMDGPU_SHARED_MEM_GB:-}" ]]; then
    echo "${LEMONSEED_AMDGPU_SHARED_MEM_GB}"
    return 0
  fi
  if [[ -f /etc/lemonseed/amdgpu-shared-mem-override ]]; then
    cat /etc/lemonseed/amdgpu-shared-mem-override
    return 0
  fi
  return 1
}

# --- Calculate pages from GB (4K pages) ---
gb_to_pages() {
  echo $(( $1 * 1024 * 1024 / 4 ))
}

# --- Main ---
main() {
  echo "amdgpu-kernel-params: starting (vulkaninfo=$(command -v vulkaninfo 2>/dev/null || echo absent), lspci=$(command -v lspci 2>/dev/null || echo absent), lshw=$(command -v lshw 2>/dev/null || echo absent))"
  if ! has_amd_igpu; then
    echo "amdgpu-kernel-params: no AMD iGPU detected; skipping"
    return 0
  fi
  echo "amdgpu-kernel-params: AMD iGPU detected"

  local gpu_gb pages params new_content

  if gpu_gb=$(manual_override); then
    pages=$(gb_to_pages "$gpu_gb")
    params="ttm.pages_limit=${pages} ttm.page_pool_size=${pages}"
    new_content="set kernel_params='${params}'"
    echo "amdgpu-kernel-params: manual override ${gpu_gb}GB → ${pages} pages"
  else
    local total_gb
    if ! total_gb=$(physical_total_gb); then
      return 0
    fi

    if (( total_gb < MIN_TOTAL_GB )); then
      echo "amdgpu-kernel-params: physical total ${total_gb}GB < ${MIN_TOTAL_GB}GB; keeping default split"
      return 0
    fi

    gpu_gb=$(( total_gb - CPU_RESERVE_GB ))
    if (( gpu_gb < 4 )); then
      echo "amdgpu-kernel-params: computed GPU ${gpu_gb}GB < 4GB; skipping"
      return 0
    fi

    pages=$(gb_to_pages "$gpu_gb")
    params="ttm.pages_limit=${pages} ttm.page_pool_size=${pages}"
    new_content="set kernel_params='${params}'"
    echo "amdgpu-kernel-params: physical total ${total_gb}GB, GPU ${gpu_gb}GB, CPU ${CPU_RESERVE_GB}GB → ${pages} pages"
  fi

  # Mount ESP and compare against current value.
  if ! ensure_esp; then
    return 0
  fi

  local esp_params="${ESP_MOUNT}/grub/kernel-params"
  local current=""
  if [[ -f "$esp_params" ]]; then
    current=$(cat "$esp_params")
  fi

  if [[ "$current" == "$new_content" ]]; then
    echo "amdgpu-kernel-params: already set; no reboot needed"
    return 0
  fi

  # Write to ESP; reboot only when the appliance service asks for it.
  # The installer runs this before the first boot, so the params are in
  # effect from boot #1 — no reboot needed.
  mkdir -p "$(dirname "$esp_params")"
  echo "amdgpu-kernel-params: writing ${esp_params}"
  echo "$new_content" > "$esp_params"

  if [[ "${LEMONSEED_APPLY_REBOOT:-0}" != "1" ]]; then
    echo "amdgpu-kernel-params: params written; taking effect from next boot"
    return 0
  fi

  # Skip the reboot when the params are already in the running kernel's
  # cmdline (e.g. the ESP was rewritten with the same values) — otherwise
  # every boot would chain another reboot.
  if grep -qF -- "$params" /proc/cmdline 2>/dev/null; then
    echo "amdgpu-kernel-params: params already in effect; no reboot needed"
    return 0
  fi

  echo "amdgpu-kernel-params: rebooting to apply GPU memory split..."
  # Tell the control plane so the portal shows "Rebooting…" (and keeps
  # polling) instead of a dead chat frame. CP deletes this at startup.
  cat > /var/lib/lemonseed/reboot-pending.json <<'EOF'
{
  "reason": "amdgpu-kernel-params",
  "params": "ttm"
}
EOF
  sleep 2
  systemctl reboot
}

main "$@"
