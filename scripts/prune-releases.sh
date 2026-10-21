#!/usr/bin/env bash
# Delete old database releases (and their tags), keeping:
#   - the newest $KEEP (default 30),
#   - the first release of each month (a monthly snapshot, kept for good),
#   - every tag listed in data/keep-releases.txt (releases other repos pin).
# Only tags named db-YYYY-MM-DD-HHMM are touched. DRY_RUN=1 lists what would go.
set -euo pipefail
cd "$(dirname "$0")/.."

KEEP=${KEEP:-30}
pinned=$(grep -oE '^db-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}' data/keep-releases.txt 2>/dev/null || true)

# Every release tag, oldest first (the tag's name is its UTC time, so it sorts).
tags=$(gh release list --limit 10000 --json tagName -q '.[].tagName' \
  | grep -E '^db-[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{4}$' | sort)
newest=$(echo "$tags" | tail -n "$KEEP")
monthly=$(echo "$tags" | awk '{ m = substr($0, 4, 7); if (!(m in seen)) { seen[m] = 1; print } }')

gone=0
for tag in $tags; do
  if grep -qxF "$tag" <<<"$newest"$'\n'"$monthly"$'\n'"$pinned"; then
    continue
  fi
  gone=$((gone + 1))
  if [ -n "${DRY_RUN:-}" ]; then
    echo "would delete $tag"
  else
    gh release delete "$tag" --cleanup-tag --yes
    echo "deleted $tag"
  fi
done
echo "$(echo "$tags" | grep -c .) releases; $gone ${DRY_RUN:+would be }deleted"
