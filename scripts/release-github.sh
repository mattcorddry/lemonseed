#!/usr/bin/env bash
# M8 P4/D6: publish a staged release to GitHub Releases. The release channel
# is the GitHub prerelease flag (D5/D7): beta = prerelease, stable = not. The
# manifest only describes the payload — flipping a beta to stable is an edit
# of the release, never a re-sign.
#
# Usage:
#   make release-github-beta
#   make release-github-stable
#   VERSION=20261102.02 make release-github-beta    # publish an already-staged set
#
# VERSION unset: runs `make release` first (SIGN/SIGN_MODE pass through), so
# the lemonbuild flow is pull → build → sign → publish. VERSION set and the
# matching out/release-<version>/ exists: the staged set is verified and
# published as-is (the offline signing flow: stage → sign elsewhere → verify
# → publish).
#
# Release notes are generated from the commits since the previous GitHub
# release: a one-sentence summary plus a CHANGELOG-style list, written to
# out/release-<version>.notes.md and uploaded as the release body. Pass
# RELEASE_NOTES_FILE=<path> to publish hand-written notes instead.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

CHANNEL="${1:-beta}"
case "${CHANNEL}" in
  beta) PRERELEASE=(--prerelease) ;;
  stable) PRERELEASE=() ;;
  *) echo "channel must be 'beta' or 'stable' (got: ${CHANNEL})" >&2; exit 1 ;;
esac

if ! command -v gh >/dev/null 2>&1; then
  echo "gh is not installed — needed to create the GitHub release" >&2
  exit 1
fi
if ! gh auth status >/dev/null 2>&1; then
  echo "gh is not authenticated — run 'gh auth login' on the build host" >&2
  exit 1
fi

# The release tag points at HEAD, so the commit must exist on the remote and
# the tree must be clean — otherwise the tag and the shipped bytes disagree.
# ALLOW_DIRTY=1 bypasses both for local dry runs (mirrors ALLOW_UNTRACKED).
if [[ -n "$(git status --porcelain)" ]] && [[ "${ALLOW_DIRTY:-0}" != "1" ]]; then
  echo "The working tree is dirty — commit (or stash) before publishing," >&2
  echo "so the tag matches the shipped bytes." >&2
  exit 1
fi
if [[ "$(git log origin/main..HEAD --oneline 2>/dev/null | wc -l)" -gt 0 ]] && [[ "${ALLOW_DIRTY:-0}" != "1" ]]; then
  echo "HEAD has commits not pushed to origin/main — push first, then publish," >&2
  echo "so the release tag can be created on the remote." >&2
  exit 1
fi

if [[ -z "${VERSION:-}" ]]; then
  VERSION="$("${ROOT}/scripts/version.sh")"
fi
OUT_DIR="${ROOT}/out/release-${VERSION}"

if [[ ! -d "${OUT_DIR}" ]]; then
  echo "==> No staged release ${OUT_DIR}; running make release (SIGN/SIGN_MODE pass through)"
  VERSION="${VERSION}" make release
fi

echo "==> Verifying the staged release ${OUT_DIR} (fail-closed, the appliance's own verifier)"
VERSION="${VERSION}" make release-verify

TAG="v${VERSION}"
if gh release view "${TAG}" >/dev/null 2>&1; then
  echo "Release ${TAG} already exists on GitHub — a version's bytes are immutable;" >&2
  echo "pick a new version." >&2
  exit 1
fi

# --- Release notes from the commits since the previous release ---------------
PREV=""
PREV_JSON="$(gh release list --limit 20 --json tagName,isDraft 2>/dev/null || echo '[]')"
if command -v python3 >/dev/null 2>&1; then
  PREV="$(printf '%s' "${PREV_JSON}" | python3 -c '
import json, sys
rows = json.load(sys.stdin)
for row in rows:
    if not row.get("isDraft"):
        print(row["tagName"]); break
' 2>/dev/null || true)"
fi

