#!/usr/bin/env bash
# Detached signature over manifest.json (M8 P1, D12; scheme per SIGNING.md).
#
# The manifest is the single root of trust: every artifact/part hash lives
# inside the signed bytes, so only the manifest is ever signed — one root, not
# two. The interface never changes: <dir> in, manifest.json.sig out, exit 0 =
# signed. SIGN_MODE selects the backend:
#
#   file    a PEM private key file (default; local/test signing)
#   pkcs11  a PKCS#11 token via the openssl pkcs11 provider — the key never
#           exists as a file, and every signature requires the token's own
#           authorization (PIN and/or physical presence, per token policy).
#   offline prepare a release set for an external signing environment: the
#           signing block is finalized but NO signature is written. The
#           detached signature produced elsewhere is dropped into the staged
#           directory as manifest.json.sig, and `make release-verify` proves
#           the completed set.
#
# The interface is identical across modes, so a token can replace the file key
# without touching the pipeline. Unknown modes error out; there is no silent
# fallback from one backend to another.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:?usage: sign.sh <artifact-dir>}"
MANIFEST="${OUT_DIR}/manifest.json"
SIG="${MANIFEST}.sig"
MODE="${SIGN_MODE:-file}"

case "${MODE}" in
  file)
    # Local/test signing: a PEM private key on disk. The production trust set
    # lists the public key a release must name, so a test-key signature is
    # self-consistent for local iteration but refused by appliances — by
    # design.
    KEY="${SIGN_KEY:-${ROOT}/keys/test.priv}"
    PUB_NAME="${SIGN_PUB_NAME:-$(basename "${KEY%.priv}").pub}"
    ;;
  pkcs11)
    # Token signing. The key URI is deliberately serial-free; override with
    # SIGN_KEY_URI when a different token/key should sign. Authorization is
    # whatever the token's slot policy demands (typically a PIN prompt, shown
    # by openssl as a pass-phrase prompt, plus physical presence); it cannot
    # be bypassed here. SIGN_PIN_FILE (0600, root-only) optionally supplies
    # the PIN for non-interactive runs.
    KEY="${SIGN_KEY_URI:-pkcs11:id=%04;type=private}"
    PUB_NAME="${SIGN_PUB_NAME:-lemonseed-release.pub}"
    ;;
  offline)
    # External signing environment: finalize the signing block naming the
    # production key, write no signature, and let the signature arrive out of
    # band. The recorded key name is the one the trust set bakes.
    KEY=""
    PUB_NAME="${SIGN_PUB_NAME:-lemonseed-release.pub}"
    ;;
  *)
    echo "Unknown SIGN_MODE '${MODE}' (expected: file, offline, or pkcs11)" >&2
    exit 1
    ;;
esac

if [[ ! -f "${MANIFEST}" ]]; then
  echo "No manifest to sign: ${MANIFEST}" >&2
  exit 1
fi

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required for signing" >&2
  exit 1
fi

# Provider configuration for the token backend, built per-invocation and handed
# to openssl via OPENSSL_CONF (never edits the global openssl.cnf). Module
# paths are discovered, not hardcoded: distros disagree about lib dirs.
PKCS11_CONF=""
if [[ "${MODE}" == "pkcs11" ]]; then
  provider_so="$(ls /usr/lib/*/ossl-modules/pkcs11.so 2>/dev/null | head -n1 || true)"
  module_so="$(ls /usr/lib/*/libykcs11.so* 2>/dev/null | head -n1 || true)"
  if [[ -z "${provider_so}" || -z "${module_so}" ]]; then
    echo "pkcs11 backend: could not locate the openssl pkcs11 provider and its" >&2
    echo "PKCS#11 module. Install the pkcs11-provider and ykcs11 packages (or" >&2
    echo "set them up for your token) and retry." >&2
    exit 1
  fi
  PKCS11_CONF="$(mktemp)"
  trap 'rm -f "${PKCS11_CONF}"' EXIT
  {
    echo "openssl_conf = openssl_init"
    echo "[openssl_init]"
    echo "providers = provider_sect"
    echo "[provider_sect]"
    echo "default = default_sect"
    echo "pkcs11 = pkcs11_sect"
    echo "[default_sect]"
    echo "activate = 1"
    echo "[pkcs11_sect]"
    echo "module = ${provider_so}"
    echo "pkcs11-module-path = ${module_so}"
    echo "activate = 1"
    if [[ -n "${SIGN_PIN_FILE:-}" && -r "${SIGN_PIN_FILE}" ]]; then
      echo "pkcs11-module-token-pin = file:${SIGN_PIN_FILE}"
    fi
  } >"${PKCS11_CONF}"
  chmod 0600 "${PKCS11_CONF}"
fi

if [[ "${MODE}" == "file" ]]; then
  if [[ ! -f "${KEY}" ]]; then
    echo "Signing key not found at ${KEY}." >&2
    echo "Generate local test keys with 'make keys', or point SIGN_KEY at an" >&2
    echo "existing key. Production releases are signed with SIGN_MODE=pkcs11." >&2
    exit 1
  fi
fi

# Record the signing metadata inside the signed bytes, then sign exactly what
# ships: any edit after signing breaks verification by design.
if [[ -f "${SIG}" ]]; then
  rm -f "${SIG}"
fi
if command -v jq >/dev/null 2>&1; then
  tmp="$(mktemp)"
  jq --arg pub "${PUB_NAME}" \
    '.signing.enabled = true | .signing.scheme = "detached-ecdsa-p256" | .signing.public_key = $pub' \
    "${MANIFEST}" >"${tmp}"
  # mktemp creates 0600 and the manifest must stay HTTP-servable.
  chmod 0644 "${tmp}"
  mv "${tmp}" "${MANIFEST}"
else
  echo "jq not found: signing metadata not updated (manifest keeps its current signing block)" >&2
fi

sign_one() {
  if [[ "${MODE}" == "pkcs11" ]]; then
    OPENSSL_CONF="${PKCS11_CONF}" openssl pkeyutl -sign -rawin \
      -provider pkcs11 -provider default \
      -inkey "${KEY}" \
      -in "${MANIFEST}" -out "${SIG}"
  else
    openssl pkeyutl -sign -rawin -inkey "${KEY}" -in "${MANIFEST}" -out "${SIG}"
  fi
}

if [[ "${MODE}" == "offline" ]]; then
  echo "==> Signing block finalized on ${MANIFEST} (mode=offline; no signature written)"
  echo "==> Hand this manifest.json to the signing environment; when the detached"
  echo "    signature comes back as ${SIG}, run: make release-verify"
  exit 0
fi

sign_one

if [[ ! -s "${SIG}" ]]; then
  echo "Error: signature file is empty — signing did not complete." >&2
  exit 1
fi

echo "==> Signed ${MANIFEST} (detached-ecdsa-p256, mode=${MODE})"
echo "==> Wrote ${SIG}"
