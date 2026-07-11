#!/usr/bin/env bash
# Pull the most recent successful "Build and Push Dev Containers" run's
# tag and launch docker-compose.dev.yml against it.
#
# Usage:
#   ./bin/dev-up.sh                # default: macedot/reclip-telegram-bot, ./.env
#   ./bin/dev-up.sh --detach       # forward flags to docker compose up
#   REPO=other/repo ./bin/dev-up.sh
#
# Env overrides:
#   REPO       GitHub repo (default: macedot/reclip-telegram-bot)
#   ENV_FILE   Path to .env (default: ./.env)
#
# Requirements:
#   - gh CLI authenticated (gh auth status)
#   - docker with compose v2
#   - python3 (used inline for JSON parsing of the runs API)

set -euo pipefail

REPO="${REPO:-macedot/reclip-telegram-bot}"
ENV_FILE="${ENV_FILE:-.env}"

# --- preflight ---------------------------------------------------------------

command -v gh     >/dev/null 2>&1 || { echo "gh CLI not installed" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "docker not installed" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not installed" >&2; exit 1; }

gh auth status >/dev/null 2>&1 || { echo "gh CLI not authenticated (run: gh auth login)" >&2; exit 1; }

[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE. Copy from .env.example and fill required vars." >&2; exit 1; }

# --- resolve latest successful dev CI run -----------------------------------

echo "Resolving latest successful dev CI run for ${REPO}..." >&2

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
echo "Using dev image tag: ${TAG}  (full SHA: ${SHA})" >&2

# --- docker login to GHCR ---------------------------------------------------

LOGIN_USER=$(gh api user --jq .login)
gh auth token | docker login ghcr.io -u "$LOGIN_USER" --password-stdin >/dev/null \
  || { echo "Failed to docker login to ghcr.io" >&2; exit 1; }

# --- launch compose ---------------------------------------------------------

# All args are forwarded to docker compose up so the user can pass
# --detach, --build, --force-recreate, etc.
exec env IMAGE_TAG="$TAG" \
  docker compose --env-file "$ENV_FILE" -f docker-compose.dev.yml up "$@"
