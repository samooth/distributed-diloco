#!/usr/bin/env bash
set -euo pipefail

# Simple deploy: rsync project to VPS and run docker compose.
#
# Usage:
#   ./deploy/deploy.sh user@vps:/path/to
#   DEPLOY_TARGET=user@vps:/path/to ./deploy/deploy.sh
#
# Optional env:
#   COMPOSE_FILE   Remote compose file relative to deploy path (default: deploy/docker-compose.diloco.yml)
#   ENV_FILE       Remote env file relative to deploy path (default: deploy/.env)
#   DOCKER_NETWORK External docker network to ensure exists (default: proxy-net)
#   SSH_OPTS       Extra ssh/rsync options (e.g. "-p 2222 -i ~/.ssh/id_ed25519")
#   RSYNC_OPTS     Extra rsync options (default: -az --delete)
#   DEPLOY_BUILD   1 to pass --build (default), 0 to skip
#   DRY_RUN        1 for rsync dry-run (skips remote compose/env init)

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

TARGET="${1:-${DEPLOY_TARGET:-}}"
[[ -n "$TARGET" ]] || die "usage: $0 user@vps:/path/to (or set DEPLOY_TARGET)"

if [[ "$TARGET" == *:* ]]; then
  REMOTE_HOST="${TARGET%%:*}"
  REMOTE_PATH="${TARGET#*:}"
else
  die "target must be user@host:/remote/path"
fi
[[ -n "$REMOTE_HOST" && -n "$REMOTE_PATH" ]] || die "target must be user@host:/remote/path"

COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.diloco.yml}"
ENV_FILE="${ENV_FILE:-deploy/.env}"
DOCKER_NETWORK="${DOCKER_NETWORK:-proxy-net}"
SSH_OPTS="${SSH_OPTS:-}"
RSYNC_OPTS="${RSYNC_OPTS:--az --delete}"
DEPLOY_BUILD="${DEPLOY_BUILD:-1}"
DRY_RUN="${DRY_RUN:-0}"

command -v ssh >/dev/null 2>&1 || die "ssh not found"
command -v rsync >/dev/null 2>&1 || die "rsync not found"

if [[ -n "$SSH_OPTS" ]]; then
  RSYNC_RSH=(-e "ssh $SSH_OPTS")
  # shellcheck disable=SC2206
  SSH_CMD=(ssh $SSH_OPTS "$REMOTE_HOST")
else
  RSYNC_RSH=()
  SSH_CMD=(ssh "$REMOTE_HOST")
fi

RSYNC=(rsync $RSYNC_OPTS "${RSYNC_RSH[@]}")
[[ "$DRY_RUN" == "1" ]] && RSYNC+=(--dry-run)

log "deploy ${REMOTE_HOST}:${REMOTE_PATH}"
[[ "$DRY_RUN" == "1" ]] && log "DRY_RUN=1"

log "ensure remote directories"
# shellcheck disable=SC2029
"${SSH_CMD[@]}" "mkdir -p '$REMOTE_PATH' '$REMOTE_PATH/runs'"

log "rsync project"
"${RSYNC[@]}" \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'venv/' \
  --exclude 'node_modules/' \
  --exclude '__pycache__/' \
  --exclude '*.py[cod]' \
  --exclude '*$py.class' \
  --exclude '.pytest_cache/' \
  --exclude '.mypy_cache/' \
  --exclude '*.egg-info/' \
  --exclude 'runs/' \
  --exclude 'tmp/' \
  --exclude '.tmp/' \
  --exclude 'dist/' \
  --exclude 'build/' \
  --exclude '.DS_Store' \
  --exclude '*.log' \
  --exclude '.env' \
  --exclude 'deploy/.env' \
  --exclude '*.wif' \
  --exclude '*.key' \
  --exclude '*.pem' \
  --exclude 'data/' \
  ./ "${REMOTE_HOST}:${REMOTE_PATH}/"

if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY_RUN: skipping remote env init and docker compose"
  exit 0
fi

log "ensure ${ENV_FILE} exists on remote (not overwriting)"
# shellcheck disable=SC2029
"${SSH_CMD[@]}" "cd '$REMOTE_PATH' && [ -f '$ENV_FILE' ] || cp deploy/.env.example '$ENV_FILE'"

log "ensure docker network '${DOCKER_NETWORK}'"
# shellcheck disable=SC2029
"${SSH_CMD[@]}" "docker network inspect '$DOCKER_NETWORK' >/dev/null 2>&1 || docker network create '$DOCKER_NETWORK'"

BUILD_FLAG=""
if [[ "$DEPLOY_BUILD" == "1" ]]; then
  BUILD_FLAG="--build"
fi

log "docker compose up (${COMPOSE_FILE})"
# shellcheck disable=SC2029
"${SSH_CMD[@]}" "cd '$REMOTE_PATH' && docker compose -f '$COMPOSE_FILE' --env-file '$ENV_FILE' up -d ${BUILD_FLAG} --remove-orphans"

log "status"
# shellcheck disable=SC2029
"${SSH_CMD[@]}" "cd '$REMOTE_PATH' && docker compose -f '$COMPOSE_FILE' ps"

log "done"
