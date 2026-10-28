#!/usr/bin/env bash
# Generate an EC P-256 keypair for release signing (M8 P1 key ceremony, D11/D15;
# scheme per SIGNING.md).
#
# Default (no args) generates the local test keys (keys/test.priv, gitignored)
# used for milestone iteration — signing never requires the production key
# during development. The ceremony uses --name <name> for any additional
# keypair; existing keys are never overwritten.
#
# Note the production release key is NOT generated here: it is generated in a
# temporary directory on the signing machine during the key ceremony, imported
# into the signing hardware, and only its public half (keys/lemonseed-release.pub)
# is committed. See proposals/M8/SIGNING.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY_DIR="${ROOT}/keys"
NAME="test"
OUT_DIR="${KEY_DIR}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --name)
      NAME="${2:?--name requires a value}"
      shift
      ;;
    --out)
      OUT_DIR="${2:?--out requires a directory}"
      shift
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

openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
  -pkeyopt ec_param_enc:named_curve -out "${PRIV}"
openssl pkey -in "${PRIV}" -pubout -out "${PUB}"
chmod 0600 "${PRIV}"
echo "Wrote ${PRIV} and ${PUB}"
echo "Next: add {\"version\": N, \"algorithm\": \"ecdsa-p256\", \"file\": \"${NAME}.pub\"}" \
  "to ${OUT_DIR}/release-keys.json (see proposals/M8/SIGNING.md §2)."
