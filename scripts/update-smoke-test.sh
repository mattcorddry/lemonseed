#!/usr/bin/env bash
# Loopback smoke test for A/B updater: seed slot A, apply to B, verify grubenv
# and post-write hashes. Does not boot QEMU (fast CI/lab check).
#
# Runs as the invoking user; sudo is invoked narrowly, per command, for the
# block-device operations (losetup/partx/mkfs/mount/dd) and for the updater
# apply itself — which is realistic, because the packaged updater runs as root.
#
# Usage:
#   scripts/update-smoke-test.sh
#   scripts/update-smoke-test.sh --keep
#
# Host requirements (Ubuntu): systemd-repart, dosfstools, util-linux, python3,
# openssl. sudo must be passwordless or will prompt once.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${ROOT}/out/update-smoke.img"
WORKDIR="${ROOT}/out/update-smoke"
KEEP=0
LOOP=""

usage() {
  cat <<'EOF'
usage: update-smoke-test.sh [--keep]

  --keep   Leave out/update-smoke.img and workdir on success
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep) KEEP=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1" >&2; usage ;;
  esac
done

# Non-root run by design; sudo is invoked per command below. Fail fast when a
# password would be prompted for, so a CI run never hangs on a prompt.
if [[ "${EUID}" -ne 0 ]] && ! sudo -n true 2>/dev/null; then
  echo "update-smoke-test: passwordless sudo required (block-device ops only)" >&2
  exit 1
fi

for tool in systemd-repart mkfs.vfat losetup partx blockdev udevadm; do
  if ! command -v "${tool}" >/dev/null 2>&1; then
    echo "update-smoke-test: missing host tool: ${tool}" >&2
    exit 1
  fi
done

export PYTHONPATH="${ROOT}/scripts"
# The updater verifies release signatures against the baked trust set (M8 P1).
# The smoke signs its local artifact sets against the tracked QEMU-only trust
# set (private key tracked on purpose; never a release key).
export LEMONSEED_RELEASE_KEYS_DIR="${ROOT}/keys/qemu"
# The packaged updater exports this (nixos/packages/lemonseed-update.nix); the
# smoke needs the same template or the arm step refuses.
export LEMONSEED_GRUB_CFG="${ROOT}/boot/grub.cfg"
export LEMONSEED_AB="${ROOT}/scripts/lemonseed-ab.sh"
# The updater's bookkeeping stays in the workdir: a smoke run must not leave
# attempt records or a slot registry in the host's /var/lib/lemonseed.
export LEMONSEED_ATTEMPT_FILE="${WORKDIR}/update-attempt.json"
export LEMONSEED_LATEST_FILE="${WORKDIR}/latest-update.json"
export LEMONSEED_SLOT_VERSIONS_FILE="${WORKDIR}/slot-versions.json"

# Env the root-spawned updater stages need (the packaged updater exports these;
# the smoke reproduces that environment explicitly).
SUDO_ENV=(
  env "PATH=${PATH}" "PYTHONPATH=${PYTHONPATH}"
  "LEMONSEED_AB=${LEMONSEED_AB}" "LEMONSEED_RELEASE_KEYS_DIR=${LEMONSEED_RELEASE_KEYS_DIR}"
  "LEMONSEED_GRUB_CFG=${LEMONSEED_GRUB_CFG}"
  "LEMONSEED_ATTEMPT_FILE=${LEMONSEED_ATTEMPT_FILE}"
  "LEMONSEED_LATEST_FILE=${LEMONSEED_LATEST_FILE}"
  "LEMONSEED_SLOT_VERSIONS_FILE=${LEMONSEED_SLOT_VERSIONS_FILE}"
  "LEMONSEED_DOWNLOAD_DIR=${WORKDIR}/dl"
)

