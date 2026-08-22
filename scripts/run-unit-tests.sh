#!/usr/bin/env bash
# Run all unit tests (no root, no hypervisor, no bare metal).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

echo "==> lemonseed-update / artifacts"
python3 "${ROOT}/scripts/test_lemonseed_update.py"

echo "==> gen-repart"
python3 "${ROOT}/scripts/test_gen_repart.py"

echo "==> control-plane"
PYTHONPATH="${ROOT}/control-plane" python3 -m unittest discover -s "${ROOT}/control-plane" -p 'test_*.py' -v

echo "==> all unit tests passed"
