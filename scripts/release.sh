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
#
# Signing backend (scripts/sign.sh): SIGN_MODE=file (default) signs with the
# local test keys — milestone iteration never needs the production key. The
# signing environment signs with SIGN_MODE=pkcs11. Either way the self-verify
# below proves the set against exactly the key the manifest names.
#
# A staged set can also be verified on its own — e.g. after a signature was
# produced elsewhere and copied back as out/release-<version>/manifest.json.sig:
#   scripts/release.sh verify
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

VERSION="$("${ROOT}/scripts/version.sh")"
OUT_DIR="${ROOT}/out/release-${VERSION}"

if [[ "${1:-}" == "verify" ]]; then
  echo "==> Verifying the staged release ${OUT_DIR} (the appliance's own verifier)"
  LEMONSEED_RELEASE_KEYS_DIR="${ROOT}/keys" \
    python3 "${ROOT}/scripts/release_payload.py" verify --release-dir "${OUT_DIR}"
  exit 0
fi

echo "==> Building release candidate ${VERSION}"
# The build step does not sign: pack extends the manifest afterwards, so a
# signature here would be thrown away — with a token backend it would also
# cost a second authorization for nothing. The final sign below covers the
# whole manifest.
SIGN=0 VERSION="${VERSION}" make build-production
VERSION="${VERSION}" make build-installer

echo "==> Packing the sharded payload (+ size gate)"
rm -rf "${OUT_DIR}"
python3 "${ROOT}/scripts/release_payload.py" pack \
  --artifacts-dir "${ROOT}/out/appliance-${VERSION}" \
  --installer "${ROOT}/out/installer/lemonseed-installer.iso" \
  --out-dir "${OUT_DIR}"

echo "==> Signing the release manifest"
"${ROOT}/scripts/sign.sh" "${OUT_DIR}"

if [[ "${SIGN_MODE:-file}" == "offline" ]]; then
  # The signing block is finalized but the signature is produced out of band.
  # Nothing here is verifiable until manifest.json.sig lands in the staged
  # directory — `make release-verify` is the completion gate.
  echo "==> Release staged unsigned-for-signing: ${OUT_DIR}"
  echo "==> After the detached signature returns as ${OUT_DIR}/manifest.json.sig,"
  echo "    run: VERSION=${VERSION} make release-verify"
  exit 0
fi

# The self-verify runs against the trust set matching the key the manifest
# names: the production trust set for token signing, or the signing key's own
# public half for local test-key signing. Either way it is the appliance's
# verifier and the same fail-closed path.
TRUST_DIR=""
cleanup_trust() {
  if [[ -n "${TRUST_DIR}" ]]; then rm -rf "${TRUST_DIR}"; fi
}
trap cleanup_trust EXIT

if [[ "${SIGN_MODE:-file}" == "pkcs11" ]]; then
  TRUST_DIR="${ROOT}/keys"
else
  KEY="${SIGN_KEY:-${ROOT}/keys/test.priv}"
  PUB="${KEY%.priv}.pub"
  if [[ ! -f "${PUB}" ]]; then
    echo "No public key beside the signing key: ${PUB}" >&2
    exit 1
  fi
  TRUST_DIR="$(mktemp -d)"
  cp "${PUB}" "${TRUST_DIR}/$(basename "${PUB}")"
  printf '{"version": 1, "keys": [{"version": 1, "algorithm": "ecdsa-p256", "file": "%s"}]}\n' \
    "$(basename "${PUB}")" >"${TRUST_DIR}/release-keys.json"
fi

echo "==> Self-verifying the staged release (the appliance's own verifier)"
LEMONSEED_RELEASE_KEYS_DIR="${TRUST_DIR}" \
  python3 "${ROOT}/scripts/release_payload.py" verify --release-dir "${OUT_DIR}"

echo "==> Release staged: ${OUT_DIR}"
ls -lh "${OUT_DIR}"