sign_manifest() {  # sign_manifest <dir> — add signing metadata, sign in place
  python3 - "$1" "${ROOT}/keys/qemu/qemu-release-v1.priv" <<'PYSIGN'
import json
import subprocess
import sys
from pathlib import Path

d = Path(sys.argv[1])
priv = Path(sys.argv[2])
mp = d / "manifest.json"
manifest = json.loads(mp.read_text(encoding="utf-8"))
manifest["signing"] = {
    "scheme": "detached-ecdsa-p256",
    "enabled": True,
    "public_key": "qemu-release-v1.pub",
}
mp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
subprocess.run(
    ["openssl", "pkeyutl", "-sign", "-inkey", str(priv), "-rawin",
     "-in", str(mp), "-out", str(mp) + ".sig"],
    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
print("signed", mp)
PYSIGN
}

detach_image_loops() {
  local img="$1"
  local loops loop
  [[ -e "${img}" ]] || return 0
  loops="$(sudo losetup -j "${img}" -n -O NAME 2>/dev/null || true)"
  [[ -n "${loops}" ]] || return 0
  while IFS= read -r loop; do
    [[ -n "${loop}" ]] || continue
    sudo partx -d "${loop}" 2>/dev/null || true
    sudo losetup -d "${loop}" 2>/dev/null || true
  done <<<"${loops}"
}

cleanup() {
  if [[ -n "${LOOP}" ]]; then
    sudo partx -d "${LOOP}" 2>/dev/null || true
    sudo losetup -d "${LOOP}" 2>/dev/null || true
    LOOP=""
  fi
  detach_image_loops "${IMAGE}"
  if [[ "${KEEP}" -eq 0 ]]; then
    # Root-owned debris (updater download dir, arm-time records) needs root.
    sudo rm -rf "${WORKDIR}"
    sudo rm -f "${IMAGE}"
  else
    sudo chown -R "$(id -u):$(id -g)" "${WORKDIR}" "${IMAGE}" 2>/dev/null || true
  fi
}
trap cleanup EXIT

mkdir -p "${ROOT}/out" "${WORKDIR}"
rm -rf "${WORKDIR}"
mkdir -p "${WORKDIR}/artifacts" "${WORKDIR}/esp" "${WORKDIR}/dl"

echo "==> generating repart definitions"
"${ROOT}/scripts/gen-repart.py" -o "${ROOT}/out/repart.d"

echo "==> creating sparse 20G image"
truncate -s 20G "${IMAGE}"
detach_image_loops "${IMAGE}"

echo "==> applying GPT layout"
systemd-repart --empty=force --dry-run=no --definitions="${ROOT}/out/repart.d" "${IMAGE}"

LOOP="$(sudo losetup -f --show -P "${IMAGE}")"
udevadm settle -t 10 2>/dev/null || true
# Wait for partition nodes.
n=0
while [[ ! -e "${LOOP}p2" || ! -e "${LOOP}p3" || ! -e "${LOOP}p1" ]]; do
  if [[ "${n}" -ge 40 ]]; then
    echo "partition nodes did not appear under ${LOOP}" >&2
    ls -l "${LOOP}"* >&2 || true
    exit 1
  fi
  sleep 0.25
  n=$((n + 1))
done

# Map PARTLABELs via temporary udev-less links for the test harness.
ESP="${LOOP}p1"
ROOT_A="${LOOP}p2"
ROOT_B="${LOOP}p3"
sudo mkdir -p /dev/disk/by-partlabel
sudo ln -sfn "${ESP}" /dev/disk/by-partlabel/ESP
sudo ln -sfn "${ROOT_A}" /dev/disk/by-partlabel/root-a
sudo ln -sfn "${ROOT_B}" /dev/disk/by-partlabel/root-b

# Fake artifacts sized well under 4G.
python3 - "${WORKDIR}/artifacts" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
files = {
    "vmlinuz": b"vmlinuz-slot-a\n" * 1000,
    "initrd.img": b"initrd-slot-a\n" * 1000,
    "root.squashfs": b"SQUASHFS-A" + (b"\0" * (1024 * 1024)),
}
manifest = {"version": "smoke-a", "artifacts": {}}
for name, body in files.items():
    (root / name).write_bytes(body)
    manifest["artifacts"][name] = {
        "path": name,
        "length": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
(root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print("wrote artifacts", root)
PY
sign_manifest "${WORKDIR}/artifacts"

echo "==> seeding slot A + ESP"
sudo dd if="${WORKDIR}/artifacts/root.squashfs" of="${ROOT_A}" bs=4M status=none conv=fsync
sudo mkfs.vfat -F 32 -n ESP "${ESP}" >/dev/null
# umask=000: the ESP is a loop-backed test fixture, so the unprivileged file
# operations below (cp, lemonseed-ab) need no sudo of their own.
sudo mount -t vfat -o rw,umask=000 "${ESP}" "${WORKDIR}/esp"
mkdir -p "${WORKDIR}/esp/EFI/lemonseed" "${WORKDIR}/esp/grub"
cp "${ROOT}/boot/grub.cfg" "${WORKDIR}/esp/grub/grub.cfg"
cp "${WORKDIR}/artifacts/vmlinuz" "${WORKDIR}/esp/EFI/lemonseed/vmlinuz-a"
cp "${WORKDIR}/artifacts/initrd.img" "${WORKDIR}/esp/EFI/lemonseed/initrd-a.img"
bash "${ROOT}/scripts/lemonseed-ab.sh" init --esp "${WORKDIR}/esp"
sudo umount "${WORKDIR}/esp"

# Build B artifacts with different content.
python3 - "${WORKDIR}/artifacts-b" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
files = {
    "vmlinuz": b"vmlinuz-slot-b\n" * 1000,
    "initrd.img": b"initrd-slot-b\n" * 1000,
    "root.squashfs": b"SQUASHFS-B" + (b"\0" * (1024 * 1024)),
}
manifest = {"version": "smoke-b", "artifacts": {}}
for name, body in files.items():
    (root / name).write_bytes(body)
    manifest["artifacts"][name] = {
        "path": name,
        "length": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
(root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY
sign_manifest "${WORKDIR}/artifacts-b"

# Pretend we are running on slot A.
export LEMONSEED_DOWNLOAD_DIR="${WORKDIR}/dl"
# The apply writes the inactive slot partition and the ESP — exactly the
# privileged operations the packaged updater performs as root in production,
# so the updater stages run under sudo with the environment it expects.
sudo "${SUDO_ENV[@]}" python3 - "${WORKDIR}/artifacts-b" <<'PY'
import os, sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ["PYTHONPATH"])
import lemonseed_update as update

arts = Path(sys.argv[1])
with mock.patch.object(update, "current_slot", return_value="a"), mock.patch.object(
    update, "cmdline_slot", return_value="a"
):
    rc = update.cmd_apply(None, str(arts), reboot=False, force=False)
    assert rc == 0, rc
print("apply ok")
PY

# Verify B contents and grubenv.
B_HASH="$(sudo python3 - "${WORKDIR}/artifacts-b/root.squashfs" "${ROOT_B}" <<'PY'
import hashlib, sys
from pathlib import Path
src = Path(sys.argv[1]).read_bytes()
size = len(src)
with open(sys.argv[2], "rb") as fh:
    got = fh.read(size)
assert got == src, "root-b content mismatch"
print(hashlib.sha256(got).hexdigest())
PY
)"
echo "root-b hash ${B_HASH}"

sudo mount -t vfat -o rw,umask=000 "${ESP}" "${WORKDIR}/esp"
STATUS="$(bash "${ROOT}/scripts/lemonseed-ab.sh" status --esp "${WORKDIR}/esp")"
echo "${STATUS}"
echo "${STATUS}" | grep -q 'boot_default=a'
echo "${STATUS}" | grep -q 'boot_once=b'
test -f "${WORKDIR}/esp/EFI/lemonseed/vmlinuz-b"
test -f "${WORKDIR}/esp/EFI/lemonseed/initrd-b.img"

# Cancel pending update → boot_default stays a, once cleared.
bash "${ROOT}/scripts/lemonseed-ab.sh" clear-once --esp "${WORKDIR}/esp"
STATUS2="$(bash "${ROOT}/scripts/lemonseed-ab.sh" status --esp "${WORKDIR}/esp")"
echo "${STATUS2}"
echo "${STATUS2}" | grep -q 'boot_default=a'
if echo "${STATUS2}" | grep -q 'boot_once=b'; then
  echo "FAIL: boot_once still set after clear-once" >&2
  exit 1
fi
sudo umount "${WORKDIR}/esp"

# Rollback round trip (M8 P6, D2/D30): the apply put smoke-b on slot B and
# recorded it; fleeing back is a boot-pointer flip, not a write. The updater
# stages run as root (same as apply), with the slot registry and attempt
# record isolated to the workdir via the LEMONSEED_* env above.
echo "==> rollback to slot B (the prior slot)"
sudo "${SUDO_ENV[@]}" python3 - <<'PY'
import os, sys
from unittest import mock

sys.path.insert(0, os.environ["PYTHONPATH"])
import lemonseed_update as update

# The panel's *cancel* runs this before any new verb; the attempt record left
# by the apply above is landed success, and the rollback must proceed.
update.clear_attempt()
with mock.patch.object(update, "current_slot", return_value="a"), mock.patch.object(
    update, "cmdline_slot", return_value="a"
), mock.patch.object(update, "running_version", return_value="smoke-a"):
    rc = update.cmd_rollback(reboot=False, force=False)
    assert rc == 0, rc
print("rollback ok")
PY

# The flip: default is now the prior slot, no boot_once pending, and the
# grubenv carries the version label the boot menu expands.
sudo mount -t vfat -o rw,umask=000 "${ESP}" "${WORKDIR}/esp"
STATUS_RB="$(bash "${ROOT}/scripts/lemonseed-ab.sh" status --esp "${WORKDIR}/esp")"
echo "${STATUS_RB}"
echo "${STATUS_RB}" | grep -q 'boot_default=b'
if echo "${STATUS_RB}" | grep -q 'boot_once=b'; then
  echo "FAIL: rollback left boot_once pending" >&2
  exit 1
fi
grep -q "slot_b_version=smoke-b" "${WORKDIR}/esp/grub/grubenv"
sudo umount "${WORKDIR}/esp"
python3 -c '
import json, os
from pathlib import Path
registry = json.loads(Path(os.environ["LEMONSEED_SLOT_VERSIONS_FILE"]).read_text(encoding="utf-8"))
assert registry["slots"]["b"]["version"] == "smoke-b", registry
assert registry["rolled_back_version"] == "smoke-a", registry
print("registry honest after rollback")
'

# Re-apply B (--force is the deliberate override of the rollback marker's
# suppression), then cancel so the fixture ends clean.
echo "==> re-applying smoke-b over the rolled-back state"
sudo "${SUDO_ENV[@]}" python3 - "${WORKDIR}/artifacts-b" <<'PY'
import os, sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ["PYTHONPATH"])
import lemonseed_update as update

with mock.patch.object(update, "current_slot", return_value="a"), mock.patch.object(
    update, "cmdline_slot", return_value="a"
), mock.patch.object(update, "running_version", return_value="smoke-a"):
    rc = update.cmd_apply(None, sys.argv[1], reboot=False, force=True)
    assert rc == 0, rc
print("re-apply ok")
PY
sudo "${SUDO_ENV[@]}" python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import lemonseed_update as update
update.cmd_cancel()
print("cancel ok")
' "${PYTHONPATH}"
python3 -c '
import json, os
from pathlib import Path
registry = json.loads(Path(os.environ["LEMONSEED_SLOT_VERSIONS_FILE"]).read_text(encoding="utf-8"))
assert registry["rolled_back_version"] is None, registry
assert registry["slots"]["b"]["version"] == "smoke-b", registry
print("apply cleared the rollback marker")
'

# Checksum mismatch refusal.
python3 - "${WORKDIR}/artifacts-bad" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
(root / "vmlinuz").write_bytes(b"k")
(root / "initrd.img").write_bytes(b"i")
(root / "root.squashfs").write_bytes(b"bad-squash")
manifest = {
    "version": "bad",
    "artifacts": {
        "vmlinuz": {"path": "vmlinuz", "sha256": "11" * 32},
        "initrd.img": {"path": "initrd.img", "sha256": hashlib.sha256(b"i").hexdigest()},
        "root.squashfs": {
            "path": "root.squashfs",
            "sha256": hashlib.sha256(b"bad-squash").hexdigest(),
        },
    },
}
(root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY
sign_manifest "${WORKDIR}/artifacts-bad"

if sudo "${SUDO_ENV[@]}" python3 - "${WORKDIR}/artifacts-bad" <<'PY'
import os, sys
from unittest import mock
sys.path.insert(0, os.environ["PYTHONPATH"])
import lemonseed_update as update
from lemonseed_artifacts import ArtifactError
update.AB_HELPER = os.environ["LEMONSEED_AB"]
with mock.patch.object(update, "current_slot", return_value="a"):
    try:
        update.cmd_apply(None, sys.argv[1], reboot=False, force=True)
    except SystemExit as exc:
        raise SystemExit(exc.code)
    except ArtifactError:
        raise SystemExit(0)
    except update.UpdateError:
        raise SystemExit(0)
raise SystemExit("expected checksum failure")
PY
then
  echo "checksum refusal ok"
else
  echo "FAIL: expected checksum mismatch" >&2
  exit 1
fi

echo "OK: update smoke passed (slot B armed, rollback flips the default back, re-apply clears the marker, bad checksum refused)"
