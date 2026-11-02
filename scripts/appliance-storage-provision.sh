#!/usr/bin/env bash
# First-boot / every-boot storage provisioner for appliance user-data.
#
# Idempotent: if VG/LV already exist, only ensure btrfs subvolumes (and never mkfs).
# Unlock: if the user-data partition is a LUKS2 container (M8 P7), open it first
# (TPM token, then one Hardware-password prompt, bounded retry — D-E2) and use
# the mapper as the PV. Not-LUKS (pre-P7 plain volume, unencrypted install) and
# blank behave exactly as before.
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
# Test seams (unit tests): the device paths are env-overridable so the whole
# pipeline runs against stubbed tooling without a real mapper node.
LV_PATH="${APPLIANCE_LV_PATH:-/dev/${VG_NAME}/${LV_NAME}}"
MAPPER_PATH="${APPLIANCE_MAPPER_PATH:-/dev/mapper/${VG_NAME}-${LV_NAME}}"
SUBVOLS=(@etc @var)
REQUIRED="${APPLIANCE_STORAGE_REQUIRED:-0}"

# LUKS2 container (M8 P7, ENCRYPTED-USER-DATA.md §3.4): the PV lives inside
# this mapper when the installer created it with the encryption checkbox on.
LUKS_MAPPER="user-data-luks"
LUKS_MAPPER_PATH="/dev/mapper/${LUKS_MAPPER}"
# Failed-unlock UX (D-E2): show the recovery copy for 30 s and reboot; after
# MAX_UNLOCK_ATTEMPTS failed boots, halt. The counter lives on the ESP
# (writable, nothing secret there) and is deleted by the first success.
MAX_UNLOCK_ATTEMPTS=3
RETRY_WAIT_SECONDS="${APPLIANCE_UNLOCK_WAIT:-30}"
ESP_PART="${APPLIANCE_ESP_PART:-/dev/disk/by-partlabel/ESP}"
UNLOCK_COUNTER_REL="grub/unlock-attempts"

log() { echo "appliance-storage: $*"; }

# Logs for everything that runs inside a command substitution (the unlock
# step): stdout there is the return value, so diagnostics go to stderr — the
# unit forwards both to journal+console.
log2() { echo "appliance-storage: $*" >&2; }

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

  # Test seam (unit tests, AGENTS.md): a discovery override so the whole
  # pipeline runs against stubbed tooling without a real block device.
  if [[ -n "${APPLIANCE_STORAGE_DEVICE:-}" ]]; then
    printf '%s\n' "${APPLIANCE_STORAGE_DEVICE}"
    return 0
  fi

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

esp_counter() {
  # read | bump | clear the ESP unlock-attempt counter. "bump" prints the new
  # count, "read" the current one (0 when absent) — each call is one mount,
  # so bump-then-read never interleaves stale copy-backs. Best effort: a box
  # whose ESP cannot be mounted loses only the bounded-retry cap, never the
  # unlock itself.
  local op="$1" mnt counter n="0"
  if [[ ! -e "${ESP_PART}" ]]; then
    printf '0\n'
    return 0
  fi
  mnt="$(mktemp -d)"
  if ! mount -t vfat -o rw,umask=0077 "${ESP_PART}" "${mnt}" 2>/dev/null; then
    rmdir "${mnt}"
    printf '0\n'
    return 0
  fi
  counter="${mnt}/${UNLOCK_COUNTER_REL}"
  case "${op}" in
    read)
      if [[ -f "${counter}" ]]; then
        n="$(tr -dc '0-9' <"${counter}" 2>/dev/null || true)"
      fi
      ;;
    bump)
      mkdir -p "$(dirname "${counter}")"
      if [[ -f "${counter}" ]]; then
        n="$(tr -dc '0-9' <"${counter}" 2>/dev/null || true)"
      fi
      n=$((n + 1))
      printf '%s\n' "${n}" >"${counter}"
      sync
      ;;
    clear)
      rm -f "${counter}"
      ;;
  esac
  umount "${mnt}"
  rm -rf "${mnt}"
  case "${op}" in
    read|bump) printf '%s\n' "${n:-0}" ;;
  esac
}

