#!/usr/bin/env bash
# Drop mounts, swaps, LVM, and device-mapper maps that keep a disk busy.
#
# systemd-repart --empty=force writes a new GPT then BLKRRPART. If a leftover
# LV from a previous Lemonseed install is still mapped (even with nothing
# mounted), the kernel returns EBUSY:
#   Failed to reread partition table: Device or resource busy
#
# After a failed wipe, LVM metadata on disk may already be gone while the
# in-kernel linear map remains — so vgchange alone is not enough; we also
# remove any DM table that references this disk's major:minor.
#
# Usage: scripts/disk-release.sh [--keep-signatures] /dev/sdX
set -euo pipefail

KEEP_SIGNATURES=0
if [[ "${1:-}" == "--keep-signatures" ]]; then
  KEEP_SIGNATURES=1
  shift
fi

DISK="${1:-}"
if [[ -z "${DISK}" || ! -b "${DISK}" ]]; then
  echo "usage: $0 [--keep-signatures] <block-device>" >&2
  exit 2
fi

DISK="$(readlink -f "${DISK}")"
NAME="$(basename "${DISK}")"

echo "==> releasing holders on ${DISK}"

umount_dev() {
  local dev="$1"
  local mp
  if command -v findmnt >/dev/null 2>&1; then
    while IFS= read -r mp; do
      [[ -n "${mp}" ]] || continue
      echo "    umount ${mp}"
      umount -R "${mp}" 2>/dev/null || umount -l "${mp}" 2>/dev/null || true
    done < <(findmnt -nr -o TARGET -S "${dev}" 2>/dev/null | awk '{ print length($0), $0 }' | sort -nr | cut -d' ' -f2-)
  fi
  umount "${dev}" 2>/dev/null || true
}

disk_majmin_list() {
  local node child
  node="/sys/block/${NAME}"
  [[ -f "${node}/dev" ]] || return 0
  cat "${node}/dev"
  for child in "${node}/${NAME}"*; do
    [[ -f "${child}/dev" ]] || continue
    cat "${child}/dev"
  done
}

is_pv_on_disk() {
  local pv pv_name parent
  pv="$(readlink -f "$1" 2>/dev/null || echo "$1")"
  [[ "${pv}" == "${DISK}" ]] && return 0
  pv_name="$(basename "${pv}")"
  [[ -d "/sys/class/block/${pv_name}" ]] || return 1
  parent="$(basename "$(readlink -f "/sys/class/block/${pv_name}/..")")"
  [[ "${parent}" == "${NAME}" ]]
}

# Unmount and swapoff the disk and every partition.
if command -v lsblk >/dev/null 2>&1; then
  while IFS= read -r path; do
    [[ -n "${path}" ]] || continue
    umount_dev "${path}"
    swapoff "${path}" 2>/dev/null || true
  done < <(lsblk -nr -o PATH "${DISK}" 2>/dev/null)
fi

# Best-effort LVM deactivate while metadata is still visible.
if command -v pvs >/dev/null 2>&1 && command -v vgchange >/dev/null 2>&1; then
  declare -A vgs_seen=()
  while IFS='|' read -r pv vg; do
    pv="$(echo "${pv}" | tr -d ' ')"
    vg="$(echo "${vg}" | tr -d ' ')"
    [[ -n "${pv}" ]] || continue
    is_pv_on_disk "${pv}" || continue
    if [[ -n "${vg}" && -z "${vgs_seen[${vg}]+x}" ]]; then
      vgs_seen["${vg}"]=1
      if command -v lvs >/dev/null 2>&1; then
        while IFS= read -r lv; do
          lv="$(echo "${lv}" | tr -d ' ')"
          [[ -n "${lv}" ]] || continue
          umount_dev "${lv}"
          swapoff "${lv}" 2>/dev/null || true
        done < <(lvs --noheadings -o lv_path "${vg}" 2>/dev/null || true)
      fi
      echo "    vgchange -an ${vg}"
      vgchange -an "${vg}" 2>/dev/null || true
    fi
  done < <(pvs --noheadings --separator='|' -o pv_name,vg_name 2>/dev/null || true)
fi

# Tear down any remaining DM linear/crypt maps onto this disk. This is the
# path that still works after a failed wipe has already erased LVM metadata.
if command -v dmsetup >/dev/null 2>&1; then
  majmins="$(disk_majmin_list | sort -u | tr '\n' ' ')"
  echo "    disk maj:min: ${majmins}"
  pass=0
  while [[ "${pass}" -lt 8 ]]; do
    removed=0
    while IFS= read -r dm; do
      [[ -n "${dm}" ]] || continue
      table="$(dmsetup table "${dm}" 2>/dev/null || true)"
      [[ -n "${table}" ]] || continue
      for mm in ${majmins}; do
        if echo "${table}" | grep -Eq "(^|[[:space:]])${mm}([[:space:]]|$)"; then
          umount_dev "/dev/mapper/${dm}"
          echo "    dmsetup remove ${dm} (table uses ${mm})"
          dmsetup remove -f "${dm}" 2>/dev/null || true
          removed=1
          break
        fi
      done
    done < <(dmsetup ls 2>/dev/null | awk '{ print $1 }')
    if [[ "${removed}" -eq 0 ]]; then
      break
    fi
    pass=$((pass + 1))
  done
fi

# Drop leftover filesystem signatures on partitions so udev/LVM cannot
# immediately reactivate the old VG once the event queue is running again.
if [[ "${KEEP_SIGNATURES}" -eq 0 ]] && command -v wipefs >/dev/null 2>&1; then
  while IFS= read -r path; do
    [[ -n "${path}" && "${path}" != "${DISK}" ]] || continue
    wipefs -a "${path}" 2>/dev/null || true
  done < <(lsblk -nr -o PATH "${DISK}" 2>/dev/null)
fi

udevadm settle -t 5 2>/dev/null || true
echo "==> ${DISK} is released"
