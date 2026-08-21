#!/usr/bin/env bash
# Loopback smoke test for M3: partitions.yaml → systemd-repart → GPT layout.
#
# Creates a sparse disk image, applies repart definitions, and verifies
# partition count, labels, types, and approximate sizes.
#
# Usage:
#   sudo scripts/repart-smoke-test.sh              # default 20G image
#   sudo scripts/repart-smoke-test.sh --size 32G
#   sudo scripts/repart-smoke-test.sh --keep        # leave out/repart-smoke.img
#
# Requires root: systemd-repart uses a loop device for image files.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPART_DIR="${ROOT}/out/repart.d"
IMAGE="${ROOT}/out/repart-smoke.img"
IMAGE_SIZE="20G"
KEEP=0
LOOP=""

usage() {
  cat <<'EOF'
usage: repart-smoke-test.sh [--size SIZE] [--keep]

  --size SIZE   Loopback image size (default: 20G)
  --keep        Do not delete out/repart-smoke.img on success
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --size) IMAGE_SIZE="${2:-}"; shift 2 ;;
    --keep) KEEP=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "repart-smoke-test: run as root (sudo make repart-test)" >&2
  exit 1
fi

export PATH="/nix/var/nix/profiles/default/bin:${PATH}"

# Detach every loop currently backed by this image (systemd-repart and our
# verify step both create them; failures can leave them behind).
detach_image_loops() {
  local img="$1"
  local loops loop
  [[ -e "${img}" ]] || return 0
  loops="$(losetup -j "${img}" -n -O NAME 2>/dev/null || true)"
  [[ -n "${loops}" ]] || return 0
  while IFS= read -r loop; do
    [[ -n "${loop}" ]] || continue
    # Drop partition mappings first so -d is not EBUSY.
    if command -v partx >/dev/null; then
      partx -d "${loop}" 2>/dev/null || true
    fi
    losetup -d "${loop}" 2>/dev/null || true
  done <<<"${loops}"
}

cleanup() {
  if [[ -n "${LOOP}" ]]; then
    if command -v partx >/dev/null; then
      partx -d "${LOOP}" 2>/dev/null || true
    fi
    losetup -d "${LOOP}" 2>/dev/null || true
    LOOP=""
  fi
  detach_image_loops "${IMAGE}"
}
trap cleanup EXIT

# Always use nixpkgs tools so mkfs.vfat (dosfstools) is present even when the
# host has systemd-repart but not the format helpers.
run_repart() {
  local mode="${1:-apply}"
  local -a args=(
    --definitions="${REPART_DIR}"
    --empty=require
  )
  if [[ "${mode}" == "dry" ]]; then
    args+=(--dry-run=yes)
  else
    args+=(--dry-run=no)
  fi

  local quoted="" a
  for a in "${args[@]}" "${IMAGE}"; do
    quoted+=" $(printf '%q' "${a}")"
  done
  nix-shell -p systemd dosfstools util-linux --run "systemd-repart${quoted}"
}

sgdisk_p() {
  if command -v sgdisk >/dev/null; then
    sgdisk -p "${IMAGE}"
  else
    nix-shell -p gptfdisk --run "sgdisk -p $(printf '%q' "${IMAGE}")"
  fi
}

bytes_human() {
  numfmt --to=iec-i --suffix=B "$1" 2>/dev/null || echo "${1}B"
}

approx_eq() {
  local a="$1" b="$2"
  local diff=$(( a > b ? a - b : b - a ))
  local tol=$(( b / 100 + 1048576 ))
  [[ "${diff}" -le "${tol}" ]]
}

echo "==> generating repart definitions"
"${ROOT}/scripts/gen-repart.py" -o "${REPART_DIR}"

echo "==> preparing sparse image ${IMAGE} (${IMAGE_SIZE})"
mkdir -p "$(dirname "${IMAGE}")"
detach_image_loops "${IMAGE}"
rm -f "${IMAGE}"
truncate -s "${IMAGE_SIZE}" "${IMAGE}"

echo "==> dry-run"
run_repart dry

echo "==> applying layout"
run_repart apply

echo "==> partition table"
sgdisk_p

