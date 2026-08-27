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
USERDATA_SIZE="${QEMU_USERDATA_SIZE:-32G}"
HOST_PORT="${QEMU_API_HOST_PORT:-19080}"
TIMEOUT="${QEMU_API_TIMEOUT:-1200}"
PIDFILE="${ROOT}/out/qemu-api-smoke.pid"
LOG="${ROOT}/out/qemu-api-smoke.log"
MEM="${QEMU_MEM:-8192}"
SMP="${QEMU_SMP:-2}"
VIRTFS_DIR="${QEMU_OWUI_VIRTFS:-${ROOT}/out/qemu-owui-virtfs}"

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

# Expose the pinned OWUI image tar to the guest via virtio-fs (avoids ~5GB in squashfs).
OWUI_TAR="${QEMU_OWUI_TAR:-}"
if [[ -z "${OWUI_TAR}" ]] && command -v nix >/dev/null 2>&1; then
  OWUI_TAR="$(nix build --no-link --print-out-paths "${ROOT}#qemu-owui-image" 2>/dev/null || true)"
fi
if [[ -z "${OWUI_TAR}" ]]; then
  OWUI_TAR="$(find /nix/store -maxdepth 1 -name '*-docker-image-ghcr.io-open-webui-open-webui-v0.11.0.tar' 2>/dev/null | head -1 || true)"
fi
VIRTFS_ARGS=()
if [[ -n "${OWUI_TAR}" && -f "${OWUI_TAR}" ]]; then
  mkdir -p "${VIRTFS_DIR}"
  rm -f "${VIRTFS_DIR}/open-webui-v0.11.0.tar"
  # Prefer a real inode in the share — guest 9p cannot follow host symlinks
  # that point outside the exported tree.
  if ln "${OWUI_TAR}" "${VIRTFS_DIR}/open-webui-v0.11.0.tar" 2>/dev/null; then
    VIRTFS_PATH="${VIRTFS_DIR}"
  else
    VIRTFS_PATH="$(dirname "${OWUI_TAR}")"
  fi
  VIRTFS_ARGS=(-virtfs "local,path=${VIRTFS_PATH},mount_tag=lemonseedowui,security_model=none,readonly=on")
  echo "==> virtio 9p OWUI export: ${OWUI_TAR} (share ${VIRTFS_PATH})"
else
  echo "==> No local OWUI docker tar found; guest may pull from registry (slow under TCG)" >&2
fi

ACCEL_ARGS=()
if [[ -r /dev/kvm && -w /dev/kvm ]]; then
  ACCEL_ARGS=(-accel kvm)
  echo "==> Using KVM acceleration"
else
  echo "==> /dev/kvm not usable; falling back to TCG (slow)"
fi

echo "==> Starting QEMU (host API http://127.0.0.1:${HOST_PORT}/ → guest :80)"
"${QEMU_BIN}" \
  -machine q35 \
  "${ACCEL_ARGS[@]}" \
  "${VIRTFS_ARGS[@]}" \
  -m "${MEM}" \
  -smp "${SMP}" \
  -display none \
  -serial file:"${LOG}" \
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

echo "==> Checking /api/data (Postgres + Valkey + provisioning)"
# Docker pulls + first-boot init are slow under TCG; use remaining budget.
data_deadline=$((deadline))
data_json=""
while (( SECONDS < data_deadline )); do
  if candidate="$(poll_http "/api/data" 30 2>/dev/null)"; then
    ready="$(echo "${candidate}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
pg = (d.get("postgres") or {}).get("state")
vk = (d.get("valkey") or {}).get("state")
prov = d.get("provisioning") or {}
prov_state = prov.get("state")
prov_detail = prov.get("error") or prov.get("reason") or ""
print("yes" if pg == "ready" and vk == "ready" and prov_state == "ready" else "no")
print(f"waiting: postgres={pg!r} valkey={vk!r} provisioning={prov_state!r} {prov_detail!r}", file=sys.stderr)
' 2>/tmp/qemu-data-wait.err || true)"
    cat /tmp/qemu-data-wait.err >&2 || true
    if [[ "${ready}" == "yes" ]]; then
      data_json="${candidate}"
      break
    fi
  fi
  if ! kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
    echo "QEMU exited during /api/data wait; tail ${LOG}:" >&2
    tail -80 "${LOG}" >&2 || true
    exit 1
  fi
  sleep 5
