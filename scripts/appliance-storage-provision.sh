#!/usr/bin/env bash
# First-boot / every-boot storage provisioner for appliance user-data.
#
# Idempotent: if VG/LV already exist, only ensure btrfs subvolumes (and never mkfs).
# Creates: PV (if needed) → VG "appliance" → LV "user-data" → btrfs → @etc, @var
#
# Device discovery (first match wins):
#   1. /dev/disk/by-partlabel/lemonseed-user-data (installer GPT partition)
#   2. lsblk PARTLABEL=lemonseed-user-data fallback
#   3. appliance.storage=<path> on kernel cmdline (QEMU / overrides)
#
# Set APPLIANCE_STORAGE_REQUIRED=1 (initrd/stage-2) to fail instead of skipping
# when no device is found — /etc and /var cannot mount without this volume.
set -euo pipefail

VG_NAME="appliance"
LV_NAME="user-data"
STORAGE_TAG="lemonseed.storage=v1"
FS_LABEL="lemonseed-user-data"
IDENTITY_FILE=".lemonseed-storage.json"
LV_PATH="/dev/${VG_NAME}/${LV_NAME}"
MAPPER_PATH="/dev/mapper/${VG_NAME}-${LV_NAME}"
SUBVOLS=(@etc @var)
REQUIRED="${APPLIANCE_STORAGE_REQUIRED:-0}"

log() { echo "appliance-storage: $*"; }

die_or_skip() {
  log "$*"
  if [[ "${REQUIRED}" == "1" ]]; then
    exit 1
  fi
  exit 0
}

partlabel_via_lsblk() {
  # Fallback when by-partlabel/ is not populated yet.
  lsblk -nr -o PARTLABEL,PATH 2>/dev/null |
    awk '$1 == "lemonseed-user-data" { print $2; exit }'
}

find_storage_device() {
  local device=""
  local arg n candidate

  if [[ -e /dev/disk/by-partlabel/lemonseed-user-data ]]; then
    device="$(readlink -f /dev/disk/by-partlabel/lemonseed-user-data)"
  elif candidate="$(partlabel_via_lsblk)"; [[ -n "${candidate}" ]]; then
    device="${candidate}"
  elif [[ -r /proc/cmdline ]]; then
    # shellcheck disable=SC2013
    for arg in $(cat /proc/cmdline); do
      case "${arg}" in
        appliance.storage=*)
          device="${arg#appliance.storage=}"
          ;;
      esac
    done
  fi

  if [[ -z "${device}" ]]; then
    return 1
  fi

  # Virtio / USB / udev can lag; wait for the block node.
  n=0
  while [[ "${n}" -lt 120 ]]; do
    if [[ -e /dev/disk/by-partlabel/lemonseed-user-data ]]; then
      device="$(readlink -f /dev/disk/by-partlabel/lemonseed-user-data)"
    fi
    if [[ -b "${device}" ]]; then
      printf '%s\n' "${device}"
      return 0
    fi
    # Re-probe lsblk periodically.
    if (( n % 4 == 0 )); then
      candidate="$(partlabel_via_lsblk || true)"
      if [[ -n "${candidate}" ]]; then
        device="${candidate}"
      fi
      udevadm settle -t 5 2>/dev/null || true
    fi
    sleep 0.5
    n=$((n + 1))
  done
  log "timed out waiting for ${device}"
  return 1
}

ensure_subvolumes() {
  local dev="$1"
  local mnt
  mnt="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "umount '${mnt}' 2>/dev/null || true; rmdir '${mnt}' 2>/dev/null || true" RETURN
  mount -t btrfs -o rw "${dev}" "${mnt}"
  local sv
  for sv in "${SUBVOLS[@]}"; do
    if [[ ! -d "${mnt}/${sv}" ]]; then
      log "creating subvolume ${sv}"
      btrfs subvolume create "${mnt}/${sv}"
    fi
  done
  umount "${mnt}"
  rmdir "${mnt}"
  trap - RETURN
}

