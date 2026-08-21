#!/usr/bin/env bash
# Reclaim Nix store space without slowing the next build.
#
# What this does:
#   - Drops stale `nix build --out-link` GC roots under out/.build/
#     (keeps roots for out/latest, out/latest-qemu, and --keep dirs)
#   - Runs `nix-collect-garbage` WITHOUT -d
#     (unreachable paths only; does not delete profile generations)
#
# What this does not do:
#   - `nix-collect-garbage -d` (would drop old nix profiles and re-download)
#   - Delete the current appliance/ISO closures (next build stays warm)
#
# Usage:
#   scripts/nix-gc.sh              # drop stale roots, then soft GC
#   scripts/nix-gc.sh --if-needed  # only if / or /nix is >= NIX_GC_THRESHOLD % (default 80)
#   scripts/nix-gc.sh --keep DIR   # extra out-link directory to retain
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
THRESHOLD="${NIX_GC_THRESHOLD:-80}"
IF_NEEDED=0
KEEP_DIRS=()

usage() {
  echo "usage: $0 [--if-needed] [--keep DIR]..." >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --if-needed) IF_NEEDED=1; shift ;;
    --keep)
      KEEP_DIRS+=("${2:-}")
      shift 2
      ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

export PATH="/nix/var/nix/profiles/default/bin:${PATH}"

used_pct() {
  local mount="$1"
  df -P "${mount}" | awk 'NR==2 { gsub("%","",$5); print $5 }'
}

df_line() {
  local mount="$1"
  df -h "${mount}" | awk -v m="${mount}" 'NR==2 { print m ": " $3 " used, " $4 " free (" $5 ")" }'
}

if [[ "${IF_NEEDED}" -eq 1 ]]; then
  used_root="$(used_pct /)"
  used_nix="$(used_pct /nix)"
  if [[ "${used_root}" -lt "${THRESHOLD}" && "${used_nix}" -lt "${THRESHOLD}" ]]; then
    echo "==> nix-gc: / at ${used_root}%, /nix at ${used_nix}% (threshold ${THRESHOLD}%); skip"
    exit 0
  fi
  echo "==> nix-gc: / at ${used_root}%, /nix at ${used_nix}% >= ${THRESHOLD}%; collecting"
fi

# Map out/latest → out/.build/<ver>-appliance, out/latest-qemu → …-appliance-qemu
keep_build_dir_from_link() {
  local link="$1"
  local target base ver
  [[ -L "${link}" ]] || return 0
  target="$(readlink "${link}")"
  base="$(basename "${target}")"
  case "${base}" in
    appliance-*-qemu)
      ver="${base#appliance-}"
      ver="${ver%-qemu}"
      KEEP_DIRS+=("${ROOT}/out/.build/${ver}-appliance-qemu")
      ;;
    appliance-*)
      ver="${base#appliance-}"
      KEEP_DIRS+=("${ROOT}/out/.build/${ver}-appliance")
      ;;
  esac
}

keep_build_dir_from_link "${ROOT}/out/latest"
keep_build_dir_from_link "${ROOT}/out/latest-qemu"

declare -A KEEP=()
for d in "${KEEP_DIRS[@]}"; do
  [[ -n "${d}" ]] || continue
  KEEP["$(readlink -f "${d}" 2>/dev/null || echo "${d}")"]=1
  KEEP["${d}"]=1
done

BUILD_ROOT="${ROOT}/out/.build"
if [[ -d "${BUILD_ROOT}" ]]; then
  shopt -s nullglob
  for d in "${BUILD_ROOT}"/*; do
    [[ -d "${d}" ]] || continue
    resolved="$(readlink -f "${d}" || echo "${d}")"
    if [[ -n "${KEEP["${d}"]:-}" || -n "${KEEP["${resolved}"]:-}" ]]; then
      echo "==> nix-gc: keep GC root ${d}"
      continue
    fi
    echo "==> nix-gc: drop stale GC root ${d}"
    rm -rf "${d}"
  done
  shopt -u nullglob
fi

# Do not delete out/installer/result here: it is the current ISO root.
# The next `nix build -o` replaces it; the previous ISO then becomes garbage.

if command -v nix-collect-garbage >/dev/null; then
  echo "==> nix-gc: nix-collect-garbage (no -d)"
  nix-collect-garbage
else
  echo "nix-collect-garbage not found; skipped store collect" >&2
fi

echo "==> nix-gc: $(df_line /); $(df_line /nix)"
