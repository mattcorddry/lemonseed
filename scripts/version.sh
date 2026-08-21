#!/usr/bin/env bash
# Resolve appliance version: VERSION env override, else YYYYMMDD.NN from local counter.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COUNTER_FILE="${ROOT}/.build-counter"

if [[ -n "${VERSION:-}" ]]; then
  printf '%s\n' "${VERSION}"
  exit 0
fi

today="$(date -u +%Y%m%d)"

if [[ -f "${COUNTER_FILE}" ]]; then
  # shellcheck disable=SC2034
  IFS=. read -r last_date last_nn <"${COUNTER_FILE}" || true
else
  last_date=""
  last_nn="0"
fi

if [[ "${last_date}" == "${today}" ]]; then
  # Force decimal; strip leading zeros.
  nn=$((10#${last_nn} + 1))
else
  nn=1
fi

printf '%s.%02d\n' "${today}" "${nn}" >"${COUNTER_FILE}"
printf '%s.%02d\n' "${today}" "${nn}"
