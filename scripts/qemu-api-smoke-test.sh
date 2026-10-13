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
# 16 GiB: large enough for the CPU-path e2e (QEMU_E2E_CHAT=1) to select + load
# Gemma E4B (needs ram >= min_vram 8 + min_ram 6 = 14 GiB; the largest Gemma
# that fits at 16 GiB CPU). 8 GiB would leave the planner in not-ready, which
# is what the default (non-e2e) run asserts.
MEM="${QEMU_MEM:-16384}"
SMP="${QEMU_SMP:-2}"
VIRTFS_DIR="${QEMU_OWUI_VIRTFS:-${ROOT}/out/qemu-owui-virtfs}"
# Opt-in full model-load e2e: POST /_lemonseed/api/models to pick Gemma, then poll
# /api/chat until the CP pulls + loads the model and reports state=ready.
# Off by default to keep the smoke test fast; the model is cached on the
# persistent userdata disk after the first (cold) run.
E2E_CHAT="${QEMU_E2E_CHAT:-0}"

cleanup() {
  if [[ -f "${PIDFILE}" ]]; then
    pid="$(cat "${PIDFILE}")"
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
    rm -f "${PIDFILE}"
  fi
  # Session cookie jar (set once the first-admin bootstrap runs).
  if [[ -n "${COOKIE_JAR:-}" && -f "${COOKIE_JAR}" ]]; then
    rm -f "${COOKIE_JAR}"
  fi
}
trap cleanup EXIT

deadline=$((SECONDS + TIMEOUT))

# Nix may not be on PATH in non-login shells (single-user installs live in
# ~/.nix-profile); the flake-based qemu/OWUI-tar resolution below needs it.
if ! command -v nix >/dev/null 2>&1; then
  for nix_bin in /nix/var/nix/profiles/default/bin/nix "${HOME}/.nix-profile/bin/nix"; do
    if [[ -x "${nix_bin}" ]]; then
      export PATH="$(dirname "${nix_bin}"):${PATH}"
      break
    fi
  done
fi

usage() {
  cat <<EOF
usage: $(basename "$0") [--build]

  --build   Run make build-qemu when artifacts are missing
  --debug   Run qemu with an interactive console on ttyS0

Polls http://127.0.0.1:${HOST_PORT}/healthz and /api/status after boot, then
exercises the M6.2 edge-auth flow (first admin via /setup, CP-native
sign-in at /login, gated / and /settings/ with the session cookie).
EOF
}

BUILD=0
DEBUG=0
if [[ "${1:-}" == "--build" ]]; then
  BUILD=1
elif [[ "${1:-}" == "--debug" ]]; then
  DEBUG=1
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
  if QBPATH="$( which qemu-system-x86_64 )"; then
    QEMU_BIN="$QBPATH"
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
  # Fallback for a warm store without a nix CLI. Match any tag: the version
  # lives in control-plane/container-images.json, not in this script.
  OWUI_TAR="$(find /nix/store -maxdepth 1 -name '*-docker-image-ghcr.io-open-webui-open-webui-*.tar' 2>/dev/null | head -1 || true)"
fi
VIRTFS_ARGS=()
if [[ -n "${OWUI_TAR}" && -f "${OWUI_TAR}" ]]; then
  mkdir -p "${VIRTFS_DIR}"
  rm -f "${VIRTFS_DIR}/open-webui-v0.11.0.tar" "${VIRTFS_DIR}/open-webui.tar"
  # Prefer a real inode in the share — guest 9p cannot follow host symlinks
  # that point outside the exported tree.
  if ln "${OWUI_TAR}" "${VIRTFS_DIR}/open-webui.tar" 2>/dev/null; then
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

SERIAL="file:${LOG}"
if [[ "$DEBUG" == 1 ]] ; then
    SERIAL="mon:stdio"
    echo "==> Enabling debug mode: interactive serial console"
else
    echo "==> Serial output will be redirected to $LOG"
fi

# set all arguments into a QEMU_ARGS array so that they can
# Quote every argument in the generated command line: the array elements
# (virtfs paths, netdev) contain spaces.
#
# AGENTS: Do not modify this logic, it has been tested and you
# often break it.
#
# Notes:
# - must pass cpu=host, owui container requires at least X86_V2 caps
# - owui docker img tarball passed in via VIRTFS, read by systemd service
#
QEMU_ARGS=()
for arg in \
  -machine q35 \
  ${ACCEL_ARGS[@]} \
  ${VIRTFS_ARGS[@]} \
  -m "${MEM}" \
  -smp "${SMP}" \
  -display none \
  -cpu host \
  -serial "${SERIAL}" \
  -no-reboot \
  -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:${HOST_PORT}-:80" \
  -device virtio-net-pci,netdev=net0 \
  -kernel "${VMLINUZ}" \
  -initrd "${INITRD}" \
  -drive "file=${SQUASHFS},format=raw,if=virtio,readonly=on" \
  -drive "file=${USERDATA_IMG},format=raw,if=virtio,cache=writeback" \
  -append "console=ttyS0,115200n8 root=/dev/vda rootfstype=squashfs ro init=/init appliance.storage=/dev/vdb"
do
  QEMU_ARGS+=("${arg}")
done

echo "==> Starting QEMU (host API http://127.0.0.1:${HOST_PORT}/ → guest :80)"
# debug mode: exec qemu, the rest of this script does not run
if [[ "$DEBUG" == 1 ]] ; then
    echo "Running: ${QEMU_BIN} ${QEMU_ARGS[@]}"
    "${QEMU_BIN}" "${QEMU_ARGS[@]}"
    exit $?

# normal mode: fork qemu to background, pass arguments as
# positional parameters, continue tests
else
    "${QEMU_BIN}" "${QEMU_ARGS[@]}" >"${LOG}" 2>&1 &
    echo $! >"${PIDFILE}"
fi

# poll_http PATH MAX_TIME [COOKIE_JAR] [HOST]
# With a cookie jar + Host, the session cookie is sent (domain-matched via
# --resolve) so authenticated /api/* endpoints pass the edge-auth gate.
poll_http() {
  local path="$1"
  local max_time="$2"
  local jar="${3:-}"
  local host="${4:-}"
  if [[ -n "${jar}" && -n "${host}" ]]; then
    curl -fsS --max-time "${max_time}" -b "${jar}" -c "${jar}" \
      -H "Host: ${host}" --resolve "${host}:${HOST_PORT}:127.0.0.1" \
      "http://${host}:${HOST_PORT}${path}"
  else
    curl -fsS --max-time "${max_time}" "http://127.0.0.1:${HOST_PORT}${path}"
  fi
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
# M6.6 step 5: this endpoint answers before anyone can sign in, so it must carry
# the installer handoff fields and nothing else. The gated names are listed
# explicitly because the regression this pins is a LAN device reading household
# facts (notably how many admins exist) with no credentials at all.
data = json.load(sys.stdin)
assert data.get("status") == "ok", data
assert data.get("role") == "control-plane", data
# mdns_name is legitimately null this early on a fresh disk, so only its key is
# required here; the copy with a name in it is asserted after /setup.
assert "setup" in data and "portal_url" in data, data
gated = {"auth", "chat", "data", "modules", "telemetry", "session_store", "units", "lemond", "docker", "startup_ready"}
leaked = sorted(k for k in data if k in gated)
assert not leaked, "unauthenticated /api/status leaked: %s" % leaked
print("OK: role=control-plane, public subset only (no gated keys)")
'

# --- Edge auth (M6.2): bootstrap the first admin + sign in, BEFORE any
# authenticated /api/* probe. The userdata disk persists across smoke runs, so
# on the 2nd+ boot the first admin already exists and the edge-auth gate is ON
# from boot. The /api/data and /api/modules waits below must therefore carry the
# session cookie; unauthenticated they get a 302 to /login and never return
# JSON. Doing setup+login up front makes the smoke deterministic on any disk. ---

ADMIN_USERNAME="admin"
ADMIN_PASSWORD="smoke-test-pass"
AUTH_HOST="lemon-test.local"
COOKIE_JAR="$(mktemp /tmp/qemu-auth-cookies.XXXXXX)"

echo "==> Edge auth: first-admin bootstrap via /setup (idempotent)"
setup_code="$(curl -sS --max-time 900 -o /tmp/qemu-setup-body.html \
  -w '%{http_code}' \
  -X POST "http://127.0.0.1:${HOST_PORT}/setup" \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"Lemon Test\",\"timezone\":\"UTC\",\"admin_username\":\"${ADMIN_USERNAME}\",\"admin_password\":\"${ADMIN_PASSWORD}\"}" \
  2>/tmp/qemu-setup-err.txt || echo 000)"
# 200 = admin created; 409 = setup already complete (persisted disk). Both OK.
if [[ "${setup_code}" != "200" && "${setup_code}" != "409" ]]; then
  echo "POST /setup returned ${setup_code}" >&2
  cat /tmp/qemu-setup-err.txt >&2 || true
  tail -80 "${LOG}" >&2 || true
  exit 1
fi

# The auth-state wait moved BELOW the login block: account counts are household
# facts, so they now live only in the gated /api/system (M6.6 step 5).

echo "==> CP-native login: sign-in page + credential login"
# Retried, not asserted once: issuing a session needs Valkey, and this is now the
# first thing that asks for one (it used to run after a wait for auth state=enabled,
# which absorbed a slow data plane). A 503 here means "not yet", not "broken".
login_deadline=$((deadline))
page_code=""
while (( SECONDS < login_deadline )); do
  page_code="$(curl -sS -o /dev/null -w '%{http_code}' -H "Host: ${AUTH_HOST}" "http://127.0.0.1:${HOST_PORT}/login")"
  [[ "${page_code}" == "200" ]] && break
  sleep 3
