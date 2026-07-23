#!/usr/bin/env bash
# Show what Railway is actually serving, and where it sits relative to your local
# commit and your pushed commit. Comparing all three is what distinguishes
# "I haven't pushed yet" from "I pushed but the deploy hasn't landed" — the two
# look identical if you only compare local HEAD against the deployed commit.
#
# Requires the Railway CLI to be logged in and linked (`railway link`) — see
# sync-prod-db.md.
#
# Usage: ./check-deploy.sh [service]   (defaults to "web")

set -euo pipefail

# Railway statuses that mean a deploy is on its way but not yet serving.
_IN_FLIGHT="INITIALIZING QUEUED BUILDING DEPLOYING WAITING NEEDS_APPROVAL"
# ...and ones that mean it tried and didn't make it.
_BROKEN="FAILED CRASHED"

_contains() {  # _contains <word> <space-separated list>
  case " $2 " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}

# Decide the one-line verdict. Kept as a pure function of its arguments so every
# branch can be exercised without having to reproduce the state for real.
#
#   describe_status <local> <upstream> <upstream_name> <deployed> <latest_status> <latest_commit>
#
# Any of upstream/deployed/latest may be empty.
describe_status() {
  local local_head="$1" upstream="$2" upstream_name="$3"
  local deployed="$4" latest_status="$5" latest_commit="$6"
  local ls="${local_head:0:7}" us="${upstream:0:7}" ds="${deployed:0:7}"

  if [ -n "$upstream" ] && [ "$local_head" != "$upstream" ]; then
    if git merge-base --is-ancestor "$local_head" "$upstream" 2>/dev/null; then
      echo "local checkout is BEHIND $upstream_name ($ls < $us) — git pull"
    elif git merge-base --is-ancestor "$upstream" "$local_head" 2>/dev/null; then
      local n
      n=$(git rev-list --count "$upstream..$local_head" 2>/dev/null || echo "?")
      echo "NOT PUSHED — $n commit(s) ahead of $upstream_name ($ls vs $us)"
    else
      echo "local and $upstream_name have DIVERGED ($ls vs $us)"
    fi
    return
  fi

  if [ -z "$deployed" ]; then
    echo "no successful deployment found"
    return
  fi

  if [ "$local_head" = "$deployed" ]; then
    echo "up to date — local HEAD is pushed and deployed ($ls)"
    return
  fi

  # Pushed (or no upstream to compare), but Railway is serving something else.
  # The newest deployment record says whether that's still in progress.
  if [ -n "$latest_commit" ] && [ "$latest_commit" = "$local_head" ]; then
    if _contains "$latest_status" "$_IN_FLIGHT"; then
      echo "PUSHED, DEPLOY IN PROGRESS — $ls is $latest_status; still serving $ds"
      return
    fi
    if _contains "$latest_status" "$_BROKEN"; then
      echo "DEPLOY FAILED for $ls ($latest_status) — still serving $ds"
      return
    fi
  fi
  echo "pushed, but $ls has no deployment yet — still serving $ds"
}

# Sourcing with CHECK_DEPLOY_LIB=1 gets the functions without running anything,
# so the logic above can be tested directly.
[ -n "${CHECK_DEPLOY_LIB:-}" ] && return 0

service="${1:-web}"

deployments=$(railway deployment list -s "$service" --json --limit 20)

# What is actually serving: Railway marks superseded deployments REMOVED, so the
# newest SUCCESS is the live one.
live=$(echo "$deployments" | jq -r '
  map(select(.status == "SUCCESS")) | .[0] |
  if . == null then empty else
    "\(.createdAt)\t\(.meta.commitHash // "")\t\(.meta.commitMessage // "" | split("\n")[0])"
  end
')
# The newest record of any status — this is what reveals an in-flight or failed deploy.
latest=$(echo "$deployments" | jq -r '
  .[0] | if . == null then empty else "\(.status)\t\(.meta.commitHash // "")" end
')

created_at=""; commit_hash=""; subject=""
[ -n "$live" ] && IFS=$'\t' read -r created_at commit_hash subject <<< "$live"
latest_status=""; latest_commit=""
[ -n "$latest" ] && IFS=$'\t' read -r latest_status latest_commit <<< "$latest"

echo "Service:      $service"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  if [ -z "$commit_hash" ]; then
    echo "No successful deployment found for service '$service'." >&2
    exit 1
  fi
  echo "Deployed:     ${commit_hash:0:7}  $subject"
  echo "Deployed at:  $created_at"
  exit 0
fi

# origin/main can be stale, and a stale ref is exactly what makes this check lie.
git fetch --quiet origin 2>/dev/null || echo "  (warning: could not fetch — pushed state may be stale)" >&2

local_head=$(git rev-parse HEAD)
upstream_name=$(git rev-parse --abbrev-ref '@{u}' 2>/dev/null || echo "origin/main")
upstream=$(git rev-parse "$upstream_name" 2>/dev/null || echo "")

echo "Local HEAD:   ${local_head:0:7}  $(git log -1 --format=%s)"
if [ -n "$upstream" ]; then
  printf '%-14s%s\n' "$upstream_name:" "${upstream:0:7}"
else
  printf '%-14s%s\n' "$upstream_name:" "(not found — is the branch pushed?)"
fi
if [ -n "$commit_hash" ]; then
  echo "Deployed:     ${commit_hash:0:7}  $subject"
  echo "Deployed at:  $created_at"
else
  echo "Deployed:     (none)"
fi

echo "Status:       $(describe_status "$local_head" "$upstream" "$upstream_name" \
  "$commit_hash" "$latest_status" "$latest_commit")"
