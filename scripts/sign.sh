#!/usr/bin/env bash
# Detached ed25519 signature over manifest.json (M8 P1, D12).
#
# The manifest is the single root of trust: every artifact/part hash lives
# inside the signed bytes, so only the manifest is ever signed — one root, not
# two. The interface never changes: <dir> in, manifest.json.sig out, exit 0 =
# signed. SIGN_MODE selects the backend so a hardware signer (PKCS#11 /
# tap-to-sign, D15) can replace the file key without touching the pipeline.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:?usage: sign.sh <artifact-dir>}"
MANIFEST="${OUT_DIR}/manifest.json"
SIG="${MANIFEST}.sig"
MODE="${SIGN_MODE:-file}"

case "${MODE}" in
  file)
    # The ceremony key by default; the QEMU smoke overrides via SIGN_KEY. The
    # public key name recorded inside the signed metadata must be a key the
    # baked trust set actually carries, so it derives from the key's own name
    # (release-v1.priv -> release-v1.pub) unless SIGN_PUB_NAME says otherwise.
    KEY="${SIGN_KEY:-${ROOT}/keys/release-v1.priv}"
    PUB_NAME="${SIGN_PUB_NAME:-$(basename "${KEY%.priv}").pub}"
    ;;
  pkcs11)
    echo "SIGN_MODE=pkcs11 is not wired yet (M8 P4 wires the build box); " \
      "the interface is reserved" >&2
    exit 1
    ;;
  *)
    echo "Unknown SIGN_MODE '${MODE}' (expected: file)" >&2
    exit 1
    ;;
esac

if [[ ! -f "${MANIFEST}" ]]; then
  echo "No manifest to sign: ${MANIFEST}" >&2
  exit 1
fi

if [[ "${MODE}" == "file" ]]; then
  if [[ ! -f "${KEY}" ]]; then
    echo "Signing key not found at ${KEY}" >&2
    echo "Run the key ceremony on the signing box (SIGNING.md §3), or point" >&2
    echo "SIGN_KEY at an existing key." >&2
    exit 1
  fi
  if ! command -v openssl >/dev/null 2>&1; then
    echo "openssl is required for signing" >&2
    exit 1
  fi
fi

sign_one() {
  openssl pkeyutl -sign -inkey "${KEY}" -rawin -in "${MANIFEST}" -out "${SIG}"
}

# Record the signing metadata inside the signed bytes, then sign exactly what
# ships: any edit after signing breaks verification by design.
if [[ -f "${SIG}" ]]; then
  rm -f "${SIG}"
fi
if command -v jq >/dev/null 2>&1; then
  tmp="$(mktemp)"
  jq --arg pub "${PUB_NAME}" \
    '.signing.enabled = true | .signing.scheme = "detached-ed25519" | .signing.public_key = $pub' \
    "${MANIFEST}" >"${tmp}"
  # mktemp creates 0600 and the manifest must stay HTTP-servable.
  chmod 0644 "${tmp}"
  mv "${tmp}" "${MANIFEST}"
else
  echo "jq not found: signing metadata not updated (manifest keeps its current signing block)" >&2
fi

sign_one
echo "==> Signed ${MANIFEST} (detached ed25519, mode=${MODE})"
echo "==> Wrote ${SIG}"
