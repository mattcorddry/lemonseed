#!/usr/bin/env bash
# Build NixOS appliance artifacts into out/appliance-<version>[ -qemu]/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if ! command -v nix >/dev/null 2>&1; then
  # Multi-user Nix install default location (not a project path).
  if [[ -x /nix/var/nix/profiles/default/bin/nix ]]; then
    export PATH="/nix/var/nix/profiles/default/bin:${PATH}"
  else
    echo "nix not found on PATH; source the Nix profile or install Nix" >&2
    exit 1
  fi
fi

VERSION="$("${ROOT}/scripts/version.sh")"
PROFILE="${PROFILE:-appliance}" # appliance | appliance-qemu

"${ROOT}/scripts/nix-gc.sh" --if-needed

case "${PROFILE}" in
  appliance)
    PACKAGE=".#artifacts"
    OUT_DIR="${ROOT}/out/appliance-${VERSION}"
    LATEST_LINK="latest"
    ;;
  appliance-qemu)
    PACKAGE=".#artifacts-qemu"
    OUT_DIR="${ROOT}/out/appliance-${VERSION}-qemu"
    LATEST_LINK="latest-qemu"
    ;;
  *)
    echo "Unknown PROFILE=${PROFILE} (expected appliance|appliance-qemu)" >&2
    exit 1
    ;;
esac

BUILD_DIR="${ROOT}/out/.build/${VERSION}-${PROFILE}"
mkdir -p "${BUILD_DIR}" "${OUT_DIR}"

echo "==> Building appliance ${VERSION} (profile=${PROFILE})"

APPLIANCE_VERSION="${VERSION}" nix build \
  --impure \
  --out-link "${BUILD_DIR}/artifacts" \
  "${PACKAGE}"

install -m 0644 "$(readlink -f "${BUILD_DIR}/artifacts/vmlinuz")" "${OUT_DIR}/vmlinuz"
install -m 0644 "$(readlink -f "${BUILD_DIR}/artifacts/root.squashfs")" "${OUT_DIR}/root.squashfs"
install -m 0644 "$(readlink -f "${BUILD_DIR}/artifacts/initrd.img")" "${OUT_DIR}/initrd.img"

sha256_vmlinuz="$(sha256sum "${OUT_DIR}/vmlinuz" | awk '{print $1}')"
sha256_squashfs="$(sha256sum "${OUT_DIR}/root.squashfs" | awk '{print $1}')"
sha256_initrd="$(sha256sum "${OUT_DIR}/initrd.img" | awk '{print $1}')"

cat >"${OUT_DIR}/manifest.json" <<EOF
{
  "name": "lemonseed",
  "version": "${VERSION}",
  "arch": "x86_64",
  "profile": "${PROFILE}",
  "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "artifacts": {
    "vmlinuz": {
      "path": "vmlinuz",
      "sha256": "${sha256_vmlinuz}"
    },
    "root.squashfs": {
      "path": "root.squashfs",
      "sha256": "${sha256_squashfs}"
    },
    "initrd.img": {
      "path": "initrd.img",
      "sha256": "${sha256_initrd}",
      "note": "Included for practical boot/QEMU; soft goal is to drop when no-initrd spike succeeds."
    }
  },
  "signing": {
    "scheme": "detached-ed25519",
    "enabled": false
  }
}
EOF

if [[ "${SIGN:-0}" == "1" ]]; then
  "${ROOT}/scripts/sign.sh" "${OUT_DIR}"
else
  echo "==> Signing disabled (set SIGN=1 to enable local/test keys)"
fi

ln -sfn "$(basename "${OUT_DIR}")" "${ROOT}/out/${LATEST_LINK}"

"${ROOT}/scripts/nix-gc.sh" --keep "${BUILD_DIR}"

echo "==> Artifacts ready: ${OUT_DIR}"
ls -lh "${OUT_DIR}"