if [[ -n "${PREV}" ]]; then
  # The previous tag may exist only on the remote (the signing box publishes
  # it); fetch tags so git log can walk from it.
  git fetch --tags --quiet 2>/dev/null || true
  SUBJECTS_RAW="$(git log --no-merges --format='%s' "${PREV}..HEAD" 2>/dev/null || true)"
else
  SUBJECTS_RAW="$(git log --no-merges --format='%s' -25 2>/dev/null || true)"
fi
[[ -z "${SUBJECTS_RAW//[[:space:]]/}" ]] && SUBJECTS_RAW=""
mapfile -t SUBJECTS < <(printf '%s\n' "${SUBJECTS_RAW}")

# Drop merges (already filtered), duplicates, and pure bookkeeping lines.
mapfile -t SUBJECTS < <(printf '%s\n' "${SUBJECTS[@]}" | awk '$0 !~ /^Merge / { if (!seen[$0]++) print }')

classify() {
  local s
  s="$(echo "$1" | tr '[:upper:]' '[:lower:]')"
  case "${s}" in
    *fix*|*repair*|*refus*|*regression*|*stale*|*race*|*crash*|*broken*) echo "Fixes" ;;
    *doc*|*readme*|*manual*|*backlog*|*typo*|*comment*) echo "Documentation" ;;
    *test*|*qemu*|*smoke*|*lint*|*coverage*) echo "Testing" ;;
    *build*|*packag*|*pin*|*make*|*nix*|*version*|*bump*|*sign*) echo "Build & packaging" ;;
    *) echo "Features & improvements" ;;
  esac
}

declare -A GROUPED=()
ORDER=()
n=0
for subject in "${SUBJECTS[@]}"; do
  [[ -z "${subject}" ]] && continue
  bucket="$(classify "${subject}")"
  if [[ -z "${GROUPED[${bucket}]+x}" ]]; then GROUPED["${bucket}"]=""; ORDER+=("${bucket}"); fi
  GROUPED["${bucket}"]+="- ${subject}"$'\n'
  n=$((n + 1))
done

if [[ -n "${PREV}" ]]; then
  if [[ "${n}" -eq 0 ]]; then
    SUMMARY="Lemonseed ${VERSION} collects no code changes since ${PREV}"
  else
    SUMMARY="Lemonseed ${VERSION} collects ${n} changes since ${PREV}"
  fi
else
  if [[ "${n}" -eq 0 ]]; then
    SUMMARY="Lemonseed ${VERSION} is the first GitHub release (no code changes)"
  else
    SUMMARY="Lemonseed ${VERSION} is the first GitHub release, collecting ${n} changes"
  fi
fi

NOTES_FILE="${RELEASE_NOTES_FILE:-${ROOT}/out/release-${VERSION}.notes.md}"
{
  echo "# Lemonseed ${VERSION}"
  echo
  echo "${SUMMARY}."
  if [[ -n "${RELEASE_NOTES_EXTRA:-}" ]]; then
    echo
    echo "${RELEASE_NOTES_EXTRA}"
  fi
  for bucket in "${ORDER[@]}"; do
    echo
    echo "## ${bucket}"
    echo "${GROUPED[${bucket}]}"
  done
} >"${NOTES_FILE}"

echo "==> Generated release notes: ${NOTES_FILE}"
cat "${NOTES_FILE}"

# --- Publish ------------------------------------------------------------------
ASSETS=()
for f in "${OUT_DIR}"/*; do
  [[ -f "${f}" ]] && ASSETS+=("${f}")
done

echo "==> Creating GitHub release ${TAG} (channel: ${CHANNEL}, ${#ASSETS[@]} assets)"
gh release create "${TAG}" "${PRERELEASE[@]}" \
  --target "$(git branch --show-current)" \
  --title "${VERSION}" \
  --notes-file "${NOTES_FILE}" \
  "${ASSETS[@]}"

echo "==> Published: https://github.com/$(git remote get-url origin | sed -E 's#.*[:/]([^/]+)/([^/.]+)(\.git)?$#\1/\2#')/releases/tag/${TAG}"