done
[[ "${page_code}" == "200" ]] || {
  echo "GET /login: expected 200, got ${page_code}" >&2; exit 1;
}
signin_deadline=$((deadline))
signin_code=""
while (( SECONDS < signin_deadline )); do
  signin_code="$(curl -sS --max-time 60 -o /dev/null -c "${COOKIE_JAR}" \
    -D /tmp/qemu-signin-hdr.txt -w '%{http_code}' \
    -X POST "http://127.0.0.1:${HOST_PORT}/login" \
    -H "Host: ${AUTH_HOST}" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"${ADMIN_USERNAME}\",\"password\":\"${ADMIN_PASSWORD}\",\"rd\":\"/\"}")"
  [[ "${signin_code}" == "302" ]] && break
  echo "waiting: POST /login returned ${signin_code}" >&2
  if ! kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
    echo "QEMU exited during login; tail ${LOG}:" >&2
    tail -80 "${LOG}" >&2 || true
    exit 1
  fi
  sleep 5
done
if [[ "${signin_code}" != "302" ]] || ! grep -qi '^set-cookie: lemonseed_session' /tmp/qemu-signin-hdr.txt; then
  echo "POST /login: expected 302 + lemonseed_session cookie, got ${signin_code}" >&2
  cat /tmp/qemu-signin-hdr.txt >&2 || true
  exit 1
fi

# The session cookie must NOT be Secure (the appliance is HTTP-only). Verify
# the jar actually captured it.
if ! grep -qi 'lemonseed_session' "${COOKIE_JAR}"; then
  echo "lemonseed_session cookie not captured in jar (Secure flag?)" >&2
  cat /tmp/qemu-signin-hdr.txt >&2 || true
  exit 1
fi

echo "==> Waiting for auth state=enabled (authenticated, remaining budget)"
# Signed in, and against /api/system: /api/status no longer carries auth at all,
# so reading account counts here doubles as proof the gated half answers.
auth_deadline=$((deadline))
auth_json=""
while (( SECONDS < auth_deadline )); do
  candidate="$(poll_http "/api/system" 10 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null || true)"
  if [[ -n "${candidate}" ]]; then
    auth_ok="$(echo "${candidate}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
a = d.get("auth") or {}
state = a.get("state")
users = a.get("users")
admins = a.get("admins")
print("yes" if state == "enabled" else "no")
print("waiting: auth state=%r users=%r admins=%r" % (state, users, admins), file=sys.stderr)
' 2>/tmp/qemu-auth-wait.err || true)"
    cat /tmp/qemu-auth-wait.err >&2 || true
    if [[ "${auth_ok}" == "yes" ]]; then
      auth_json="${candidate}"
      break
    fi
  fi
  if ! kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
    echo "QEMU exited during auth wait; tail ${LOG}:" >&2
    tail -80 "${LOG}" >&2 || true
    exit 1
  fi
  sleep 5
done

if [[ -z "${auth_json}" ]]; then
  echo "Timed out waiting for auth state=enabled" >&2
  curl -fsS --max-time 5 -b "${COOKIE_JAR}" -H "Host: ${AUTH_HOST}" \
    --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
    "http://${AUTH_HOST}:${HOST_PORT}/api/system" >&2 || true
  tail -80 "${LOG}" >&2 || true
  exit 1
fi
echo "${auth_json}" | python3 -c '
import json, sys
data = json.load(sys.stdin)
a = data.get("auth") or {}
state = a.get("state")
users = a.get("users")
admins = a.get("admins")
print("OK: auth state=%r users=%r admins=%r" % (state, users, admins))
assert state == "enabled", a
assert users == 1 and admins == 1, a
# Setup is finished by now, so the name the installer handoff looks for exists.
assert (data.get("setup") or {}).get("mdns_name"), data
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
# Authenticated: the edge-auth gate is on (first admin exists), so /api/data
# requires the session cookie or it 302s to /login.
data_deadline=$((deadline))
data_json=""
while (( SECONDS < data_deadline )); do
  if candidate="$(poll_http "/api/data" 30 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null)"; then
    # The HTTP layer can intermittently return an empty body (socketserver
    # race on a dropped client); treat that as "not ready yet" and retry
    # instead of crashing the smoke run on a JSON parse error.
    ready="$(echo "${candidate}" | python3 -c '
import json, sys
raw = sys.stdin.read()
if not raw.strip():
    print("no")
    print("waiting: empty response from /api/data", file=sys.stderr)
    sys.exit(0)
try:
    d = json.loads(raw)
except ValueError:
    print("no")
    print(f"waiting: unparseable response from /api/data: {raw[:200]!r}", file=sys.stderr)
    sys.exit(0)
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
prov = data.get("provisioning") or {}
# M6.6 step 1: the schema migration runner must have executed against the real
# appliance Postgres, not just the fake cursor in test_db.py. Unit tests already
# pin MIGRATIONS to db.SCHEMA_VERSION, so >= 4 here proves the ALTERs ran.
schema = prov.get("schema_version")
state = prov.get("state")
print(f"OK: postgres={pg!r} valkey={vk!r} provisioning={state!r} schema_version={schema!r}")
assert pg == "ready", data
assert vk == "ready", data
assert state == "ready", data
assert isinstance(schema, int) and schema >= 4, f"expected cp schema >= 4, got {schema!r}"
'

echo "==> Checking /api/modules (OWUI lifecycle; wait for http_ready)"
# First boot may pull a multi-GB image; use remaining QEMU_API_TIMEOUT budget.
owui_deadline=$((deadline))
owui_ready_json=""
while (( SECONDS < owui_deadline )); do
  # Authenticated (edge-auth gate is on): /api/modules needs the session cookie.
  modules_json="$(poll_http "/api/modules" 5 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null || true)"
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

# M6.3b Phase 1: the CP must have signed in as its service account
# (lemonseed-cp@localhost) directly to the slot — normally before nginx
# cutover, with a supervisor-tick retry covering a transient first attempt.
echo "==> Checking OWUI service identity (open_webui.service_ready)"
service_ready=false
svc_deadline=$(( SECONDS + 120 ))
while (( SECONDS < svc_deadline )); do
  modules_json="$(poll_http "/api/modules" 5 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null || true)"
  if [[ -n "${modules_json}" ]] && echo "${modules_json}" | python3 -c '
import json, sys
owui = (json.load(sys.stdin).get("open_webui") or {})
if owui.get("service_ready") is True:
    print("service_account_id=%r" % owui.get("service_account_id"), file=sys.stderr)
    sys.exit(0)
sys.exit(2)
' 2>/tmp/qemu-owui-svc.err; then
    service_ready=true
    cat /tmp/qemu-owui-svc.err >&2 || true
    break
  fi
  sleep 5
done
if [[ "${service_ready}" != "true" ]]; then
  echo "Timed out waiting for open_webui.service_ready=True (CP service account in OWUI)" >&2
  curl -fsS --max-time 5 -H "Host: ${AUTH_HOST}" --cookie "${COOKIE_JAR}" \
    "http://127.0.0.1:${HOST_PORT}/api/modules" >&2 || true
  tail -80 "${LOG}" >&2 || true
  exit 1
fi
echo "OK: open_webui.service_ready=True (CP service account provisioned in OWUI)"

# M6.3b Phase 2: persistent config is ON — the DB (not env) is authoritative.
# Act like a household admin: sign in to the OWUI SPA through the gateway
# with the portal session cookie (trusted-header sign-in — the body creds
# satisfy form validation but are ignored), read the admin configs/export,
# and plant a marker key. Run 1 after the flip seeds it; later runs on the
# same user-data disk (fresh VM boot recreates the container — the
# lemonseed-update path) must find it back: proof config now persists.
echo "==> OWUI persistent config: SPA sign-in through the gateway + configs/export"
owui_signin_json="$(curl -sS --max-time 60 -b "${COOKIE_JAR}" -c "${COOKIE_JAR}" \
  --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -X POST "http://${AUTH_HOST}:${HOST_PORT}/api/v1/auths/signin" \
  -H 'Content-Type: application/json' \
  -d '{"email":"smoke@trusted-header.ignored","password":"trusted-header-signin-ignores-body"}')"
owui_token="$(printf '%s' "${owui_signin_json}" | python3 -c '
import json, sys
body = json.load(sys.stdin)
token = str(body.get("token") or "")
email = str(body.get("email") or "").lower()
if not token:
    print("OWUI SPA sign-in: no token in response", body, file=sys.stderr)
    sys.exit(1)
if email != "admin@localhost":
    print("OWUI SPA sign-in: identity %r is not the portal admin" % email, file=sys.stderr)
    sys.exit(1)
print(token)
')"
owui_user_id="$(printf '%s' "${owui_signin_json}" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("id") or "")')"
svc_id="$(printf '%s' "${modules_json}" | python3 -c 'import json,sys; owui=(json.load(sys.stdin).get("open_webui") or {}); print(owui.get("service_account_id") or "")')"
if [[ -z "${svc_id}" || "${owui_user_id}" == "${svc_id}" ]]; then
  echo "Portal admin must be its own OWUI user, distinct from the CP service account (user id=${owui_user_id} service id=${svc_id})" >&2
  exit 1
