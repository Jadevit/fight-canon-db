#!/usr/bin/env bash
# Sofascore refuses GitHub's runners (HTTP 403), so its part of the daily update runs here,
# on a machine it serves (Windows Task Scheduler, through Git Bash). It takes the latest
# release, runs only the sofascore source on a copy in a temporary folder, and publishes a
# new release if that changed anything. The working copy's data/canon.db isn't touched;
# fetched files go to data/raw/sofascore/ as usual. summary.json in git catches up on the
# next CI update.
#
#   scripts/sofascore-update.sh            # update and publish
#   DRY_RUN=1 scripts/sofascore-update.sh  # update, but don't publish
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/Scripts/python
[ -x "$PY" ] || PY=.venv/bin/python
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

busy() {  # is a workflow that publishes releases running or about to?
  gh run list --limit 20 --json workflowName,status \
    --jq '[.[] | select((.workflowName == "Update database" or .workflowName == "Discover events")
                        and .status != "completed")] | length'
}

for try in 1 2 3; do
  while [ "$(busy)" != "0" ]; do
    echo "$(date '+%F %T') waiting for a running update to finish"
    sleep 120
  done
  base=$(gh release view --json tagName --jq .tagName)
  rm -f "$WORK"/*
  gh release download "$base" --pattern canon.db --dir "$WORK"
  echo "$(date '+%F %T') updating $base"
  "$PY" -m canon_db --db "$WORK/canon.db" --raw data/raw update sofascore
  if [ ! -f "$WORK/summary.json" ]; then  # written only when the database changed
    echo "$(date '+%F %T') no change"
    exit 0
  fi
  if [ "$(gh release view --json tagName --jq .tagName)" != "$base" ]; then
    echo "$(date '+%F %T') a newer release appeared while updating; starting over"
    continue
  fi
  last=$("$PY" -c "import json, sys; print(json.load(open(sys.argv[1]))['coverage']['last_event'])" "$WORK/summary.json")
  tag="db-$(date -u +%Y-%m-%d-%H%M)"
  if [ -n "${DRY_RUN:-}" ]; then
    echo "$(date '+%F %T') would publish $tag (Data through $last)"
    exit 0
  fi
  gh release create "$tag" "$WORK/canon.db" "$WORK/summary.json" --title "Data through $last" \
    --notes "Sofascore update, run outside GitHub (Sofascore refuses its runners)."
  echo "$(date '+%F %T') published $tag"
  gh workflow run pages.yml  # the site redeploys by itself only after the CI workflows
  exit 0
done
echo "$(date '+%F %T') gave up: releases kept changing" >&2
exit 1
