#!/usr/bin/env bash
# Loopback smoke test for A/B updater: seed slot A, apply to B, verify grubenv
# and post-write hashes. Does not boot QEMU (fast CI/lab check).
#
# Usage:
#   sudo scripts/update-smoke-test.sh
#   sudo scripts/update-smoke-test.sh --keep
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

if [[ "${EUID}" -ne 0 ]]; then
  echo "update-smoke-test: run as root (sudo)" >&2
  exit 1
fi

export PATH="/nix/var/nix/profiles/default/bin:${PATH}"
export PYTHONPATH="${ROOT}/scripts"

detach_image_loops() {
  local img="$1"
  local loops loop
  [[ -e "${img}" ]] || return 0
  loops="$(losetup -j "${img}" -n -O NAME 2>/dev/null || true)"
  [[ -n "${loops}" ]] || return 0
  while IFS= read -r loop; do
    [[ -n "${loop}" ]] || continue
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
  if [[ "${KEEP}" -eq 0 ]]; then
    rm -rf "${WORKDIR}"
    rm -f "${IMAGE}"
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
if command -v systemd-repart >/dev/null; then
  systemd-repart --empty=force --dry-run=no --definitions="${ROOT}/out/repart.d" "${IMAGE}"
else
  nix-shell -p systemd dosfstools util-linux --run \
    "systemd-repart --empty=force --dry-run=no --definitions=$(printf '%q' "${ROOT}/out/repart.d") $(printf '%q' "${IMAGE}")"
fi

LOOP="$(losetup -f --show -P "${IMAGE}")"
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
mkdir -p /dev/disk/by-partlabel
ln -sfn "${ESP}" /dev/disk/by-partlabel/ESP
ln -sfn "${ROOT_A}" /dev/disk/by-partlabel/root-a
ln -sfn "${ROOT_B}" /dev/disk/by-partlabel/root-b

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
        "sha256": hashlib.sha256(body).hexdigest(),
    }
(root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print("wrote artifacts", root)
PY

echo "==> seeding slot A + ESP"
dd if="${WORKDIR}/artifacts/root.squashfs" of="${ROOT_A}" bs=4M status=none conv=fsync
mkfs.vfat -F 32 -n ESP "${ESP}" >/dev/null
mount -t vfat -o rw,umask=0077 "${ESP}" "${WORKDIR}/esp"
mkdir -p "${WORKDIR}/esp/EFI/lemonseed" "${WORKDIR}/esp/grub"
cp "${ROOT}/boot/grub.cfg" "${WORKDIR}/esp/grub/grub.cfg"
cp "${WORKDIR}/artifacts/vmlinuz" "${WORKDIR}/esp/EFI/lemonseed/vmlinuz-a"
cp "${WORKDIR}/artifacts/initrd.img" "${WORKDIR}/esp/EFI/lemonseed/initrd-a.img"
bash "${ROOT}/scripts/lemonseed-ab.sh" init --esp "${WORKDIR}/esp"
umount "${WORKDIR}/esp"

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
        "sha256": hashlib.sha256(body).hexdigest(),
    }
(root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
PY

export LEMONSEED_AB="${ROOT}/scripts/lemonseed-ab.sh"
# Pretend we are running on slot A.
export LEMONSEED_DOWNLOAD_DIR="${WORKDIR}/dl"
# lemonseed-update reads cmdline; inject via a wrapper env by patching detect:
# For smoke we call Python APIs directly with mocked current_slot.
python3 - "${WORKDIR}/artifacts-b" <<'PY'
import os, sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.environ["PYTHONPATH"])
import lemonseed_update as update

arts = Path(sys.argv[1])
with mock.patch.object(update, "current_slot", return_value="a"), mock.patch.object(
    update, "cmdline_slot", return_value="a"
):
    # Point AB helper at the repo script (no wrap).
    update.AB_HELPER = os.environ["LEMONSEED_AB"]
    # ensure_esp_mounted uses PARTLABEL=ESP which we linked.
    rc = update.cmd_apply(None, str(arts), reboot=False, force=False)
    assert rc == 0, rc
print("apply ok")
PY

# Verify B contents and grubenv.
B_HASH="$(python3 - "${WORKDIR}/artifacts-b/root.squashfs" "${ROOT_B}" <<'PY'
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

mount -t vfat -o rw,umask=0077 "${ESP}" "${WORKDIR}/esp"
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
umount "${WORKDIR}/esp"

# Checksum mismatch refusal.
python3 - "${WORKDIR}/artifacts-bad" <<'PY'
import hashlib, json, sys
from pathlib import Path
root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
(root / "vmlinuz").write_bytes(b"k")
(root / "initrd.img").write_bytes(b"i")
(root / "root.squashfs").write_bytes(b"bad-squash")
(root / "manifest.json").write_text(
    json.dumps(
        {
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
    ),
    encoding="utf-8",
)
PY

if python3 - "${WORKDIR}/artifacts-bad" <<'PY'
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

echo "OK: update smoke passed (slot B armed, cancel restores A default, bad checksum refused)"