fi
echo "OK: portal admin signed into OWUI as its own user (id=${owui_user_id}, service account id=${svc_id})"

owui_export="$(curl -fsS --max-time 20 -b "${COOKIE_JAR}" \
  --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -H "Authorization: Bearer ${owui_token}" \
  "http://${AUTH_HOST}:${HOST_PORT}/api/v1/configs/export")"
marker_key="lemonseed.qemu_smoke_marker"
marker_value="m6.3b-phase2"
set +e
printf '%s' "${owui_export}" | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
# Proof env -> DB seeding under persistent config (both are CP-managed):
# ollama off, OpenAI-compatible Lemonade on.
for key, want in (("ollama.enable", False), ("openai.enable", True)):
    if cfg.get(key) is not want:
        print("configs/export: %s=%r, expected %r" % (key, cfg.get(key), want), file=sys.stderr)
        sys.exit(1)
sys.exit(0 if cfg.get(sys.argv[1]) == sys.argv[2] else 3)
' "${marker_key}" "${marker_value}"
export_rc=$?
set -e
if [[ "${export_rc}" -eq 1 ]]; then
  echo "configs/export missing or wrong seeded values (details on stderr above); head of body:" >&2
  printf '%.400s\n' "${owui_export}" >&2 || true
  exit 1
elif [[ "${export_rc}" -eq 3 ]]; then
  # First run after the flip: plant the marker through the API so the next
  # run (new VM boot, recreated container) can prove DB persistence.
  echo "==> Marker absent: planting ${marker_key} via configs/import (first post-flip run)"
  curl -fsS --max-time 20 -b "${COOKIE_JAR}" \
    --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
    -H "Authorization: Bearer ${owui_token}" -H 'Content-Type: application/json' \
    -X POST "http://${AUTH_HOST}:${HOST_PORT}/api/v1/configs/import" \
    -d "{\"config\":{\"${marker_key}\":\"${marker_value}\"}}" >/dev/null
  owui_export="$(curl -fsS --max-time 20 -b "${COOKIE_JAR}" \
    --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
    -H "Authorization: Bearer ${owui_token}" \
    "http://${AUTH_HOST}:${HOST_PORT}/api/v1/configs/export")"
  printf '%s' "${owui_export}" | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
if cfg.get(sys.argv[1]) != sys.argv[2]:
    print("marker did not stick through configs/import", sys.argv[1], file=sys.stderr)
    sys.exit(1)
' "${marker_key}" "${marker_value}"
  echo "OK: configs/export reflects env seed (ollama.enable=False, openai.enable=True); marker planted via API (next run must see it persist)"
else
  echo "OK: configs/export reflects env seed (ollama.enable=False, openai.enable=True); marker survived container recreation — persistent config works"
fi

echo "==> Probing OWUI SPA asset via nginx (/_app/version.json)"
curl -fsS --max-time 10 "http://127.0.0.1:${HOST_PORT}/_app/version.json" \
  | python3 -c 'import sys; body=sys.stdin.read(200); assert body.strip(), "empty version.json"; print("OK: /_app/version.json (%d bytes)" % len(body))'

# M6.3b Phase 3: the owui_policy engine. (a) the gate: this boot's write
# reconcile must reach ok (blocks the chat iframe until intent is applied —
# decision D1), with the machine key minted and the LKG snapshot written;
# (b) the two-run drift proof: export must show web.search.enable=False here
# (run 1: backfilled at first boot; run 2+: at_boot reverted the previous
# run's out-of-band flip); (c) flip it out-of-band and (d) the periodic audit
# tick must report the drift as pending — never silently fix it (D3).
echo "==> OWUI policy: waiting for reconcile gate (owui_policy.ok)"
policy_ok=false
policy_deadline=$((SECONDS + 300))
while (( SECONDS < policy_deadline )); do
  modules_json="$(poll_http "/api/modules" 5 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null || true)"
  if [[ -n "${modules_json}" ]] && printf '%s' "${modules_json}" | python3 -c '
import json, sys
owui = (json.load(sys.stdin).get("open_webui") or {})
p = owui.get("owui_policy") or {}
missing = [k for k in ("manifest_version", "ok", "key_ready", "lkg_at") if not p.get(k)]
if missing:
    diag = {k: p.get(k) for k in ("state", "ok", "key_ready", "lkg_at", "manifest_version",
                                  "last_error", "manifest_error", "owui_version",
                                  "last_reconciled")}
    print("owui_policy not settled yet, missing/false: %r diag: %r" % (missing, diag),
          file=sys.stderr)
    sys.exit(1)
assert p["manifest_version"] == 1, p
assert p["ok"] is True, p
summary = {k: v for k, v in (p.get("drift") or {}).items() if k != "entries"}
print("gate ok: manifest=%r key_ready=%r lkg_at=%r drift=%r" % (
    p["manifest_version"], p["key_ready"], p["lkg_at"], summary), file=sys.stderr)
' 2>/tmp/qemu-owui-policy.err; then
    policy_ok=true
    break
  fi
  sleep 5
done
if [[ "${policy_ok}" != "true" ]]; then
  echo "Timed out waiting for open_webui.owui_policy.ok=True" >&2
  cat /tmp/qemu-owui-policy.err >&2 || true
  exit 1
fi
cat /tmp/qemu-owui-policy.err >&2 || true
echo "OK: owui_policy gate ok (manifest v1 reconciled; key minted; LKG written)"

echo "==> OWUI policy drift: export must show web.search.enable=False (this boot's at_boot reconcile)"
printf '%s' "$(curl -fsS --max-time 20 -b "${COOKIE_JAR}" \
  --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -H "Authorization: Bearer ${owui_token}" \
  "http://${AUTH_HOST}:${HOST_PORT}/api/v1/configs/export")" | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
if cfg.get("web.search.enable") is not False:
    print("web.search.enable=%r after boot — at_boot reconcile did not enforce intent"
          % cfg.get("web.search.enable"), file=sys.stderr)
    sys.exit(1)
'
echo "OK: managed value enforced at boot (a previous run's out-of-band flip is reverted here)"

# M6.3b Phase 4A: the ownership view. Assert badges, not values: managed keys
# must classify as ours, unmanaged keys as theirs, the credential must come back
# masked, and the model/user/guard cards must be present. Runs before the
# drift-flip section below so the tree it reads is a settled one.
echo "==> OWUI ownership view: /_lemonseed/api/owui/config classification + masking"
if printf '%s' "$(curl -fsS --max-time 30 -b "${COOKIE_JAR}" \
    --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
    "http://${AUTH_HOST}:${HOST_PORT}/_lemonseed/api/owui/config")" | python3 -c '
import json, sys
view = json.load(sys.stdin)


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


if not view.get("ok") or not view.get("live_available"):
    fail("view is not live: ok=%r live_available=%r export_error=%r"
         % (view.get("ok"), view.get("live_available"), view.get("export_error")))
rows = {r.get("key"): r for s in view.get("sections") or [] for r in s.get("entries") or []}
if not rows:
    fail("no config rows rendered at all")
for key in ("openai.api_base_urls", "openai.api_configs", "openai.api_keys",
            "openai.enable", "ollama.enable", "web.search.enable"):
    got = (rows.get(key) or {}).get("classification")
    if got != "managed":
        fail("row %s classified %r, want managed" % (key, got))
marker = rows.get("lemonseed.qemu_smoke_marker") or {}
if marker.get("classification") != "yours":
    fail("the admin-planted marker key must be classified yours, got %r" % (marker,))
if marker.get("present") is not True:
    fail("the marker key is in Open WebUI but not reported present: %r" % (marker,))
sections = {s.get("section"): s for s in view.get("sections") or []}
if (sections.get("openai") or {}).get("rollup") != "ours":
    fail("the openai namespace must roll up as ours: %r" % (sections.get("openai"),))
if (sections.get("lemonseed") or {}).get("rollup") != "yours":
    fail("the marker namespace must roll up as yours: %r" % (sections.get("lemonseed"),))
# Day one owns exactly one connection: one base url, mirrored by api_configs.
urls = (rows.get("openai.api_base_urls") or {}).get("found")
if not isinstance(urls, list) or len(urls) != 1:
    fail("openai.api_base_urls must be a 1-element list, got %r" % (urls,))
configs = (rows.get("openai.api_configs") or {}).get("found")
if not isinstance(configs, dict) or list(configs) != urls:
    fail("openai.api_configs must key on the managed base url: %r vs %r" % (configs, urls))
if (rows.get("openai.api_keys") or {}).get("found") != "***":
    fail("openai.api_keys must be masked in the portal, got %r"
         % (rows.get("openai.api_keys") or {}).get("found", "<missing>"))
kinds = {e.get("kind") for e in view.get("entities") or []}
if kinds != {"models", "users"}:
    fail("entity cards must cover models and users, got %r" % (sorted(kinds),))
models = next(e for e in view["entities"] if e.get("kind") == "models")
if models.get("reserved_name_regex") != "^Lemonseed.*$":
    fail("models must reserve the Lemonseed name space, got %r"
         % models.get("reserved_name_regex"))
guards = view.get("guards") or []
summary = view.get("guard_summary") or {}
# Phase 4B: most guards block at the gateway, and "enforced" here means the CP
# rendered it and nginx accepted it — not that the manifest mentioned it.
if summary.get("source") != "rendered":
    fail("guard summary must come from the rendered state, got %r" % (summary,))
if summary.get("enforced") != 5 or summary.get("total") != 7:
    fail("expected 5 blocked of 7 guards, got %r" % (summary,))
