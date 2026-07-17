#!/usr/bin/env bash
# Show the timestamp and commit of the latest successful Railway deployment,
# and whether it matches the local git HEAD. Requires the Railway CLI to be
# logged in and linked (`railway link`) — see sync-prod-db.md.
#
# Usage: ./check-deploy.sh [service]   (defaults to "web")

set -euo pipefail

service="${1:-web}"

deploy=$(railway deployment list -s "$service" --json --limit 20 | jq -r '
  map(select(.status == "SUCCESS")) | .[0] |
  if . == null then empty else
    "\(.createdAt)\t\(.meta.commitHash)\t\(.meta.commitMessage | split("\n")[0])"
  end
')

if [ -z "$deploy" ]; then
  echo "No successful deployment found for service '$service'." >&2
  exit 1
fi

IFS=$'\t' read -r created_at commit_hash subject <<< "$deploy"
short_hash="${commit_hash:0:7}"

echo "Service:      $service"
echo "Deployed at:  $created_at"
echo "Commit:       $short_hash  $subject"

if git rev-parse --git-dir >/dev/null 2>&1; then
  local_head=$(git rev-parse HEAD)
  if [ "$local_head" = "$commit_hash" ]; then
    echo "Status:       up to date with local HEAD ($short_hash)"
  else
    local_short="${local_head:0:7}"
    echo "Status:       local HEAD is $local_short — differs from deployed $short_hash"
  fi
fi
