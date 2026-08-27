#!/usr/bin/env bash
# Fail if the working tree has untracked files that are not .gitignore'd.
# Nix flakes only see tracked paths; untracked sources are silently omitted
# from builds (e.g. a new installer/*.py import that never lands in the ISO).
#
# Bypass (rare): ALLOW_UNTRACKED=1

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

if [[ "${ALLOW_UNTRACKED:-0}" == "1" ]]; then
  echo "==> check-untracked: skipped (ALLOW_UNTRACKED=1)"
  exit 0
fi

if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "==> check-untracked: not a git work tree; skipped"
  exit 0
fi

mapfile -t untracked < <(git ls-files --others --exclude-standard)
if ((${#untracked[@]} == 0)); then
  echo "==> check-untracked: ok (no untracked files)"
  exit 0
fi

echo "error: untracked files present — Nix flakes will omit them from builds:" >&2
printf '  %s\n' "${untracked[@]}" >&2
echo >&2
echo "Stage them with: git add <paths>" >&2
echo "Or bypass once with: ALLOW_UNTRACKED=1" >&2
exit 1
