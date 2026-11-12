#!/usr/bin/env bash
set -euo pipefail

# Deploy DiLoCo public page + private dashboard to a VPS that already runs
# jonasal/nginx-certbot (or compatible nginx) with an external docker network `proxy-net`.
#
# This script:
#   1) Syncs project files needed to build/run DiLoCo containers to the VPS
#   2) Installs nginx vhost configs into the nginx user_conf.d directory
#   3) Publishes web/public static files to the nginx web root
#   4) Ensures docker network `proxy-net` exists
#   5) Builds/updates DiLoCo containers via docker compose
#   6) Optionally reloads the nginx container
#
# Usage:
#   ./deploy/deploy_vps.sh user@vps.tls:/path/to/ddiloco
#
# Required env (or derived from arg):
#   DEPLOY_TARGET          user@host (or pass user@host:/path as first arg)
#   DEPLOY_PATH            Remote path for this repo/compose (e.g. /opt/ddiloco)
#
# Optional env:
#   NGINX_USER_CONF_DIR    Remote dir mounted to nginx /etc/nginx/user_conf.d
#   PUBLIC_WWW_DIR         Remote dir for public static files (default: /var/www/ddiloco.bsv.direct)
#   NGINX_CONTAINER        Nginx container name/id to reload (optional)
#   COMPOSE_FILE           Remote compose path relative to DEPLOY_PATH (default: deploy/docker-compose.diloco.yml)
#   DOCKER_NETWORK         External network name (default: proxy-net)
#   SSH_OPTS               Extra ssh options (default: empty)
#   RSYNC_OPTS             Extra rsync options (default: -az --delete)
#   DRY_RUN                Set to 1 for rsync dry-run
#   SKIP_NGINX             Set to 1 to skip nginx conf/static publish
#   SKIP_CONTAINERS        Set to 1 to skip docker compose up
#   SKIP_BUILD             Set to 1 to use existing images (no --build)
#
# Examples:
#   DEPLOY_TARGET=user@vps.tls DEPLOY_PATH=/opt/ddiloco \
#   NGINX_USER_CONF_DIR=/opt/nginx/user_conf.d PUBLIC_WWW_DIR=/var/www/ddiloco.bsv.direct \
#   NGINX_CONTAINER=nginx ./deploy/deploy_vps.sh
#
#   ./deploy/deploy_vps.sh user@vps.tls:/opt/ddiloco  # then set NGINX_* env vars as needed

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