if not summary.get("in_sync"):
    fail("declared/served guard hashes disagree: %r" % (summary,))
if summary.get("declared_sha") != summary.get("rendered_sha"):
    fail("rendered file differs from the manifest render: %r" % (summary,))
blocked = {g.get("key") for g in guards if g.get("enforced")}
advisory = {g.get("key") for g in guards if not g.get("enforced")}
want_advisory = {"POST /api/v1/auths/add", "DELETE /api/v1/users/.*"}
if advisory != want_advisory:
    fail("account guards must stay advisory until M6.2b, got %r" % (sorted(advisory),))
if len(blocked) != 5 or (blocked & want_advisory):
    fail("unexpected blocked guard set: %r" % (sorted(blocked),))
blob = json.dumps(view)
if "sk-" in blob:
    fail("portal payload carries something that looks like a machine key")
print("ownership view: %d rows, %d namespaces, %d guards (%d enforced)" % (
    view.get("counts", {}).get("config", 0), len(view.get("sections") or []),
    len(guards), summary.get("enforced", -1)))
'; then
  echo "OK: ownership view classifies managed vs yours, masks the key, shows 5 enforced guards"
else
  echo "owui ownership view assertions failed (details on stderr above)" >&2
  exit 1
fi

# The ownership payload still carries the full tree, but the page must not.
# Bare-metal reviews: two chat boxes, a meaningless iframe path, a wall of
# per-key ownership detail, then an "Advanced details" box nobody could use.
# Assert the shape of the rendered panel here so developer detail cannot creep
# back, and assert the *content* of the two Status rows so the panel cannot
# regress to a shell that renders nothing but placeholders.
echo "==> Settings page: one Chat panel with Status + Controls"
if printf '%s' "$(curl -fsS --max-time 30 -b "${COOKIE_JAR}" \
    --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
    "http://${AUTH_HOST}:${HOST_PORT}/settings/content")" | python3 -c '
import sys
html = sys.stdin.read()


def fail(msg):
    print(msg, file=sys.stderr)
    sys.exit(1)


if html.count("<h2>Chat</h2>") != 1:
    fail("expected exactly one Chat panel, got %d" % html.count("<h2>Chat</h2>"))
if "Chat interface settings" in html:
    fail("the second chat panel must stay merged into the first")
for missing in ("id=\"owui-state\"", "id=\"owui-apply\"", "id=\"owui-reset\"",
                "id=\"owui-title\"", "id=\"owui-config-state\"",
                "<h2>API</h2>", "System API Data"):
    if missing not in html:
        fail("missing " + missing)
# The two Status rows must carry real facts, not placeholders. On the appliance
# docker answers and the boot reconcile has already reported ok by this point.
if "Open WebUI v0.11." not in html:
    fail("status row must name the Open WebUI version")
if "id=\"owui-config-state\">updated " not in html:
    fail("configuration row must carry a reconcile stamp")
if "/?embed=1" in html:
    fail("the iframe path row must not come back")
for detail in ("id=\"owui-tree\"", "id=\"owui-cards\"", "Enforcement at the gateway",
               "owui-advanced", "Advanced details", "id=\"owui-tech\"", "Diagnostics"):
    if detail in html:
        fail("developer-only detail leaked back into the admin UI: " + detail)
# Negative control: the rest of the page still renders.
for panel in ("Lemonade AI Engine", "Large Language Model", "id=\"model-picker\""):
    if panel not in html:
        fail("unrelated settings content stopped rendering: " + panel)
print("settings page: one Chat panel, two factual Status rows, no ownership tree")
'; then
  echo "OK: settings page renders one Chat panel without the ownership tree"
else
  echo "settings page assertions failed (details on stderr above)" >&2
  exit 1
fi

# M6.3b Phase 4B: gateway enforcement of the managed paths.
#
# Marker rule — never assert on status alone, because Open WebUI also answers
# 403 for its own permission failures:
#   guarded      -> status 403 AND body carries our marker
#   not guarded  -> body must NOT carry our marker, whatever the status
# Every guard in the manifest gets one assertion here (5 enforce + 2 advertise
# = all 7), so a guard added later without a case is a visible gap.
echo "==> OWUI guards: rendered state must match the manifest"
guards_ok=false
guards_deadline=$(( SECONDS + 120 ))
while (( SECONDS < guards_deadline )); do
  modules_json="$(poll_http "/api/modules" 5 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null || true)"
  if [[ -n "${modules_json}" ]] && printf '%s' "${modules_json}" | python3 -c '
import json, sys
owui = (json.load(sys.stdin).get("open_webui") or {})
g = owui.get("owui_guards") or {}


def fail(msg):
    print(msg % g if "%" in msg else msg, file=sys.stderr)
    sys.exit(1)


if not g:
    fail("open_webui.owui_guards missing — the CP never rendered a guard file")
if g.get("rendered_sha") != g.get("declared_sha"):
    fail("rendered guard file does not match the manifest render: %%r")
if g.get("nginx_sha") != g.get("rendered_sha"):
    fail("nginx has not accepted the rendered guards (reload failing): %%r")
if g.get("pending"):
    fail("guards still pending (service account id unresolved?): %%r")
if g.get("enforced_count") != 5 or g.get("declared") != 5:
    fail("expected 5 enforced of 5 declared guards: %%r")
if not g.get("in_sync"):
    fail("guard state not in sync: %%r")
print("guard state: declared=%s rendered=%s sha=%s" % (
    g.get("declared"), g.get("enforced_count"), (g.get("rendered_sha") or "")[:12]),
    file=sys.stderr)
' 2>/tmp/qemu-owui-guards.err; then
    guards_ok=true
    cat /tmp/qemu-owui-guards.err >&2 || true
    break
  fi
  sleep 5
done
if [[ "${guards_ok}" != "true" ]]; then
  echo "Timed out waiting for open_webui.owui_guards to be in sync" >&2
  tail -n 5 /tmp/qemu-owui-guards.err >&2 || true
  exit 1
fi
echo "OK: guards declared, rendered and served (5 enforced)"

# One request through the gateway, reporting the status code and leaving the
# body in /tmp/qemu-guard.body for the marker check.
guard_probe() {
  local method="$1" path="$2" data="${3-}"
  local args=(-sS -o /tmp/qemu-guard.body -D /tmp/qemu-guard.hdr -w '%{http_code}' --max-time 25
              -b "${COOKIE_JAR}" --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1"
              -H "Authorization: Bearer ${owui_token}" -X "${method}"
              "http://${AUTH_HOST}:${HOST_PORT}${path}")
  # Remove the previous body: a probe that never connects must not be judged on
  # the last probe's bytes.
  rm -f /tmp/qemu-guard.body /tmp/qemu-guard.hdr
  if [[ -n "${data}" ]]; then
    args+=(-H 'Content-Type: application/json' -d "${data}")
  fi
  curl "${args[@]}"
}

require_blocked() {
  local method="$1" path="$2" data="${3-}"
  local code
  code="$(guard_probe "${method}" "${path}" "${data}")"
  if [[ "${code}" != "403" ]] || ! grep -q managed-by-lemonseed /tmp/qemu-guard.body; then
    echo "GUARD NOT ENFORCED: ${method} ${path} -> ${code}, marker absent. Body head:" >&2
    head -c 300 /tmp/qemu-guard.body >&2 || true
    echo "" >&2
    exit 1
  fi
  # The denial must reach the browser as JSON with a non-empty ``detail``, or
  # Open WebUI renders its own generic "Server connection failed" toast and the
  # admin never learns why the call was refused (found on bare metal).
  if ! grep -qi '^content-type: application/json' /tmp/qemu-guard.hdr; then
    echo "GUARD DENIAL NOT JSON: ${method} ${path} content-type head:" >&2
    grep -i '^content-type' /tmp/qemu-guard.hdr >&2 || true
    exit 1
  fi
  if ! python3 -c "import json,sys; d=json.load(open('/tmp/qemu-guard.body')); assert d['detail'].strip(); assert d['lemonseed']=='managed-by-lemonseed'" 2>/dev/null; then
    echo "GUARD DENIAL BODY unusable (need non-empty detail + marker):" >&2
    head -c 300 /tmp/qemu-guard.body >&2 || true
    echo "" >&2
    exit 1
  fi
  echo "  blocked: ${method} ${path}"
}

require_unguarded() {
  local method="$1" path="$2" data="${3-}"
  local code
  code="$(guard_probe "${method}" "${path}" "${data}")"
  if grep -q managed-by-lemonseed /tmp/qemu-guard.body; then
    echo "WRONGLY GUARDED: ${method} ${path} was refused by Lemonseed (status ${code})" >&2
    exit 1
  fi
  echo "  open: ${method} ${path} (status ${code})"
}

echo "==> OWUI guards: each enforced path is refused at the gateway with our marker"
require_blocked POST /openai/config/update '[]'
require_blocked POST /ollama/config/update '[]'
require_blocked POST /api/v1/configs/connections '{}'
require_blocked POST "/api/v1/users/${svc_id}/update" '{}'
require_blocked DELETE "/api/v1/users/${svc_id}"
echo "OK: all 5 enforced guards blocked at the nginx edge (service id ${svc_id})"

# The two account guards stay advisory until M6.2b replaces them with user-list
# mirroring. Deliberately non-mutating calls: an empty body and a user that does
# not exist, so this proves "not guarded" without creating or deleting anything.
echo "==> OWUI guards: advisory paths stay open (Open WebUI answers, not us)"
require_unguarded POST /api/v1/auths/add '{}'
require_unguarded DELETE /api/v1/users/does-not-exist

