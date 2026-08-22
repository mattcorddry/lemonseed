#!/usr/bin/env bash
# Boot the QEMU appliance profile and poll public HTTP APIs (CPU path).
#
# Requires: out/latest-qemu artifacts (make build-qemu), qemu-system-x86_64
# Does not require root unless QEMU_USERDATA_IMG is not writable.
#
#   make test-qemu
#   QEMU_API_TIMEOUT=420 make test-qemu
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT_DIR="${QEMU_ARTIFACT_DIR:-${ROOT}/out/latest-qemu}"
USERDATA_IMG="${QEMU_USERDATA_IMG:-${ROOT}/out/qemu-userdata.img}"
USERDATA_SIZE="${QEMU_USERDATA_SIZE:-8G}"
HOST_PORT="${QEMU_API_HOST_PORT:-19080}"
TIMEOUT="${QEMU_API_TIMEOUT:-300}"
PIDFILE="${ROOT}/out/qemu-api-smoke.pid"
LOG="${ROOT}/out/qemu-api-smoke.log}"
MEM="${QEMU_MEM:-2048}"
SMP="${QEMU_SMP:-2}"

cleanup() {
  if [[ -f "${PIDFILE}" ]]; then
    pid="$(cat "${PIDFILE}")"
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
    rm -f "${PIDFILE}"
  fi
}
trap cleanup EXIT

deadline=$((SECONDS + TIMEOUT))

usage() {
  cat <<EOF
usage: $(basename "$0") [--build]

  --build   Run make build-qemu when artifacts are missing

Polls http://127.0.0.1:${HOST_PORT}/healthz and /api/status after boot.
EOF
}

BUILD=0
if [[ "${1:-}" == "--build" ]]; then
  BUILD=1
elif [[ -n "${1:-}" ]]; then
  usage >&2
  exit 2
fi

if [[ ! -f "${ARTIFACT_DIR}/vmlinuz" ]]; then
  if [[ "${BUILD}" -eq 1 ]]; then
    echo "==> Building QEMU artifacts"
    make -C "${ROOT}" build-qemu
  else
    echo "Missing ${ARTIFACT_DIR}/vmlinuz — run: make build-qemu (or pass --build)" >&2
    exit 1
  fi
fi

VMLINUZ="${ARTIFACT_DIR}/vmlinuz"
SQUASHFS="${ARTIFACT_DIR}/root.squashfs"
INITRD="${ARTIFACT_DIR}/initrd.img"
for f in "${VMLINUZ}" "${SQUASHFS}" "${INITRD}"; do
  [[ -f "${f}" ]] || { echo "Missing ${f}" >&2; exit 1; }
done

if [[ ! -f "${USERDATA_IMG}" ]]; then
  echo "==> Creating ${USERDATA_IMG} (${USERDATA_SIZE})"
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
[[ -n "${QEMU_BIN}" && -e "${QEMU_BIN}" ]] || {
  echo "qemu-system-x86_64 not found" >&2
  exit 1
}

mkdir -p "$(dirname "${LOG}")"
rm -f "${PIDFILE}" "${LOG}"

echo "==> Starting QEMU (host API http://127.0.0.1:${HOST_PORT}/ → guest :80)"
"${QEMU_BIN}" \
  -machine q35 \
  -m "${MEM}" \
  -smp "${SMP}" \
  -display none \
  -serial none \
  -no-reboot \
  -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${HOST_PORT}-:80" \
  -device virtio-net-pci,netdev=net0 \
  -kernel "${VMLINUZ}" \
  -initrd "${INITRD}" \
  -drive "file=${SQUASHFS},format=raw,if=virtio,readonly=on" \
  -drive "file=${USERDATA_IMG},format=raw,if=virtio,cache=writeback" \
  -append "console=ttyS0,115200n8 root=/dev/vda rootfstype=squashfs ro init=/init appliance.storage=/dev/vdb" \
  >"${LOG}" 2>&1 &
echo $! >"${PIDFILE}"

poll_http() {
  local path="$1"
  local max_time="$2"
  curl -fsS --max-time "${max_time}" "http://127.0.0.1:${HOST_PORT}${path}"
}

echo "==> Waiting for /healthz (timeout ${TIMEOUT}s)"
while (( SECONDS < deadline )); do
  if poll_http "/healthz" 2 >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
    echo "QEMU exited early; tail ${LOG}:" >&2
    tail -40 "${LOG}" >&2 || true
    exit 1
  fi
  sleep 2
done

if ! poll_http "/healthz" 2 >/dev/null 2>&1; then
  echo "Timed out waiting for /healthz" >&2
  tail -40 "${LOG}" >&2 || true
  exit 1
fi

echo "==> Waiting for /api/status"
status_deadline=$((SECONDS + 120))
status_json=""
while (( SECONDS < status_deadline )); do
  if status_json="$(poll_http "/api/status" 30 2>/dev/null)"; then
    break
  fi
  sleep 3
done

if [[ -z "${status_json}" ]]; then
  echo "Timed out waiting for /api/status" >&2
  tail -40 "${LOG}" >&2 || true
  exit 1
fi

echo "${status_json}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
assert data.get("status") == "ok", data
assert data.get("role") == "control-plane", data
lemond = (data.get("lemond") or {}).get("state")
print(f"OK: role=control-plane lemond={lemond!r}")
'

echo "==> Checking /api/boot"
boot_json="$(poll_http "/api/boot" 30)"
echo "${boot_json}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
assert 'version' in data, data
assert 'lemond' in data, data
print('OK: version=%r' % data.get('version'))
"

echo "==> QEMU API smoke test PASSED"