echo "==> attaching verify loop"
LOOP="$(losetup -f --show --partscan --read-only "${IMAGE}")"
# Give udev/partscan a moment to create /dev/loopNp*
sleep 0.5
udevadm settle 2>/dev/null || true

echo "==> lsblk"
lsblk -o NAME,SIZE,TYPE,FSTYPE,PARTLABEL,PARTTYPE "${LOOP}"

fail=0
declare -A PARTS=()
# Walk partition nodes (loopXpN), not the parent loop — avoids empty PARTLABEL rows
# and multi-column lsblk parsing quirks (IFS=/raw empty fields).
shopt -s nullglob
for path in "${LOOP}"p*; do
  label="$(lsblk -nr -o PARTLABEL "${path}" | head -1)"
  label="${label//$'\r'/}"
  if [[ -z "${label}" ]]; then
    continue
  fi
  PARTS["${label}"]="${path}"
done
shopt -u nullglob

if [[ ${#PARTS[@]} -eq 0 ]]; then
  echo "FAIL: no partitions with PARTLABEL found under ${LOOP}" >&2
  fail=1
fi

for want in ESP root-a root-b lemonseed-user-data; do
  if [[ -z "${PARTS[${want}]:-}" ]]; then
    echo "FAIL: missing partition PARTLABEL=${want}" >&2
    fail=1
  fi
done

if [[ "${fail}" -eq 0 ]]; then
  esp="${PARTS[ESP]}"
  root_a="${PARTS[root-a]}"
  root_b="${PARTS[root-b]}"
  user_data="${PARTS[lemonseed-user-data]}"

  esp_b="$(blockdev --getsize64 "${esp}")"
  ra_b="$(blockdev --getsize64 "${root_a}")"
  rb_b="$(blockdev --getsize64 "${root_b}")"
  ud_b="$(blockdev --getsize64 "${user_data}")"

  one_g=$((1024 ** 3))
  four_g=$((4 * 1024 ** 3))

  if approx_eq "${esp_b}" "${one_g}"; then
    echo "OK: ESP size ~1G ($(bytes_human "${esp_b}"))"
  else
    echo "FAIL: ESP size $(bytes_human "${esp_b}")" >&2
    fail=1
  fi

  if approx_eq "${ra_b}" "${four_g}"; then
    echo "OK: root-a size ~4G ($(bytes_human "${ra_b}"))"
  else
    echo "FAIL: root-a size $(bytes_human "${ra_b}")" >&2
    fail=1
  fi

  if approx_eq "${rb_b}" "${four_g}"; then
    echo "OK: root-b size ~4G ($(bytes_human "${rb_b}"))"
  else
    echo "FAIL: root-b size $(bytes_human "${rb_b}")" >&2
    fail=1
  fi

  if [[ "${ud_b}" -gt $((5 * 1024 ** 3)) ]]; then
    echo "OK: user-data grow partition ($(bytes_human "${ud_b}"))"
  else
    echo "FAIL: user-data too small ($(bytes_human "${ud_b}"))" >&2
    fail=1
  fi

  esp_fstype="$(blkid -s TYPE -o value "${esp}" 2>/dev/null || true)"
  if [[ "${esp_fstype}" == "vfat" ]]; then
    echo "OK: ESP filesystem vfat"
  else
    echo "FAIL: ESP filesystem '${esp_fstype}' (expected vfat)" >&2
    fail=1
  fi

  ud_pttype="$(lsblk -rno PARTTYPE "${user_data}" | head -1 | tr '[:upper:]' '[:lower:]')"
  if [[ "${ud_pttype}" == *"e6d6d379"* ]]; then
    echo "OK: user-data GPT type LVM (${ud_pttype})"
  else
    echo "FAIL: user-data PARTTYPE=${ud_pttype} (expected LVM GUID)" >&2
    fail=1
  fi
fi

cleanup
trap - EXIT

if [[ "${fail}" -ne 0 ]]; then
  echo "==> repart smoke test FAILED" >&2
  exit 1
fi

echo "==> repart smoke test PASSED"
if [[ "${KEEP}" -eq 0 ]]; then
  rm -f "${IMAGE}"
else
  echo "    kept image: ${IMAGE}"
fi