# Parse target argument: user@host:/path
if [[ $# -ge 1 ]]; then
  ARG="$1"
  if [[ "$ARG" == *:* ]]; then
    DEPLOY_TARGET="${ARG%%:*}"
    DEPLOY_PATH="${ARG#*:}"
  else
    DEPLOY_TARGET="$ARG"
  fi
fi

: "${DEPLOY_TARGET:?Set DEPLOY_TARGET (user@host) or pass user@host:/path}"
: "${DEPLOY_PATH:?Set DEPLOY_PATH (remote path) or pass user@host:/path}"

NGINX_USER_CONF_DIR="${NGINX_USER_CONF_DIR:-}"
PUBLIC_WWW_DIR="${PUBLIC_WWW_DIR:-/var/www/ddiloco.bsv.direct}"
NGINX_CONTAINER="${NGINX_CONTAINER:-}"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/docker-compose.diloco.yml}"
DOCKER_NETWORK="${DOCKER_NETWORK:-proxy-net}"
SSH_OPTS="${SSH_OPTS:-}"
RSYNC_OPTS="${RSYNC_OPTS:--az --delete}"
DRY_RUN="${DRY_RUN:-0}"
SKIP_NGINX="${SKIP_NGINX:-0}"
SKIP_CONTAINERS="${SKIP_CONTAINERS:-0}"
SKIP_BUILD="${SKIP_BUILD:-0}"

SSH=(ssh ${SSH_OPTS} "$DEPLOY_TARGET")
RSYNC=(rsync ${SSH_OPTS:+-e "ssh ${SSH_OPTS}"} ${RSYNC_OPTS})
[[ "$DRY_RUN" == "1" ]] && RSYNC+=(--dry-run)

command -v ssh >/dev/null 2>&1 || die "ssh not found"
if ! command -v rsync >/dev/null 2>&1; then
  die "rsync not found (install rsync locally and on the VPS)"
fi

log "Target: ${DEPLOY_TARGET}:${DEPLOY_PATH}"
log "Public www: ${PUBLIC_WWW_DIR}"
[[ -n "$NGINX_USER_CONF_DIR" ]] && log "Nginx user_conf.d: ${NGINX_USER_CONF_DIR}" || log "Nginx user_conf.d: (not set, skipping conf copy)"
[[ -n "$NGINX_CONTAINER" ]] && log "Nginx container: ${NGINX_CONTAINER}" || log "Nginx container: (not set, skip reload)"

# 1) Create remote directories
log "Creating remote directories"
"${SSH[@]}" "mkdir -p '$DEPLOY_PATH' '$PUBLIC_WWW_DIR' $( [[ -n "$NGINX_USER_CONF_DIR" ]] && printf '%q' "$NGINX_USER_CONF_DIR" )"

# 2) Sync project files needed for build/runtime
log "Syncing project files to ${DEPLOY_TARGET}:${DEPLOY_PATH}"
"${RSYNC[@]}" \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'venv/' \
  --exclude '__pycache__/' \
  --exclude '**/__pycache__/' \
  --exclude '*.py[cod]' \
  --exclude '.pytest_cache/' \
  --exclude '*.egg-info/' \
  --exclude 'runs/' \
  --exclude 'tmp/' \
  --exclude '.DS_Store' \
  --exclude '*.log' \
  --exclude 'deploy/.env' \
  --exclude 'network/**/node_modules/' \
  "$ROOT_DIR/Dockerfile" \
  "$ROOT_DIR/.dockerignore" \
  "$ROOT_DIR/pyproject.toml" \
  "$ROOT_DIR/diloco" \
  "$ROOT_DIR/scripts" \
  "$ROOT_DIR/configs" \
  "$ROOT_DIR/web" \
  "$ROOT_DIR/network" \
  "$ROOT_DIR/docs" \
  "$ROOT_DIR/deploy" \
  "$ROOT_DIR/coordinator" \
  "$ROOT_DIR/worker" \
  "$ROOT_DIR/agent" \
  "$ROOT_DIR/pilot" \
  "$ROOT_DIR/README.md" \
  "$ROOT_DIR/LICENSE" \
  "$ROOT_DIR/STATUS.md" \
  "$ROOT_DIR/ROADMAP.md" \
  "$ROOT_DIR/CHANGELOG.md" \
  "$DEPLOY_TARGET:$DEPLOY_PATH/"

# Preserve remote .env if missing (do not overwrite secrets)
log "Ensuring deploy/.env exists on remote (not overwriting)"
"${SSH[@]}" "cd '$DEPLOY_PATH' && [ -f deploy/.env ] || cp deploy/.env.example deploy/.env"

# Ensure runs dir exists (volume mount target)
"${SSH[@]}" "mkdir -p '$DEPLOY_PATH/runs'"

# 3) Publish nginx configs and public static files
if [[ "$SKIP_NGINX" != "1" ]]; then
  if [[ -n "$NGINX_USER_CONF_DIR" ]]; then
    log "Publishing nginx configs to ${NGINX_USER_CONF_DIR}"
    "${RSYNC[@]}" "$ROOT_DIR/deploy/nginx/" "$DEPLOY_TARGET:$NGINX_USER_CONF_DIR/"
  else
    log "NGINX_USER_CONF_DIR not set; skipping nginx vhost config copy"
  fi

  log "Publishing public static files to ${PUBLIC_WWW_DIR}"
  "${RSYNC[@]}" "$ROOT_DIR/web/public/" "$DEPLOY_TARGET:$PUBLIC_WWW_DIR/"
else
  log "SKIP_NGINX=1; skipping nginx/static publish"
fi

# 4) Ensure external docker network exists
if [[ "$SKIP_CONTAINERS" != "1" ]]; then
  log "Ensuring docker network '${DOCKER_NETWORK}' exists on VPS"
  "${SSH[@]}" "docker network inspect '$DOCKER_NETWORK' >/dev/null 2>&1 || docker network create '$DOCKER_NETWORK'"

  # 5) Build and update containers
  log "Building/updating DiLoCo containers"
  BUILD_FLAG=""
  [[ "$SKIP_BUILD" != "1" ]] && BUILD_FLAG="--build"
  "${SSH[@]}" "cd '$DEPLOY_PATH' && docker compose -f '$COMPOSE_FILE' --env-file deploy/.env up -d ${BUILD_FLAG} --remove-orphans"

  log "Container status"
  "${SSH[@]}" "cd '$DEPLOY_PATH' && docker compose -f '$COMPOSE_FILE' ps"
else
  log "SKIP_CONTAINERS=1; skipping docker compose"
fi

# 6) Reload nginx (optional)
if [[ "$SKIP_NGINX" != "1" && -n "$NGINX_CONTAINER" ]]; then
  log "Testing and reloading nginx container '${NGINX_CONTAINER}'"
  "${SSH[@]}" "docker exec '$NGINX_CONTAINER' nginx -t && docker exec '$NGINX_CONTAINER' nginx -s reload" || {
    echo "warning: nginx reload failed; check container name/logs" >&2
  }
fi

log "Done."
echo
echo "Verify:"
echo "  curl -sS https://ddiloco.bsv.direct/api/status | jq ."
echo "  https://dash.ddiloco.bsv.direct"
echo
echo "Notes:"
echo "  - Review deploy/.env on the VPS and set a strong AGENT_TOKEN."
echo "  - Ensure DNS for ddiloco.bsv.direct and dash.ddiloco.bsv.direct points to the VPS."
echo "  - Ensure nginx-certbot can obtain certificates for both domains."
echo "  - Coordinator config for Docker: configs/docker-coordinator.json (host 0.0.0.0, port 8095)."