ensure_identity() {
  local dev="$1"
  local mnt uuid
  mnt="$(mktemp -d)"
  # shellcheck disable=SC2064
  trap "umount '${mnt}' 2>/dev/null || true; rmdir '${mnt}' 2>/dev/null || true" RETURN
  mount -t btrfs -o rw "${dev}" "${mnt}"
  if [[ ! -f "${mnt}/${IDENTITY_FILE}" ]]; then
    uuid="$(cat /proc/sys/kernel/random/uuid)"
    cat >"${mnt}/${IDENTITY_FILE}.tmp" <<EOF
{"format":"lemonseed-storage-v1","storage_uuid":"${uuid}","vg":"${VG_NAME}","lv":"${LV_NAME}"}
EOF
    mv "${mnt}/${IDENTITY_FILE}.tmp" "${mnt}/${IDENTITY_FILE}"
  fi
  umount "${mnt}"
  rmdir "${mnt}"
  trap - RETURN
}

lv_device() {
  if [[ -e "${LV_PATH}" ]]; then
    printf '%s\n' "${LV_PATH}"
  elif [[ -e "${MAPPER_PATH}" ]]; then
    printf '%s\n' "${MAPPER_PATH}"
  else
    return 1
  fi
}

detect_btrfs() {
  local dev="$1"
  local fstype
  fstype="$(blkid -o value -s TYPE "${dev}" 2>/dev/null || true)"
  if [[ "${fstype}" == "btrfs" ]]; then
    return 0
  fi
  if btrfs filesystem show "${dev}" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

udevadm settle -t 10 2>/dev/null || true

device=""
if device="$(find_storage_device)"; then
  log "storage device ${device}"
else
  die_or_skip "no user-data device found (partlabel or appliance.storage=)"
fi

if command -v vgchange >/dev/null 2>&1; then
  vgchange -ay "${VG_NAME}" 2>/dev/null || vgchange -ay 2>/dev/null || true
fi

if lv_dev="$(lv_device)"; then
  log "LV already present at ${lv_dev}"
  if findmnt -n -t btrfs /etc >/dev/null 2>&1; then
    log "ok (existing volume, mounts active)"
    exit 0
  fi
  ensure_subvolumes "${lv_dev}"
  ensure_identity "${lv_dev}"
  log "ok (existing volume)"
  exit 0
fi

log "provisioning on ${device}"

if ! pvs --noheadings "${device}" >/dev/null 2>&1; then
  log "pvcreate ${device}"
  pvcreate -ff -y "${device}"
fi

if ! vgs --noheadings "${VG_NAME}" >/dev/null 2>&1; then
  log "vgcreate ${VG_NAME}"
  vgcreate "${VG_NAME}" "${device}"
else
  pv_vg="$(pvs --noheadings -o vg_name --select "pv_name=${device}" 2>/dev/null | tr -d ' ')"
  if [[ -n "${pv_vg}" && "${pv_vg}" != "${VG_NAME}" ]]; then
    log "PV ${device} already belongs to VG ${pv_vg}; refusing to vgextend"
    exit 1
  fi
  if [[ -z "${pv_vg}" ]]; then
    log "vgextend ${VG_NAME} ${device}"
    vgextend "${VG_NAME}" "${device}"
  fi
fi
vgchange --addtag "${STORAGE_TAG}" "${VG_NAME}" 2>/dev/null || true

if ! lvs --noheadings "${VG_NAME}/${LV_NAME}" >/dev/null 2>&1; then
  log "lvcreate ${VG_NAME}/${LV_NAME}"
  lvcreate -y -l 100%FREE -n "${LV_NAME}" "${VG_NAME}"
fi
lvchange --addtag "${STORAGE_TAG}" "${VG_NAME}/${LV_NAME}" 2>/dev/null || true

vgchange -ay "${VG_NAME}"
# Wait for udev to create the mapper node the mounts expect.
udevadm settle -t 10 2>/dev/null || true
n=0
while ! lv_dev="$(lv_device)"; do
  if [[ "${n}" -ge 60 ]]; then
    log "LV created but mapper node ${MAPPER_PATH} did not appear"
    exit 1
  fi
  sleep 0.5
  n=$((n + 1))
done

if detect_btrfs "${lv_dev}"; then
  log "btrfs already present on ${lv_dev}"
else
  log "mkfs.btrfs ${lv_dev}"
  mkfs.btrfs -f -L "${FS_LABEL}" "${lv_dev}"
fi

ensure_subvolumes "${lv_dev}"
ensure_identity "${lv_dev}"
udevadm settle -t 5 2>/dev/null || true
log "ok (provisioned ${lv_dev})"
