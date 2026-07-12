#!/usr/bin/env bash
# Launch docker-compose.dev.yml against a dev image tag.
#
# By default the most recent successful "Build and Push Dev Containers"
# workflow run's tag is resolved via dev/tag.sh. To pin a specific tag,
# set IMAGE_TAG in the environment and this script will skip resolution.
#
# Usage:
#   dev/up.sh                       # resolve latest tag, then up
#   dev/up.sh --detach              # forward flags to docker compose up
#   IMAGE_TAG=dev-abc1234 dev/up.sh # pin a specific tag
#   REPO=other/repo dev/up.sh
#
# Env overrides:
#   REPO       GitHub repo (default: macedot/reclip-telegram-bot)
#   IMAGE_TAG  Skip tag resolution and use this value directly
#   ENV_FILE   Path to .env (default: ./.env)
#
# Requirements:
#   - docker with compose v2
#   - gh CLI authenticated — required for either tag resolution or ghcr.io login

set -euo pipefail

REPO="${REPO:-macedot/reclip-telegram-bot}"
ENV_FILE="${ENV_FILE:-.env}"

# --- preflight --------------------------------------------------------------

command -v docker >/dev/null 2>&1 || { echo "docker not installed" >&2; exit 1; }

[ -f "$ENV_FILE" ] || { echo "Missing $ENV_FILE. Copy from .env.example and fill required vars." >&2; exit 1; }

# --- resolve tag ------------------------------------------------------------

if [ -z "${IMAGE_TAG:-}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  if [ ! -x "${SCRIPT_DIR}/tag.sh" ]; then
    echo "IMAGE_TAG is not set and ${SCRIPT_DIR}/tag.sh is not executable." >&2
    echo "Either set IMAGE_TAG=dev-xxxxxxx or chmod +x ${SCRIPT_DIR}/tag.sh" >&2
    exit 1
  fi
  echo "Resolving latest successful dev CI tag..." >&2
  IMAGE_TAG="$("${SCRIPT_DIR}/tag.sh")"
fi

# --- docker login to GHCR ---------------------------------------------------

command -v gh >/dev/null 2>&1 || { echo "gh CLI not installed (needed for ghcr.io login)" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh CLI not authenticated (run: gh auth login)" >&2; exit 1; }

LOGIN_USER=$(gh api user --jq .login)
gh auth token | docker login ghcr.io -u "$LOGIN_USER" --password-stdin >/dev/null \
  || { echo "Failed to docker login to ghcr.io" >&2; exit 1; }

# --- launch compose ---------------------------------------------------------

# All args are forwarded to docker compose up so the user can pass
# --detach, --build, --force-recreate, etc.
exec env IMAGE_TAG="$IMAGE_TAG" \
  docker compose --env-file "$ENV_FILE" -f docker-compose.dev.yml up "$@"