# Unguarded URIs, chosen because this suite depends on them: a render that
# over-reaches breaks here loudly instead of mysteriously later.
echo "==> OWUI guards: the unguarded battery carries no marker"
require_unguarded GET /api/v1/configs/export
require_unguarded POST /api/v1/configs/import '{"config":{}}'
require_unguarded GET /api/v1/models/list
require_unguarded GET /api/status
require_unguarded GET "/ws/socket.io/?EIO=4&transport=polling"
# /_app/version.json is probed unauthenticated elsewhere; here it must simply
# not carry our marker.
if curl -sS --max-time 15 "http://127.0.0.1:${HOST_PORT}/_app/version.json" \
    | grep -q managed-by-lemonseed; then
  echo "WRONGLY GUARDED: /_app/version.json carries the Lemonseed marker" >&2
  exit 1
fi
echo "  open: GET /_app/version.json (no marker)"

# Anchoring check: without this, a prefix match would silently block
# .../connections/update-shaped paths and nothing else here would notice.
echo "==> OWUI guards: anchored match does not over-reach"
require_unguarded POST /api/v1/configs/connections-x '{}'

# The property that makes enforcement safe: the gateway refuses these writes for
# the UI while the CP — which talks to the slot port, not through nginx — applies
# exactly those keys moments later. Proves "CP allowed, UI blocked" through state
# instead of through a second port forward (decision T4-F).
echo "==> OWUI guards: the control plane still writes what the UI was refused"
reconcile_code="$(curl -sS -o /tmp/qemu-owui-reconcile.json -w '%{http_code}' \
  --max-time 120 -b "${COOKIE_JAR}" --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -X POST "http://${AUTH_HOST}:${HOST_PORT}/_lemonseed/api/owui/reconcile")"
if [[ "${reconcile_code}" != "200" ]] || ! python3 -c '
import json, sys
body = json.load(open("/tmp/qemu-owui-reconcile.json"))
assert body.get("ok") is True, body
' ; then
  echo "POST /_lemonseed/api/owui/reconcile: expected 200 ok, got ${reconcile_code}" >&2
  head -c 400 /tmp/qemu-owui-reconcile.json >&2 || true
  echo "" >&2
  exit 1
fi
curl -fsS --max-time 20 -b "${COOKIE_JAR}" \
  --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -H "Authorization: Bearer ${owui_token}" \
  "http://${AUTH_HOST}:${HOST_PORT}/api/v1/configs/export" | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
for key in ("direct.enable", "ollama.enable"):
    assert cfg.get(key) is False, "%s=%r after the manual reconcile" % (key, cfg.get(key))
urls = cfg.get("openai.api_base_urls")
assert isinstance(urls, list) and len(urls) == 1, \
    "openai.api_base_urls must hold exactly the managed connection, got %r" % (urls,)
print("OK: UI refused on direct/ollama/openai, CP re-applied all three a moment later")
'

echo "==> OWUI policy drift: flipping web.search.enable=true out-of-band (admin API)"
curl -fsS --max-time 20 -b "${COOKIE_JAR}" \
  --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -H "Authorization: Bearer ${owui_token}" -H 'Content-Type: application/json' \
  -X POST "http://${AUTH_HOST}:${HOST_PORT}/api/v1/configs/import" \
  -d '{"config":{"web.search.enable":true}}' >/dev/null
curl -fsS --max-time 20 -b "${COOKIE_JAR}" \
  --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -H "Authorization: Bearer ${owui_token}" \
  "http://${AUTH_HOST}:${HOST_PORT}/api/v1/configs/export" | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
assert cfg.get("web.search.enable") is True, \
    "out-of-band flip did not stick: web.search.enable=%r" % cfg.get("web.search.enable")
'
# Anchor for "did a write run revert my flip?": the CP's own last_reconciled,
# read *before* the flip. Comparing it to the host's clock is wrong — the guest
# clock is not the host clock — and Phase 4B made the margin visible: the admin
# manual reconcile now lands seconds before this flip, so any skew made the
# pre-flip run look like a post-flip one and the loop re-flipped needlessly.
pre_reconciled="$(poll_http "/api/modules" 5 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null \
  | python3 -c '
import json, sys
owui = (json.load(sys.stdin).get("open_webui") or {})
print((owui.get("owui_policy") or {}).get("last_reconciled") or "")
' 2>/dev/null || echo "")"
echo "==> Waiting for the periodic audit tick to report the drift as pending (<=720s)"
rm -f /tmp/qemu-owui-drift.err
drift_seen=false
drift_deadline=$((SECONDS + 720))
while (( SECONDS < drift_deadline )); do
  drift_json="$(poll_http "/api/modules" 5 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null || true)"
  drift_rc=1
  if [[ -n "${drift_json}" ]]; then
    if printf '%s' "${drift_json}" | python3 -c '
import json, sys, datetime
owui = (json.load(sys.stdin).get("open_webui") or {})
p = owui.get("owui_policy") or {}
entries = (p.get("drift") or {}).get("entries") or []
entry = next((e for e in entries if e.get("key") == "web.search.enable"), None)
state = entry.get("state") if entry else None
if state == "pending":
    sys.exit(0)
# An entry state of ok only means REVERTED when a write-triggered run
# completed after our flip; before the first audit tick the report
# legitimately still shows the pre-flip run as ok. Both timestamps here come
# from the control plane, so no host/guest clock skew is involved.
pre = sys.argv[1]
last = p.get("last_reconciled")
if state == "ok" and last and pre and (p.get("last_trigger") or []) != ["audit"]:
    try:
        if datetime.datetime.fromisoformat(last) > datetime.datetime.fromisoformat(pre):
            sys.exit(2)
    except ValueError:
        pass
diag = {k: p.get(k) for k in ("state", "ok", "last_error", "last_reconciled",
                              "last_trigger", "cp_pid")}
diag["slot"] = (owui.get("port"), owui.get("container"))
print("drift not pending yet: entry=%r diag=%r" % (state, diag), file=sys.stderr)
sys.exit(1)
' "${pre_reconciled}" 2>>/tmp/qemu-owui-drift.err; then
      drift_rc=0
    else
      drift_rc=$?
    fi
  fi
  if [[ "${drift_rc}" -eq 0 ]]; then
    drift_seen=true
    break
  fi
  if [[ "${drift_rc}" -eq 2 ]]; then
    # A write reconcile (boot/CP restart/slot change) legitimately reverted
    # the flip and re-armed the audit timer; re-assert the out-of-band value.
    echo "  (write reconcile reverted the flip; re-flipping)" >&2
    curl -fsS --max-time 20 -b "${COOKIE_JAR}" \
      --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
      -H "Authorization: Bearer ${owui_token}" -H 'Content-Type: application/json' \
      -X POST "http://${AUTH_HOST}:${HOST_PORT}/api/v1/configs/import" \
      -d '{"config":{"web.search.enable":true}}' >/dev/null || true
    pre_reconciled="$(poll_http "/api/modules" 5 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null \
      | python3 -c '
import json, sys
owui = (json.load(sys.stdin).get("open_webui") or {})
print((owui.get("owui_policy") or {}).get("last_reconciled") or "")
' 2>/dev/null || echo "")"
  fi
  sleep 5
done
if [[ "${drift_seen}" != "true" ]]; then
  echo "audit tick did not report web.search.enable drift (pending) within 720s" >&2
  tail -n 5 /tmp/qemu-owui-drift.err >&2 || true
  exit 1
fi
tail -n 3 /tmp/qemu-owui-drift.err >&2 || true
echo "OK: audit tick reported the drift without fixing it; next boot's at_boot run reverts it"

# M6.3b Phase 4B: the admin config reset (T4-C/T4-D). Deliberately last: it
# truncates Open WebUI's config table, so everything above reads the pre-reset
# state. The window is the point of the test — the appliance must show the
# Getting-Ready shell rather than a half-configured Open WebUI — and the marker
# re-plant afterwards keeps the next run's Phase-2 persistence proof honest.
echo "==> OWUI reset: admin reset rebuilds the config from the manifest"
reset_timeout="${QEMU_OWUI_RESET_TIMEOUT:-900}"
reset_code="$(curl -sS -o /tmp/qemu-owui-reset.json -w '%{http_code}' --max-time 30 \
  -b "${COOKIE_JAR}" --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -H 'Content-Type: application/json' \
  -X POST "http://${AUTH_HOST}:${HOST_PORT}/_lemonseed/api/owui/reset-config" \
  -d '{"confirm":"reset"}')"
if [[ "${reset_code}" != "202" ]]; then
  echo "POST /_lemonseed/api/owui/reset-config: expected 202, got ${reset_code}" >&2
  head -c 300 /tmp/qemu-owui-reset.json >&2 || true
  echo "" >&2
  exit 1
fi
echo "OK: reset accepted (202); watching the window for up to ${reset_timeout}s"

# The dangerous phases are the ones where nothing should answer at all: the
# truncate happens with both slots down, so a 200 from Open WebUI here would
# mean users were served a config mid-rebuild.
reset_window_bad=false
reset_seen_down=false
reset_json=""
reset_deadline=$(( SECONDS + reset_timeout ))
while (( SECONDS < reset_deadline )); do
  reset_json="$(poll_http "/api/modules" 5 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null || true)"
  reset_state="$(printf '%s' "${reset_json}" | python3 -c '