done

if [[ -z "${data_json}" ]]; then
  echo "Timed out waiting for /api/data provisioning ready" >&2
  curl -fsS --max-time 5 "http://127.0.0.1:${HOST_PORT}/api/data" >&2 || true
  tail -80 "${LOG}" >&2 || true
  exit 1
fi

echo "${data_json}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
pg = (data.get("postgres") or {}).get("state")
vk = (data.get("valkey") or {}).get("state")
prov = (data.get("provisioning") or {}).get("state")
print(f"OK: postgres={pg!r} valkey={vk!r} provisioning={prov!r}")
assert pg == "ready", data
assert vk == "ready", data
assert prov == "ready", data
'

echo "==> Checking /api/modules (OWUI lifecycle; wait for http_ready)"
# First boot may pull a multi-GB image; use remaining QEMU_API_TIMEOUT budget.
owui_deadline=$((deadline))
owui_ready_json=""
while (( SECONDS < owui_deadline )); do
  modules_json="$(curl -fsS --max-time 5 "http://127.0.0.1:${HOST_PORT}/api/modules" 2>/dev/null || true)"
  if [[ -n "${modules_json}" ]]; then
    set +e
    echo "${modules_json}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
owui = data.get("open_webui") or {}
image = str(owui.get("image") or "")
assert "state" in owui, data
assert owui.get("path") == "/?embed=1", data
assert ":v0.11." in image, ("expected OWUI v0.11.x pin", image, data)
assert ":latest" not in image and ":main" not in image, image
if owui.get("http_ready") is True or owui.get("state") == "ready":
    print("OK: open_webui state=%r image=%r http_ready=%r" % (
        owui.get("state"), image, owui.get("http_ready")))
    sys.exit(0)
print("waiting: state=%r http_ready=%r reason=%r error=%r" % (
    owui.get("state"), owui.get("http_ready"), owui.get("reason") or owui.get("message") or "",
    owui.get("error") or ""),
    file=sys.stderr)
sys.exit(2)
' 2>/tmp/qemu-owui-wait.err
    wait_rc=$?
    set -e
    if [[ "${wait_rc}" -eq 0 ]]; then
      cat /tmp/qemu-owui-wait.err >&2 || true
      owui_ready_json="${modules_json}"
      break
    fi
    if [[ "${wait_rc}" -ne 2 ]]; then
      cat /tmp/qemu-owui-wait.err >&2 || true
      echo "${modules_json}" >&2
      exit 1
    fi
    cat /tmp/qemu-owui-wait.err >&2 || true
  fi
  if ! kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
    echo "QEMU exited during OWUI wait; tail ${LOG}:" >&2
    tail -80 "${LOG}" >&2 || true
    exit 1
  fi
  sleep 10
done

if [[ -z "${owui_ready_json}" ]]; then
  echo "Timed out waiting for Open WebUI http_ready (image pin v0.11.x)" >&2
  curl -fsS --max-time 5 "http://127.0.0.1:${HOST_PORT}/api/modules" >&2 || true
  tail -80 "${LOG}" >&2 || true
  exit 1
fi

echo "==> Probing OWUI SPA asset via nginx (/_app/version.json)"
curl -fsS --max-time 10 "http://127.0.0.1:${HOST_PORT}/_app/version.json" \
  | python3 -c 'import sys; body=sys.stdin.read(200); assert body.strip(), "empty version.json"; print("OK: /_app/version.json (%d bytes)" % len(body))'

echo "==> QEMU API smoke test PASSED"
