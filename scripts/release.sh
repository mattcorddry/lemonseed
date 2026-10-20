#!/usr/bin/env bash
# M8 P2: build → package → sign → stage the complete, upload-ready release
# asset set under out/release-<version>/ (PACKAGING.md §3). Publishing to
# GitHub releases is P4's job; this target is the single place the asset set
# is defined. Releases are always signed — an unsigned release set is not a
# release set.
#
# Usage:
#   make release                       # version = date + local build counter
#   VERSION=20261020.01 make release   # official release version
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

VERSION="$("${ROOT}/scripts/version.sh")"
OUT_DIR="${ROOT}/out/release-${VERSION}"

echo "==> Building release candidate ${VERSION}"
SIGN=1 VERSION="${VERSION}" make build-production
VERSION="${VERSION}" make build-installer

echo "==> Packing the sharded payload (+ size gate)"
rm -rf "${OUT_DIR}"
python3 "${ROOT}/scripts/release_payload.py" pack \
  --artifacts-dir "${ROOT}/out/appliance-${VERSION}" \
  --installer "${ROOT}/out/installer/lemonseed-installer.iso" \
  --out-dir "${OUT_DIR}"

echo "==> Signing the release manifest"
"${ROOT}/scripts/sign.sh" "${OUT_DIR}"

echo "==> Self-verifying the staged release (the appliance's own verifier)"
LEMONSEED_RELEASE_KEYS_DIR="${ROOT}/keys" \
  python3 "${ROOT}/scripts/release_payload.py" verify --release-dir "${OUT_DIR}"

echo "==> Release staged: ${OUT_DIR}"
ls -lh "${OUT_DIR}"
