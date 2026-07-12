#!/usr/bin/env bash
# Resolve the most recent successful "Build and Push Dev Containers" tag.
# Prints the tag (e.g. dev-abc1234) to stdout; status info goes to stderr.
#
# Usage:
#   dev/tag.sh                 # default repo: macedot/reclip-telegram-bot
#   REPO=other/repo dev/tag.sh
#   TAG=$(dev/tag.sh)          # capture into a shell variable
#
# Env overrides:
#   REPO  GitHub repo to query (default: macedot/reclip-telegram-bot)
#
# Requirements:
#   - gh CLI authenticated (gh auth status)
#   - python3 (used inline for JSON parsing of the runs API)

set -euo pipefail

REPO="${REPO:-macedot/reclip-telegram-bot}"

# --- preflight --------------------------------------------------------------

command -v gh      >/dev/null 2>&1 || { echo "gh CLI not installed" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not installed" >&2; exit 1; }

gh auth status >/dev/null 2>&1 || { echo "gh CLI not authenticated (run: gh auth login)" >&2; exit 1; }

# --- resolve latest successful dev CI run -----------------------------------

SHA=$(gh api "repos/${REPO}/actions/runs?per_page=20" \
  | python3 -c "
import json, sys
runs = json.load(sys.stdin)['workflow_runs']
for r in runs:
    if r['name'] == 'Build and Push Dev Containers' and r['conclusion'] == 'success':
        print(r['head_sha'])
        break
else:
    sys.exit('No successful dev CI run found yet.')
")

# GHCR tag is dev-<7-char SHA prefix>, matching containers-dev.yml.
TAG="dev-${SHA:0:7}"
echo "Resolved ${REPO} @ ${TAG} (full SHA: ${SHA})" >&2
echo "${TAG}"