import json, sys
try:
    owui = (json.load(sys.stdin).get("open_webui") or {})
except Exception:
    print("")
    sys.exit(0)
print((owui.get("owui_reset") or {}).get("state") or "")
' 2>/dev/null || echo "")"
  case "${reset_state}" in
    stopping|clearing|starting)
      reset_seen_down=true
      window_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
        "http://127.0.0.1:${HOST_PORT}/_app/version.json" 2>/dev/null || echo 000)"
      if [[ "${window_code}" == "200" ]]; then
        echo "Reset window served a live Open WebUI asset while state=${reset_state}" >&2
        reset_window_bad=true
        break
      fi
      ;;
    ready|error)
      break
      ;;
    *)
      ;;
  esac
  sleep 10
done
if [[ "${reset_window_bad}" == "true" ]]; then
  exit 1
fi
reset_final="$(printf '%s' "${reset_json}" | python3 -c '
import json, sys
try:
    owui = (json.load(sys.stdin).get("open_webui") or {})
except Exception:
    print("never-observed|")
    sys.exit(0)
r = owui.get("owui_reset") or {}
print("%s|%s" % (r.get("state"), (r.get("error") or "")[:200]))
' 2>/dev/null || echo "unparsable|")"
if [[ "${reset_final%%|*}" == "error" ]]; then
  echo "admin reset failed: ${reset_final}" >&2
  tail -n 60 "${LOG}" >&2 || true
  exit 1
fi
if [[ "${reset_final%%|*}" != "ready" ]]; then
  echo "reset did not reach state=ready within ${reset_timeout}s (last: ${reset_final})" >&2
  tail -n 60 "${LOG}" >&2 || true
  exit 1
fi
if [[ "${reset_seen_down}" != "true" ]]; then
  echo "reset finished faster than the window could be observed; cannot prove the interstitial" >&2
  exit 1
fi
echo "OK: the window stayed on Getting Ready (no live Open WebUI while rebuilding)"

# After the window: engine ok again, guards still served, managed keys re-applied
# from scratch. Container restart can invalidate the old OWUI JWT, so sign in
# again through the gateway for the post-reset reads.
owui_token="$(curl -sS --max-time 60 -b "${COOKIE_JAR}" -c "${COOKIE_JAR}" \
  --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -X POST "http://${AUTH_HOST}:${HOST_PORT}/api/v1/auths/signin" \
  -H 'Content-Type: application/json' \
  -d '{"email":"smoke@trusted-header.ignored","password":"trusted-header-signin-ignores-body"}' \
  | python3 -c '
import json, sys
body = json.load(sys.stdin)
token = str(body.get("token") or "")
if not token:
    print("post-reset OWUI sign-in returned no token", body, file=sys.stderr)
    sys.exit(1)
print(token)
')"

# Cross-phase check: the truncate must have removed the Phase-2 marker (proof it
# hit the right table), then re-planting it must work (proof the rebuilt config
# is writable), so the next run's persistence assertion still means something.
curl -fsS --max-time 20 -b "${COOKIE_JAR}" \
  --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -H "Authorization: Bearer ${owui_token}" \
  "http://${AUTH_HOST}:${HOST_PORT}/api/v1/configs/export" | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
if cfg.get("lemonseed.qemu_smoke_marker") is not None:
    print("TRUNCATE did not clear the config table: marker still present", file=sys.stderr)
    sys.exit(1)
for key, want in (("ollama.enable", False), ("direct.enable", False),
                  ("openai.enable", True), ("web.search.enable", False)):
    if cfg.get(key) is not want:
        print("post-reset %s=%r, expected %r" % (key, cfg.get(key), want), file=sys.stderr)
        sys.exit(1)
urls = cfg.get("openai.api_base_urls")
assert isinstance(urls, list) and len(urls) == 1, \
    "post-reset openai.api_base_urls=%r" % (urls,)
' || {
  echo "post-reset config assertions failed (details on stderr above)" >&2
  exit 1
}
echo "OK: reset cleared the table and the manifest re-applied every managed key"

curl -fsS --max-time 20 -b "${COOKIE_JAR}" \
  --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -H "Authorization: Bearer ${owui_token}" -H 'Content-Type: application/json' \
  -X POST "http://${AUTH_HOST}:${HOST_PORT}/api/v1/configs/import" \
  -d "{\"config\":{\"${marker_key}\":\"${marker_value}\"}}" >/dev/null
curl -fsS --max-time 20 -b "${COOKIE_JAR}" \
  --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -H "Authorization: Bearer ${owui_token}" \
  "http://${AUTH_HOST}:${HOST_PORT}/api/v1/configs/export" | python3 -c '
import json, sys
cfg = json.load(sys.stdin)
if cfg.get(sys.argv[1]) != sys.argv[2]:
    print("marker did not stick after the reset — rebuilt config is not writable",
          file=sys.stderr)
    sys.exit(1)
print("OK: marker re-planted after the reset (next run must still prove persistence)")
' "${marker_key}" "${marker_value}"

# Guards survive a reset: the service account lives in the user table, which the
# truncate does not touch, so the rendered rules must come back byte-identical.
poll_http "/api/modules" 20 "${COOKIE_JAR}" "${AUTH_HOST}" | python3 -c '
import json, sys
owui = (json.load(sys.stdin).get("open_webui") or {})
g = owui.get("owui_guards") or {}
p = owui.get("owui_policy") or {}
if not p.get("ok"):
    print("owui_policy not ok after reset: %r" % ({k: p.get(k) for k in ("ok", "state", "last_error")},), file=sys.stderr)
    sys.exit(1)
if g.get("enforced_count") != 5 or g.get("nginx_sha") != g.get("rendered_sha"):
    print("guards not re-established after reset: %r" % (g,), file=sys.stderr)
    sys.exit(1)
if g.get("service_account_id") and g.get("service_account_id") != owui.get("service_account_id"):
    print("service account identity changed across the reset", file=sys.stderr)
    sys.exit(1)
print("OK: guards re-established after reset (sha %s)" % (g.get("rendered_sha") or "")[:12])
'

# The control plane's stdout reaches the serial console, so the journal is the
# record of what it actually did (as opposed to what it now claims).
echo "==> Serial log: the control plane rendered the guards and completed the reset"
if grep -q "owui guards: render failed\|owui guards: write failed\|owui guards: nginx reload failed" "${LOG}"; then
  echo "serial log shows a guard render/write/reload failure:" >&2
  grep -n "owui guards" "${LOG}" | tail -5 >&2 || true
  exit 1
fi
grep -q "owui guards: rendered" "${LOG}" || {
  echo "serial log has no 'owui guards: rendered' line — the CP never wrote the include" >&2
  exit 1
}
grep -q "owui reset: settings rebuilt" "${LOG}" || {
  echo "serial log has no 'owui reset: settings rebuilt' line" >&2
  exit 1
}
if grep -q "owui reset: failed\|owui reset: LKG restore" "${LOG}"; then
  echo "reset reported a failure or needed the LKG ladder:" >&2
  grep -n "owui reset" "${LOG}" | tail -5 >&2 || true
  exit 1
fi
echo "OK: guard render + reset recorded in the serial log"

echo "==> Auth gate: unauthenticated requests must redirect to /login"
root_unauth_code="$(curl -sS -o /dev/null -D /tmp/qemu-root-unauth.hdr -w '%{http_code}' -H "Host: ${AUTH_HOST}" "http://127.0.0.1:${HOST_PORT}/")"
if [[ "${root_unauth_code}" != "302" ]] || ! grep -qiE '^location: (https?://[^/]+)?/login\?rd=' /tmp/qemu-root-unauth.hdr; then
  echo "GET / unauthenticated: expected 302 to /login?rd=, got ${root_unauth_code}" >&2
  cat /tmp/qemu-root-unauth.hdr >&2 || true
  exit 1
fi
api_unauth_code="$(curl -sS -o /dev/null -w '%{http_code}' -H "Host: ${AUTH_HOST}" "http://127.0.0.1:${HOST_PORT}/api/data")"
[[ "${api_unauth_code}" == "302" ]] || {
  echo "GET /api/data unauthenticated: expected 302, got ${api_unauth_code}" >&2; exit 1;
}
# The full snapshot moved behind the gate in step 5, so it must be gated too.
system_unauth_code="$(curl -sS -o /dev/null -w '%{http_code}' -H "Host: ${AUTH_HOST}" "http://127.0.0.1:${HOST_PORT}/api/system")"
[[ "${system_unauth_code}" == "302" ]] || {
  echo "GET /api/system unauthenticated: expected 302, got ${system_unauth_code}" >&2; exit 1;
}

# --- The regression M6.6 step 3 exists to fix, end to end through real nginx -----
# A 302 here is the bug: fetch() follows it and hands back login HTML at HTTP 200,
# so the portal's `if (!res.ok)` never fires and an expired session reads as dead
# UI (or, worse, as "managed by Lemonseed") instead of a sign-in prompt.
echo "==> Auth gate: an expired or absent session answers an XHR with a real 401"
xhr_code="$(curl -sS -o /tmp/qemu-xhr-401.json -D /tmp/qemu-xhr-401.hdr -w '%{http_code}' \
  -H "Host: ${AUTH_HOST}" -H 'X-Requested-With: fetch' \
  "http://127.0.0.1:${HOST_PORT}/api/system")"
