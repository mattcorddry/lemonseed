#!/usr/bin/env bash
# Build the Lemonseed EFI/USB installer ISO into out/installer/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${ROOT}/out/installer"

export PATH="/nix/var/nix/profiles/default/bin:${PATH}"

"${ROOT}/scripts/check-untracked.sh"
"${ROOT}/scripts/nix-gc.sh" --if-needed

echo "==> building installer ISO (nix build .#installer-iso)"
mkdir -p "${OUT}"
nix build "${ROOT}#installer-iso" -o "${OUT}/result"

ISO="$(find -L "${OUT}/result" -name '*.iso' -print -quit)"
if [[ -z "${ISO}" ]]; then
  echo "No .iso found under ${OUT}/result" >&2
  ls -la "${OUT}/result" >&2 || true
  exit 1
fi

cp -f "${ISO}" "${OUT}/lemonseed-installer.iso"
ln -sfn installer "${ROOT}/out/latest-installer"
"${ROOT}/scripts/nix-gc.sh"
echo "==> ${OUT}/lemonseed-installer.iso"
echo "==> symlink out/latest-installer → installer/"
ls -lh "${OUT}/lemonseed-installer.iso"
