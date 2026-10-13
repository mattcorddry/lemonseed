#!/usr/bin/env bash
# Run all unit tests (no root, no hypervisor, no bare metal).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

"${ROOT}/scripts/check-untracked.sh"

echo "==> lemonseed-update / artifacts"
python3 "${ROOT}/scripts/test_lemonseed_update.py"

echo "==> installer"
PYTHONPATH="${ROOT}/installer" python3 -m unittest discover -s "${ROOT}/installer" -p 'test_*.py' -v

echo "==> gen-repart"
python3 "${ROOT}/scripts/test_gen_repart.py"

echo "==> image pins"
python3 "${ROOT}/scripts/test_refresh_image_pins.py"

echo "==> control-plane"
PYTHONPATH="${ROOT}/control-plane" python3 -m unittest discover -s "${ROOT}/control-plane" -p 'test_*.py' -v

echo "==> all unit tests passed"
