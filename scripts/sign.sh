#!/usr/bin/env bash
# Detached ed25519 signatures over artifact files + manifest.
# Requires keys/ed25519.priv (openssl/pkey or minisign-compatible raw).
# For M1 we use `openssl` if available, else `nix-shell`-free ssh-keygen signed via `ssh-keygen -Y`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:?usage: sign.sh <artifact-dir>}"
KEY="${SIGN_KEY:-${ROOT}/keys/ed25519.priv}"
PUB="${SIGN_PUB:-${ROOT}/keys/ed25519.pub}"

if [[ ! -f "${KEY}" ]]; then
  echo "Signing key not found at ${KEY}" >&2
  echo "Generate local test keys with: make keys" >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required for SIGN=1" >&2
  exit 1
fi

sign_one() {
  local file="$1"
  openssl pkeyutl -sign -inkey "${KEY}" -rawin -in "${file}" -out "${file}.sig"
}

for name in vmlinuz root.squashfs initrd.img manifest.json; do
  path="${OUT_DIR}/${name}"
  if [[ -f "${path}" ]]; then
    echo "==> Signing ${name}"
    sign_one "${path}"
  fi
done

# Update manifest signing metadata (best-effort rewrite).
if command -v jq >/dev/null 2>&1; then
  tmp="$(mktemp)"
  jq --arg pub "$(basename "${PUB}")" \
    '.signing.enabled = true | .signing.public_key = $pub | .signing.scheme = "detached-ed25519-openssl-pkeyutl"' \
    "${OUT_DIR}/manifest.json" >"${tmp}"
  mv "${tmp}" "${OUT_DIR}/manifest.json"
  # Re-sign manifest after metadata update.
  sign_one "${OUT_DIR}/manifest.json"
fi

echo "==> Detached signatures written alongside artifacts"
