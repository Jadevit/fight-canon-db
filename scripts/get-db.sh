#!/usr/bin/env bash
# Replace data/canon.db with a published release (the latest unless a tag is given).
# It downloads to a temporary folder first, so a failed download leaves the database alone.
# It refuses while an update is running (data/canon.db.tmp exists): that update would put its
# own copy over the download when it finishes.
#
#   scripts/get-db.sh                      # the latest release
#   scripts/get-db.sh db-2026-10-17-0532   # a given release
#   FORCE=1 scripts/get-db.sh              # even while an update is running
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f data/canon.db.tmp ] && [ "${FORCE:-}" != "1" ]; then
  echo "An update is running (data/canon.db.tmp exists); it will replace data/canon.db when it" >&2
  echo "finishes. Wait for it, or run with FORCE=1." >&2
  exit 1
fi

tag=${1:-$(gh release view --json tagName --jq .tagName)}
title=$(gh release view "$tag" --json name --jq .name)
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "Downloading $tag ($title)"
gh release download "$tag" --pattern canon.db --dir "$WORK"
mkdir -p data
mv -f "$WORK/canon.db" data/canon.db
echo "data/canon.db is now $tag"
