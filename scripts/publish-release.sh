#!/usr/bin/env bash
# Publish data/canon.db and data/summary.json as a new release, then commit summary.json. An
# update takes a while: if a newer release appeared since it started, publishing would take that
# release's rows out of the latest one, so this fails instead and the next run starts from it.
#
#   scripts/publish-release.sh <the release the update started from> "<release notes>"
set -euo pipefail
cd "$(dirname "$0")/.."

base=$1
notes=$2

latest=$(gh release view --json tagName --jq .tagName)
if [ "$latest" != "$base" ]; then
  echo "::error::$latest was published while this update ran from $base; not publishing over it."
  exit 1
fi

last=$(python -c "import json; print(json.load(open('data/summary.json'))['coverage']['last_event'])")
tag="db-$(date -u +%Y-%m-%d-%H%M)"
gh release create "$tag" data/canon.db data/summary.json --title "Data through ${last}" --notes "$notes"
exec scripts/commit-summary.sh "data: update through ${last}"