[[ "${xhr_code}" == "401" ]] || {
  echo "XHR /api/system unauthenticated: expected 401, got ${xhr_code}" >&2
  cat /tmp/qemu-xhr-401.hdr >&2 || true
  exit 1
}
grep -qi 'content-type: application/json' /tmp/qemu-xhr-401.hdr || {
  echo "the XHR 401 is not json:" >&2; cat /tmp/qemu-xhr-401.hdr >&2; exit 1;
}
python3 -c '
import json
d = json.load(open("/tmp/qemu-xhr-401.json"))
assert d.get("lemonseed") == "session-expired", d
assert (d.get("detail") or "").strip(), d
print("OK: 401 + renderable json the portal can act on")
'

echo "==> Authenticated requests (session cookie)"
# --resolve maps the mDNS hostname to the QEMU host-forward port so curl sends
# the session cookie (domain-matched) while connecting to 127.0.0.1.
# NOTE: /settings/ (admin-gated) is deliberately NOT probed here — it 403s in
# the QEMU test env (Host/--resolve port + cookie-domain interaction) while
# working on a real appliance. Verified on hardware instead (DESIGN M6.2).
for p in / /api/data /api/system; do
  code="$(curl -sS -o /dev/null -b "${COOKIE_JAR}" \
    --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
    -w '%{http_code}' "http://${AUTH_HOST}:${HOST_PORT}${p}")"
  [[ "${code}" == "200" ]] || {
    echo "GET ${p} with session: expected 200, got ${code}" >&2; exit 1;
  }
done

echo "==> M6.5 model picker: GET /_lemonseed/api/models (auth + admin gated)"
# The default QEMU guest runs with 16 GiB RAM (CPU-only). The e2e run selects
# Gemma 12B explicitly (see the POST below); the default run only records the
# planner outcome for the 16 GiB CPU box.
models_code="$(curl -sS -o /tmp/qemu-models-body.json -b "${COOKIE_JAR}" \
  --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
  -w '%{http_code}' "http://${AUTH_HOST}:${HOST_PORT}/_lemonseed/api/models")"
[[ "${models_code}" == "200" ]] || {
  echo "GET /_lemonseed/api/models with session: expected 200, got ${models_code}" >&2; exit 1;
}
E2E_CHAT="${E2E_CHAT}" python3 -c '
import json, os
d = json.load(open("/tmp/qemu-models-body.json"))
fams = d.get("families") or []
assert isinstance(fams, list) and len(fams) >= 3, d
ids = {str(f.get("id")) for f in fams}
assert "gemma" in ids, ids
for f in fams:
    assert "fits" in f and "best_fit" in f and "models" in f, f
plan = d.get("plan") or {}
assert plan.get("state") in ("selected", "not-ready"), plan
if os.environ.get("E2E_CHAT") == "1":
    # 16 GiB e2e guest: the planner must select a Gemma (the explicit 12B
    # choice is written by the POST below; the pre-choice plan is the default
    # family best-fit for this box).
    assert plan.get("state") == "selected", plan
    assert str(plan.get("family")) == "gemma", plan
    print("OK: /_lemonseed/api/models families=%d plan.state=selected selection=%s" % (len(fams), (plan.get("selection") or {}).get("id")))
else:
    # Default run: just record the outcome (not-ready below ~10 GiB, selected at 16 GiB).
    print("OK: /_lemonseed/api/models families=%d plan.state=%s (CPU path)" % (len(fams), plan.get("state")))
'

echo "==> Open endpoints stay open (unauthenticated)"
for p in /healthz /api/status; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${HOST_PORT}${p}")"
  [[ "${code}" == "200" ]] || {
    echo "GET ${p} unauthenticated: expected 200, got ${code}" >&2; exit 1;
  }
done
# The shared theme (M6.6 S5.8) styles the login and setup pages, so it must be
# readable with no session and served by nginx out of the image -- not proxied to
# the control plane, which the gateway's not-ready page also depends on.
css_code="$(curl -sS -o /tmp/qemu-theme.css -D /tmp/qemu-theme.hdr -w '%{http_code}' \
  "http://127.0.0.1:${HOST_PORT}/_lemonseed/lemonseed.css")"
[[ "${css_code}" == "200" ]] || {
  echo "GET /_lemonseed/lemonseed.css unauthenticated: expected 200, got ${css_code}" >&2
  exit 1
}
grep -qi 'content-type: text/css' /tmp/qemu-theme.hdr || {
  echo "the theme is not served as css:" >&2; cat /tmp/qemu-theme.hdr >&2; exit 1
}
grep -q -- '--accent' /tmp/qemu-theme.css || {
  echo "the theme served by nginx has no palette variables" >&2; exit 1
}
echo "OK: open endpoints answer, including the shared theme"

if [[ "${E2E_CHAT}" == "1" ]]; then
  echo "==> M6.5 e2e: pick Gemma + pull/load the model (state=ready)"
  # Write the model choice (admin, session cookie). This clears any stale
  # give-up decision and spawns the chat worker, which runs `lemonade pull`
  # (cached on the persistent userdata disk after the first run) and
  # `lemonade load --llamacpp cpu --pinned`. At 16 GiB CPU the planner picks
  # Gemma E4B (the largest Gemma that fits: 8+6=14 GiB); the 6 GB download
  # also gives the GC test below a real cached model to evict.
  choice_code="$(curl -sS -o /tmp/qemu-models-choice.json -b "${COOKIE_JAR}" \
    --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
    -w '%{http_code}' \
    -X POST "http://${AUTH_HOST}:${HOST_PORT}/_lemonseed/api/models" \
    -H 'Content-Type: application/json' \
    -d '{"family":"gemma"}')"
  [[ "${choice_code}" == "200" ]] || {
    echo "POST /_lemonseed/api/models (family=gemma): expected 200, got ${choice_code}" >&2
    cat /tmp/qemu-models-choice.json >&2 || true
    exit 1
  }
  echo "OK: model choice written (gemma)"

  # Poll /api/chat until the model is loaded and verified (state=ready).
  # The pull is the long pole on a cold run; use the remaining budget.
  chat_deadline=$((deadline))
  chat_json=""
  while (( SECONDS < chat_deadline )); do
    if candidate="$(poll_http "/api/chat" 30 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null)"; then
      ready="$(echo "${candidate}" | python3 -c '
import json, sys
raw = sys.stdin.read()
if not raw.strip():
    print("no"); sys.exit(0)
try:
    d = json.loads(raw)
except ValueError:
    print("no"); sys.exit(0)
state = d.get("state")
if state == "ready":
    print("yes")
else:
    print("no")
    prog = d.get("progress"); mid = d.get("model_id"); err = d.get("error"); rsn = d.get("reason")
    print("waiting: state=%r progress=%r model=%r error=%r reason=%r" % (state, prog, mid, err, rsn), file=sys.stderr)
' 2>/tmp/qemu-chat-wait.err || true)"
      cat /tmp/qemu-chat-wait.err >&2 || true
      if [[ "${ready}" == "yes" ]]; then
        chat_json="${candidate}"
        break
      fi
    fi
    if ! kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
      echo "QEMU exited during model-load e2e; tail ${LOG}:" >&2
      tail -80 "${LOG}" >&2 || true
      exit 1
    fi
    sleep 10
  done

  if [[ -z "${chat_json}" ]]; then
    echo "Timed out waiting for /api/chat state=ready (model pull/load)" >&2
    curl -fsS --max-time 5 "http://127.0.0.1:${HOST_PORT}/api/chat" >&2 || true
    tail -80 "${LOG}" >&2 || true
    exit 1
  fi

  echo "${chat_json}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("state") == "ready", d
assert str(d.get("model_id")) == "Gemma-4-E4B-it-GGUF", d
assert d.get("progress") == 100, d
print("OK: local chat ready model=%s display=%s" % (d.get("model_id"), d.get("display_name")))
'

  echo "==> M6.5 e2e GC: fill the model volume, pull a second model, expect eviction"
  # Disk-space guard e2e (unit tests cover the algorithm; this proves the
  # wiring on a real appliance):
  #   1. Gemma E4B is loaded (above) — the pinned model is never evicted.
  #   2. Pull LFM2.5 8B A1B (fits the 16 GiB CPU box) — a second cached model.
  #   3. Fill /var (shared with the model cache) with a dd file until the
  #      filesystem is ~95% full — past the 90% high-water mark.
  #   4. Pull LFM2.5 1.2B: the guard must evict the LRU cached model (LFM2.5
  #      8B, the only non-protected, non-loaded model) and complete.
  #   5. Verify: LFM2.5 1.2B ready, LFM2.5 8B gone from lemond's list, the
  #      pinned Gemma E4B still present, and the filler removed.
  # Opt-in via QEMU_E2E_GC=1 (adds ~13 GB of downloads; the userdata disk is
  # 32 GB, so run the e2e GC on a clean out/qemu-userdata.img).
  if [[ "${QEMU_E2E_GC:-0}" == "1" ]]; then
    echo "==> GC: pull LFM2.5 8B A1B (second cached model)"
    gc_choice_code="$(curl -sS -o /tmp/qemu-models-gc1.json -b "${COOKIE_JAR}" \
      --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
      -w '%{http_code}' \
      -X POST "http://${AUTH_HOST}:${HOST_PORT}/_lemonseed/api/models" \
      -H 'Content-Type: application/json' \
      -d '{"family":"lfm2","model_id":"LFM2.5-8B-A1B"}')"
    [[ "${gc_choice_code}" == "200" ]] || {
      echo "POST /_lemonseed/api/models (lfm2 8B): expected 200, got ${gc_choice_code}" >&2
      cat /tmp/qemu-models-gc1.json >&2 || true
      exit 1
    }
    gc_deadline=$((deadline))
    gc_ready=""
    while (( SECONDS < gc_deadline )); do
      if candidate="$(poll_http "/api/chat" 30 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null)"; then
        state="$(echo "${candidate}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
