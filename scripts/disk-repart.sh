#!/usr/bin/env bash
# Apply generated systemd-repart definitions to a block device or image file.
#
# Usage:
#   scripts/disk-repart.sh --dry-run /dev/sdX
#   scripts/disk-repart.sh /dev/sdX
#   scripts/disk-repart.sh --wipe /dev/sdX          # destroy existing table
#   scripts/disk-repart.sh --dry-run out/repart-smoke.img
#
# DEVICE may be a block device or a disk image file (systemd-repart loopbacks it).
# Requires: systemd-repart (or nix-shell -p systemd), root for real applies.
#
# On the installer image, set:
#   LEMONSEED_ROOT=/etc/lemonseed
#   LEMONSEED_REPART_DIR=/var/lib/lemonseed/repart.d
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Repo checkout: scripts/ → repo root. Installer image: files live in LEMONSEED_ROOT.
if [[ -n "${LEMONSEED_ROOT:-}" ]]; then
  ROOT="${LEMONSEED_ROOT}"
elif [[ -f "${SCRIPT_DIR}/partitions.yaml" ]]; then
  ROOT="${SCRIPT_DIR}"
else
  ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
fi

REPART_DIR="${LEMONSEED_REPART_DIR:-${ROOT}/out/repart.d}"
PARTITIONS_YAML="${LEMONSEED_PARTITIONS_YAML:-${ROOT}/partitions.yaml}"
GEN_REPART="${LEMONSEED_GEN_REPART:-}"
if [[ -z "${GEN_REPART}" ]]; then
  if [[ -x "${SCRIPT_DIR}/gen-repart.py" ]]; then
    GEN_REPART="${SCRIPT_DIR}/gen-repart.py"
  else
    GEN_REPART="${ROOT}/scripts/gen-repart.py"
  fi
fi

DRY_RUN=0
WIPE=0

usage() {
  echo "usage: $0 [--dry-run] [--wipe] <block-device-or-image>" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --wipe) WIPE=1; shift ;;
    -h|--help) usage ;;
    *) break ;;
  esac
done

TARGET="${1:-}"
[[ -n "${TARGET}" ]] || usage

if [[ ! -d "${REPART_DIR}" ]] || ! compgen -G "${REPART_DIR}/*.conf" >/dev/null; then
  echo "==> generating repart definitions"
  mkdir -p "${REPART_DIR}"
  python3 "${GEN_REPART}" -c "${PARTITIONS_YAML}" -o "${REPART_DIR}"
fi

dry_flag=no
[[ "${DRY_RUN}" -eq 1 ]] && dry_flag=yes
empty_mode=require
[[ "${WIPE}" -eq 1 ]] && empty_mode=force

args=(
  --definitions="${REPART_DIR}"
  --empty="${empty_mode}"
  --dry-run="${dry_flag}"
  --no-pager
  "${TARGET}"
)

RELEASE_SCRIPT="${SCRIPT_DIR}/disk-release.sh"
if [[ ! -f "${RELEASE_SCRIPT}" && -f "${ROOT}/scripts/disk-release.sh" ]]; then
  RELEASE_SCRIPT="${ROOT}/scripts/disk-release.sh"
fi
if [[ ! -f "${RELEASE_SCRIPT}" && -f "${ROOT}/disk-release.sh" ]]; then
  RELEASE_SCRIPT="${ROOT}/disk-release.sh"
fi

udev_queue_stopped=0
start_udev_queue() {
  if [[ "${udev_queue_stopped}" -eq 1 ]]; then
    udevadm control --start-exec-queue 2>/dev/null || true
    udev_queue_stopped=0
  fi
}
stop_udev_queue() {
  if udevadm control --stop-exec-queue 2>/dev/null; then
    udev_queue_stopped=1
  fi
}
trap start_udev_queue EXIT

release_target() {
  [[ "${WIPE}" -eq 1 && -b "${TARGET}" && -f "${RELEASE_SCRIPT}" ]] || return 0
  bash "${RELEASE_SCRIPT}" "${TARGET}"
}

run_repart() {
  echo "==> systemd-repart ${args[*]}"
  if command -v systemd-repart >/dev/null; then
    systemd-repart "${args[@]}"
    return
  fi
  quoted=""
  for a in "${args[@]}"; do
    quoted+=" $(printf '%q' "${a}")"
  done
  nix-shell -p systemd dosfstools util-linux --run "systemd-repart${quoted}"
}

# Freeze udev while we drop leftover LVM maps and rewrite GPT, otherwise
# pvscan can reactivate appliance-user--data and BLKRRPART returns EBUSY.
if [[ "${WIPE}" -eq 1 && -b "${TARGET}" && "${DRY_RUN}" -eq 0 ]]; then
  stop_udev_queue
  release_target
fi

set +e
run_repart
rc=$?
set -e
if [[ "${rc}" -ne 0 && "${WIPE}" -eq 1 && -b "${TARGET}" && "${DRY_RUN}" -eq 0 ]]; then
  echo "==> systemd-repart failed (rc=${rc}); releasing holders and retrying once"
  release_target
  run_repart
  rc=$?
fi
start_udev_queue
trap - EXIT
exit "${rc}"