# A boot that cannot open the volume shows the recovery copy, waits (some
# failures are transient and self-heal on the retry), and reboots; after
# MAX_UNLOCK_ATTEMPTS failed boots it halts on the same message — a perpetual
# reboot loop would look healthy when it is not (D-E2).
unlock_failed_boot() {
  local attempts
  attempts="$(esp_counter bump)"
  log2 "user-data cannot be unlocked; boot the USB installer to recover"
  if [[ "${attempts}" -ge "${MAX_UNLOCK_ATTEMPTS}" ]]; then
    systemctl halt --no-block 2>/dev/null || halt -f
    exit 1
  fi
  sleep "${RETRY_WAIT_SECONDS}"
  systemctl reboot --no-block 2>/dev/null || reboot -f
  exit 1
}

unlock_luks() {
  # $1 = the LUKS2 partition. TPM token first (silent — the unattended-reboot
  # contract of §3.5); then one bounded passphrase prompt for the Hardware
  # password (a keyboard is a fallback, never the only path); then the
  # failed-boot flow. Clears the ESP counter the moment the volume opens.
  # --token-only is cryptsetup's form (its --tpm2-device is a
  # systemd-cryptsetup option, not a cryptsetup one — round-4 bare metal: the
  # invalid option made every boot fall back to the prompt). The token plugin
  # comes from systemd's libdir, found via the unit's LD_LIBRARY_PATH.
  local device="$1" pass reason
  if reason=$(cryptsetup open --type luks2 --token-only "${device}" "${LUKS_MAPPER}" 2>&1); then
    esp_counter clear
    return 0
  fi
  log2 "TPM token unlock failed (${reason}); asking for the Hardware password (one attempt)"
  if ! pass="$(systemd-ask-password --timeout=120 'Hardware password: ' | tr -d '\n')"; then
    log2 "no passphrase entered"
    unlock_failed_boot
  fi
  if [[ -z "${pass}" ]]; then
    unlock_failed_boot
  fi
  if ! printf '%s' "${pass}" | cryptsetup open --type luks2 --key-file=- "${device}" "${LUKS_MAPPER}" 2>/dev/null; then
    log2 "Hardware password did not open the volume"
    unlock_failed_boot
  fi
  esp_counter clear
}

unlock_storage_device() {
  # $1 = the discovered user-data partition; sets PV_DEVICE to the device the
  # PV should be created on. Three-way branch (§3.4): LUKS2 -> open it; a
  # signature that is not LUKS (pre-P7 plain volume, unencrypted install) ->
  # today's path; blank -> plain provisioning (the installer creates the
  # container itself when the checkbox is on, so blank here means an
  # unencrypted install). Corrupted LUKS metadata fails through the same
  # legible path — never a reformat.
  local device="$1"
  if cryptsetup isLuks "${device}" 2>/dev/null; then
    if [[ -e "${LUKS_MAPPER_PATH}" ]]; then
      log2 "LUKS container already open at ${LUKS_MAPPER_PATH}"
    else
      unlock_luks "${device}"
    fi
    PV_DEVICE="${LUKS_MAPPER_PATH}"
  else
    PV_DEVICE="${device}"
  fi
}

udevadm settle -t 10 2>/dev/null || true

device=""
if device="$(find_storage_device)"; then
  log "storage device ${device}"
else
  die_or_skip "no user-data device found (partlabel or appliance.storage=)"
fi

# LUKS2 unlock (M8 P7): the VG lives inside the mapper, so the container
# must be open before any LVM scan — including the existing-LV check below.
# The three-way branch also tells blank (unencrypted install) from plain.
unlock_storage_device "${device}"

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

log "provisioning on ${PV_DEVICE}"

if ! pvs --noheadings "${PV_DEVICE}" >/dev/null 2>&1; then
  log "pvcreate ${PV_DEVICE}"
  pvcreate -ff -y "${PV_DEVICE}"
fi

if ! vgs --noheadings "${VG_NAME}" >/dev/null 2>&1; then
  log "vgcreate ${VG_NAME}"
  vgcreate "${VG_NAME}" "${PV_DEVICE}"
else
  pv_vg="$(pvs --noheadings -o vg_name --select "pv_name=${PV_DEVICE}" 2>/dev/null | tr -d ' ')"
  if [[ -n "${pv_vg}" && "${pv_vg}" != "${VG_NAME}" ]]; then
    log "PV ${PV_DEVICE} already belongs to VG ${pv_vg}; refusing to vgextend"
    exit 1
  fi
  if [[ -z "${pv_vg}" ]]; then
    log "vgextend ${VG_NAME} ${PV_DEVICE}"
    vgextend "${VG_NAME}" "${PV_DEVICE}"
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
