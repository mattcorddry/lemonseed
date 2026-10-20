#!/usr/bin/env bash
# Generate an ed25519 keypair for release signing (M8 P1 key ceremony, D11/D15).
#
# Default (no args) keeps the legacy local test keys. The ceremony on the
# signing box uses --name release-v1: keys/release-v1.priv never leaves the
# box and is gitignored; keys/release-v1.pub is committed and baked into the
# image trust set via keys/release-keys.json. Existing keys are never
# overwritten.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_DIR="${ROOT}/keys"
NAME="ed25519"
OUT_DIR="${KEY_DIR}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      NAME="${2:?--name requires a value}"
      shift 2
      ;;
    --out)
      OUT_DIR="${2:?--out requires a directory}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1 (usage: gen-keys.sh [--name NAME] [--out DIR])" >&2
      exit 2
      ;;
  esac
done

PRIV="${OUT_DIR}/${NAME}.priv"
PUB="${OUT_DIR}/${NAME}.pub"

mkdir -p "${OUT_DIR}"

if [[ -e "${PRIV}" || -e "${PUB}" ]]; then
  echo "Key already exists: ${PRIV} (or ${PUB})" >&2
  echo "Refusing to overwrite — key material is never regenerated in place." >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required" >&2
  exit 1
fi

openssl genpkey -algorithm Ed25519 -out "${PRIV}"
openssl pkey -in "${PRIV}" -pubout -out "${PUB}"
chmod 0600 "${PRIV}"
echo "Wrote ${PRIV} and ${PUB}"
if [[ "${NAME}" != ed25519 ]]; then
  echo "Next: add {\"version\": N, \"algorithm\": \"ed25519\", \"file\": \"${NAME}.pub\"}" \
    "to ${OUT_DIR}/release-keys.json (see proposals/M8/SIGNING.md §2)."
fi
