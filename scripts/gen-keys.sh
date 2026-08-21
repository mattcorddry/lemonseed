#!/usr/bin/env bash
# Generate local ed25519 test keys for optional SIGN=1 builds.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_DIR="${ROOT}/keys"
PRIV="${KEY_DIR}/ed25519.priv"
PUB="${KEY_DIR}/ed25519.pub"

mkdir -p "${KEY_DIR}"

if [[ -f "${PRIV}" ]]; then
  echo "Key already exists: ${PRIV}" >&2
  exit 0
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required" >&2
  exit 1
fi

openssl genpkey -algorithm Ed25519 -out "${PRIV}"
openssl pkey -in "${PRIV}" -pubout -out "${PUB}"
chmod 0600 "${PRIV}"
echo "Wrote ${PRIV} and ${PUB}"
