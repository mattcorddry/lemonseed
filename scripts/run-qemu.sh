#!/usr/bin/env bash
# QEMU smoke boot: kernel + initrd + squashfs root + user-data disk.
#
# Requires the appliance-qemu profile (root=/dev/vda, appliance.storage=/dev/vdb).
# Prefer: make run-qemu
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v nix >/dev/null 2>&1; then
  if [[ -x /nix/var/nix/profiles/default/bin/nix ]]; then
    export PATH="/nix/var/nix/profiles/default/bin:${PATH}"
  fi
fi

ARTIFACT_DIR="${1:-${ROOT}/out/latest-qemu}"
USERDATA_IMG="${QEMU_USERDATA_IMG:-${ROOT}/out/qemu-userdata.img}"
USERDATA_SIZE="${QEMU_USERDATA_SIZE:-8G}"

if [[ ! -L "${ARTIFACT_DIR}" && ! -d "${ARTIFACT_DIR}" ]]; then
  echo "No QEMU artifacts at ${ARTIFACT_DIR}" >&2
  echo "Run: make run-qemu   (or: make build-qemu)" >&2
  exit 1
fi

MANIFEST="${ARTIFACT_DIR}/manifest.json"
if [[ -f "${MANIFEST}" ]]; then
  profile="$(sed -n 's/.*"profile":[[:space:]]*"\([^"]*\)".*/\1/p' "${MANIFEST}" | head -1)"
  if [[ "${profile}" != "appliance-qemu" ]]; then
    echo "Refusing to boot profile '${profile}' in QEMU." >&2
    echo "Production images expect PARTLABEL=root-a (set at install time)." >&2
    echo "Use: make run-qemu" >&2
    exit 1
  fi
fi

VMLINUZ="${ARTIFACT_DIR}/vmlinuz"
SQUASHFS="${ARTIFACT_DIR}/root.squashfs"
INITRD="${ARTIFACT_DIR}/initrd.img"

for f in "${VMLINUZ}" "${SQUASHFS}" "${INITRD}"; do
  if [[ ! -f "${f}" ]]; then
    echo "Missing ${f}" >&2
    exit 1
  fi
done

if [[ ! -f "${USERDATA_IMG}" ]]; then
  echo "==> Creating QEMU user-data disk ${USERDATA_IMG} (${USERDATA_SIZE})"
  mkdir -p "$(dirname "${USERDATA_IMG}")"
  truncate -s "${USERDATA_SIZE}" "${USERDATA_IMG}"
fi

QEMU_BIN="${QEMU_BIN:-}"
if [[ -z "${QEMU_BIN}" ]]; then
  if command -v qemu-system-x86_64 >/dev/null 2>&1; then
    QEMU_BIN="qemu-system-x86_64"
  elif [[ -x "${ROOT}/out/.qemu/bin/qemu-system-x86_64" ]]; then
    QEMU_BIN="${ROOT}/out/.qemu/bin/qemu-system-x86_64"
  elif command -v nix >/dev/null 2>&1; then
    echo "==> Resolving qemu via nix flake package"
    nix build --out-link "${ROOT}/out/.qemu" "${ROOT}#qemu"
    QEMU_BIN="${ROOT}/out/.qemu/bin/qemu-system-x86_64"
  fi
fi

if [[ -z "${QEMU_BIN}" || ! -e "${QEMU_BIN}" ]]; then
  echo "qemu-system-x86_64 not found. Install qemu-system-x86, set QEMU_BIN, or ensure nix is available." >&2
  exit 1
fi

MEM="${QEMU_MEM:-2048}"
SMP="${QEMU_SMP:-2}"

echo "==> Booting ${ARTIFACT_DIR} (serial console, Ctrl-A X to exit)"
echo "==> User-data disk: ${USERDATA_IMG}"
exec "${QEMU_BIN}" \
  -machine q35 \
  -m "${MEM}" \
  -smp "${SMP}" \
  -nographic \
  -no-reboot \
  -netdev user,id=net0 \
  -device virtio-net-pci,netdev=net0 \
  -kernel "${VMLINUZ}" \
  -initrd "${INITRD}" \
  -drive "file=${SQUASHFS},format=raw,if=virtio,readonly=on" \
  -drive "file=${USERDATA_IMG},format=raw,if=virtio,cache=writeback" \
  -append "console=ttyS0,115200n8 root=/dev/vda rootfstype=squashfs ro init=/init appliance.storage=/dev/vdb"
