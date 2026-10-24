#!/usr/bin/env bash
# Publish main to the public repository without the private atlas data.
#
# The atlas data (everything under data/, the atlas, Eve, audit, website-check and wording
# reports, and the audit flag dumps) lives only in the private repository (origin,
# Fluffy-SHIBAINU/waive-data). This script clones main into a temporary folder, removes those
# paths from every commit, checks that none is left anywhere in the history, runs the tests on
# the result, and pushes it to the public repository. The rewrite is deterministic, so a later
# run gives the same commits for the same history and the push fast-forwards.
#
#   scripts/publish_public.sh            # push to https://github.com/Fluffy-SHIBAINU/waive
#   scripts/publish_public.sh --dry-run  # everything but the push
set -euo pipefail

PUBLIC_URL="${PUBLIC_URL:-https://github.com/Fluffy-SHIBAINU/waive.git}"
PRIVATE_PATHS=(
  data
  'docs/reports/atlas-*'
  'docs/reports/eve-atlas-*'
  'docs/reports/free-care-wording-*'
  'docs/reports/publication-audit*'
  'docs/reports/website-check-*'
  'docs/superpowers/plans/*/flags.json'
  'docs/superpowers/plans/*/audit-2-flags.json'
)
LEAK='^(data/|docs/reports/(atlas-|eve-atlas-|free-care-wording-|publication-audit|website-check-)|docs/superpowers/plans/[^/]+/(audit-2-)?flags\.json$)'

ROOT="$(git rev-parse --show-toplevel)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git clone --quiet --no-local --single-branch --branch main "$ROOT" "$WORK/waive"
cd "$WORK/waive"

quoted=""
for path in "${PRIVATE_PATHS[@]}"; do quoted+=" '$path'"; done
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch --prune-empty \
  --index-filter "git rm -r -q --cached --ignore-unmatch --$quoted" -- main >/dev/null

if git log --name-only --format= main | grep -E "$LEAK" | sort -u | head -5 | grep .; then
  echo "refusing to push: private paths are still in the history (listed above)" >&2
  exit 1
fi
echo "history clean: $(git rev-list --count main) commits, no private path in any of them"

uv run --frozen pytest -q -p no:cacheprovider -o addopts="" | tail -1

if [[ "${1:-}" == "--dry-run" ]]; then
  echo "dry run: not pushed"
  exit 0
fi
git push "$PUBLIC_URL" main:main
