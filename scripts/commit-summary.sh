#!/usr/bin/env bash
# Commit data/summary.json on top of the newest main and push it, retrying if main moves in
# the meantime: a CI run can take hours, and other runs (and people) push while it does.
#
#   scripts/commit-summary.sh "data: update through <last event>"
set -euo pipefail
cd "$(dirname "$0")/.."

msg=$1
new=$(mktemp)
cp data/summary.json "$new"
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

for try in 1 2 3 4 5; do
  git fetch -q origin main
  git reset -q --hard origin/main  # tracked files only; the database and raw pages are ignored
  cp "$new" data/summary.json
  git add data/summary.json
  if git diff --cached --quiet; then
    echo "summary.json is already up to date on main"
    exit 0
  fi
  git commit -q -m "$msg"
  if git push -q origin HEAD:main; then
    echo "pushed summary.json (try $try)"
    exit 0
  fi
  sleep $((try * 5))
done
echo "couldn't push summary.json" >&2
exit 1
