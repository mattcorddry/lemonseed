#!/usr/bin/env bash
# Run all unit tests (no root, no hypervisor, no bare metal).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

"${ROOT}/scripts/check-untracked.sh"

echo "==> lemonseed-update / artifacts"
python3 "${ROOT}/scripts/test_lemonseed_update.py"

echo "==> release signature verifier"
python3 "${ROOT}/scripts/test_lemonseed_verify.py"

echo "==> installer"
# The installer imports the shared verifier from scripts/ (PYTHONPATH includes
# both dirs on the ISO too — see nixos/installer/modules/web.nix).
PYTHONPATH="${ROOT}/installer:${ROOT}/scripts" python3 -m unittest discover -s "${ROOT}/installer" -p 'test_*.py' -v

echo "==> release payload"
python3 "${ROOT}/scripts/test_release_payload.py"

echo "==> gen-repart"
python3 "${ROOT}/scripts/test_gen_repart.py"

echo "==> image pins"
python3 "${ROOT}/scripts/test_refresh_image_pins.py"

echo "==> control-plane"
PYTHONPATH="${ROOT}/control-plane" python3 -m unittest discover -s "${ROOT}/control-plane" -p 'test_*.py' -v

echo "==> all unit tests passed"