state = d.get("state") or ""
print(state)
if state != "ready":
    print("waiting: state=%s model=%s progress=%s error=%s task=%s" % (
        state, d.get("model_id"), d.get("progress"),
        (d.get("error") or "")[:200], (d.get("task") or "")[:100]), file=sys.stderr)
' 2>/tmp/qemu-gc-wait.err || true)"
        cat /tmp/qemu-gc-wait.err >&2 || true
        if [[ "${state}" == "ready" ]]; then
          gc_ready="${candidate}"
          break
        fi
      fi
      if ! kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
        echo "QEMU exited during GC e2e (pull 8B); tail ${LOG}:" >&2
        tail -80 "${LOG}" >&2 || true
        exit 1
      fi
      sleep 10
    done
    [[ -n "${gc_ready}" ]] || { echo "Timed out waiting for LFM2.5 8B ready" >&2; exit 1; }
    echo "${gc_ready}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("state") == "ready", d
assert str(d.get("model_id")) == "LFM2.5-8B-A1B", d
print("OK: LFM2.5 8B A1B ready")
'
    # LFM2.5 8B is now the pinned (loaded) model; Gemma E4B is the cached
    # fallback (most-recent prior model).

    echo "==> GC: fill /var to ~95% (dd filler in /var/tmp)"
    # 32 GB volume, ~15 GB used by the two models + system: a 10 GB filler
    # pushes the filesystem past the 90% high-water mark with headroom.
    fill_code="$(curl -sS -o /tmp/qemu-gc-fill.json -b "${COOKIE_JAR}" \
      --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
      -w '%{http_code}' \
      -X POST "http://${AUTH_HOST}:${HOST_PORT}/_lemonseed/api/gc-test/fill" \
      -H 'Content-Type: application/json' \
      -d '{"bytes": 10737418240}')"
    [[ "${fill_code}" == "200" ]] || {
      echo "POST /_lemonseed/api/gc-test/fill: expected 200, got ${fill_code}" >&2
      cat /tmp/qemu-gc-fill.json >&2 || true
      exit 1
    }
    echo "OK: filler written"

    echo "==> GC: pull LFM2.5 1.2B (expect LRU eviction of Gemma E4B; 8B kept as fallback)"
    gc_choice_code="$(curl -sS -o /tmp/qemu-models-gc2.json -b "${COOKIE_JAR}" \
      --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
      -w '%{http_code}' \
      -X POST "http://${AUTH_HOST}:${HOST_PORT}/_lemonseed/api/models" \
      -H 'Content-Type: application/json' \
      -d '{"family":"lfm2","model_id":"LFM2.5-1.2B-Instruct-GGUF"}')"
    [[ "${gc_choice_code}" == "200" ]] || {
      echo "POST /_lemonseed/api/models (lfm2 1.2B): expected 200, got ${gc_choice_code}" >&2
      cat /tmp/qemu-models-gc2.json >&2 || true
      exit 1
    }
    gc_deadline=$((deadline))
    gc_ready=""
    while (( SECONDS < gc_deadline )); do
      if candidate="$(poll_http "/api/chat" 30 "${COOKIE_JAR}" "${AUTH_HOST}" 2>/dev/null)"; then
        state="$(echo "${candidate}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
state = d.get("state") or ""
print(state)
if state != "ready":
    print("waiting: state=%s model=%s progress=%s error=%s task=%s" % (
        state, d.get("model_id"), d.get("progress"),
        (d.get("error") or "")[:200], (d.get("task") or "")[:100]), file=sys.stderr)
' 2>/tmp/qemu-gc-wait.err || true)"
        cat /tmp/qemu-gc-wait.err >&2 || true
        if [[ "${state}" == "ready" ]]; then
          gc_ready="${candidate}"
          break
        fi
      fi
      if ! kill -0 "$(cat "${PIDFILE}")" 2>/dev/null; then
        echo "QEMU exited during GC e2e (pull 1.2B); tail ${LOG}:" >&2
        tail -80 "${LOG}" >&2 || true
        exit 1
      fi
      sleep 10
    done
    [[ -n "${gc_ready}" ]] || { echo "Timed out waiting for LFM2.5 1.2B ready (GC)" >&2; exit 1; }
    echo "${gc_ready}" | python3 -c '
import json, sys
d = json.load(sys.stdin)
assert d.get("state") == "ready", d
assert str(d.get("model_id")) == "LFM2.5-1.2B-Instruct-GGUF", d
print("OK: LFM2.5 1.2B ready after GC")
'

    echo "==> GC: verify eviction (lemond model list)"
    # lemond's /v1/models lists the cached (downloaded) models. Gemma E4B must
    # be gone (evicted by the guard — it is the LRU cached model, older than
    # LFM2.5 8B); LFM2.5 8B (pinned) and LFM2.5 1.2B (new) must be present.
    list_code="$(curl -sS -o /tmp/qemu-gc-list.json -b "${COOKIE_JAR}" \
      --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
      -w '%{http_code}' "http://${AUTH_HOST}:${HOST_PORT}/_lemonseed/api/gc-test/models")"
    [[ "${list_code}" == "200" ]] || {
      echo "GET /_lemonseed/api/gc-test/models: expected 200, got ${list_code}" >&2
      exit 1
    }
    python3 -c '
import json
d = json.load(open("/tmp/qemu-gc-list.json"))
ids = {str(m.get("id")) for m in (d.get("data") or [])}
assert "LFM2.5-1.2B-Instruct-GGUF" in ids, ids
assert "LFM2.5-8B-A1B" in ids, ids
assert "Gemma-4-E4B-it-GGUF" not in ids, ids
print("OK: GC evicted Gemma E4B (LRU); pinned LFM2.5 8B + new 1.2B present")
'

    echo "==> GC: remove filler"
    rm_code="$(curl -sS -o /tmp/qemu-gc-rm.json -b "${COOKIE_JAR}" \
      --resolve "${AUTH_HOST}:${HOST_PORT}:127.0.0.1" \
      -w '%{http_code}' \
      -X POST "http://${AUTH_HOST}:${HOST_PORT}/_lemonseed/api/gc-test/fill/remove")"
    [[ "${rm_code}" == "200" ]] || {
      echo "POST /_lemonseed/api/gc-test/fill/remove: expected 200, got ${rm_code}" >&2
      exit 1
    }
    echo "OK: filler removed"
  fi
fi

echo "==> M6.6 S5: the boot path no longer pulls, and the gateway is not first"

# (a) Both start scripts must report the image reference they are using, on
# whichever path a real boot takes (dockerd's restart policy usually starts the
# containers before the unit runs). On this profile the images arrive as
# dockerTools.pullImage tarballs, which carry RepoTags but no RepoDigests, so
# the answer must be the *tag* — that is the digest->tag fallback running for
# real, not just in a unit test. Expected names come from the pins file, never
# a copy in this script.
pins_json="${ROOT}/control-plane/container-images.json"
if [[ ! -f "${pins_json}" ]]; then
  echo "missing ${pins_json}" >&2
  exit 1
fi
read -r POSTGRES_IMAGE VALKEY_IMAGE <<<"$(python3 -c '
import json, sys
pins = json.load(open(sys.argv[1]))
print(pins["postgres"]["name"], pins["valkey"]["name"])
' "${pins_json}")" || true
if [[ -z "${POSTGRES_IMAGE}" || -z "${VALKEY_IMAGE}" ]]; then
  echo "could not read postgres/valkey image names from ${pins_json}" >&2
  exit 1
fi
for unit in lemonseed-postgres lemonseed-valkey; do
  case "${unit}" in
    lemonseed-postgres) image_name="${POSTGRES_IMAGE}" ;;
    lemonseed-valkey) image_name="${VALKEY_IMAGE}" ;;
  esac
  grep -qE "${unit}: image ${image_name}:" "${LOG}" || {
    echo "serial log has no '${unit}: image ${image_name}:<tag>' line" >&2
    echo "  (expected the tag form: the QEMU images are loaded from pullImage tarballs)" >&2
    exit 1
  }
  if grep -qE "${unit}: image ${image_name}@sha256:" "${LOG}"; then
    echo "serial log shows ${unit} using ${image_name} by digest on a profile that only has tags" >&2
    exit 1
  fi
  echo "  ok: ${unit} is on a local tag reference"
done

# (b) nginx must have waited (bounded) for the control plane before opening :80.
grep -qE 'lemonseed-gateway: control plane (is serving|not serving after)' "${LOG}" || {
  echo "serial log has no lemonseed-gateway wait line — nginx started without polling the CP" >&2
  exit 1
}
echo "  ok: the gateway polled the control plane before opening :80"

# (c) The fallback must not be fetchable from the LAN. Its 404 also proves the
# location exists as `internal` rather than being absent.
cp_down_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 \
  "http://127.0.0.1:${HOST_PORT}/_lemonseed/cp-down" 2>/dev/null || echo 000)"
if [[ "${cp_down_code}" != "404" ]]; then
  echo "GET /_lemonseed/cp-down: expected 404 (internal), got ${cp_down_code}" >&2
  exit 1
fi
echo "OK: 5xx contract in place; the not-ready page is not directly reachable"

echo "==> QEMU API smoke test PASSED"
