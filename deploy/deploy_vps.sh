#!/usr/bin/env bash
set -euo pipefail

# Deploy DiLoCo public page + private dashboard to a VPS that already runs
# jonasal/nginx-certbot (or compatible nginx) with an external docker network `proxy-net`.
#
# This script:
#   1) Syncs project files needed to build/run DiLoCo containers to the VPS
#   2) Publishes web/public static files to the nginx web root
#   3) Optionally installs nginx vhost config(s) into nginx user_conf.d
#   4) Ensures docker network `proxy-net` exists
#   5) Builds/updates DiLoCo containers via docker compose
#   6) Optionally reloads the nginx container
#
# Usage:
#   ./deploy/deploy_vps.sh [user@host:/path]
#
# Defaults (override via env):
#   DEPLOY_TARGET=space.bsv.direct
#   DEPLOY_PATH=/opt/didiloco
#   PUBLIC_WWW_DIR=/var/www/didiloco
#
# Optional env:
#   DEPLOY_TARGET          SSH target (user@host). Also parsed from arg user@host:/path
#   DEPLOY_PATH            Remote project path
#   NGINX_USER_CONF_DIR    Remote dir mounted to nginx /etc/nginx/user_conf.d
#   NGINX_CONF_FILES       Space-separated conf filenames from deploy/nginx/ to install
#                          (e.g. "didiloco.bsv.direct.conf"). Empty = do not install confs.
#   PUBLIC_WWW_DIR         Remote dir for public static files
#   NGINX_CONTAINER        Nginx container name/id to test/reload (optional)
#   COMPOSE_FILE           Remote compose path relative to DEPLOY_PATH (default: deploy/docker-compose.diloco.yml)
#   DOCKER_NETWORK         External network name (default: proxy-net)
#   SSH_OPTS               Extra ssh options
#   RSYNC_OPTS             Extra rsync options (default: -az --delete)
#   DRY_RUN                1 = rsync dry-run
#   SKIP_NGINX             1 = skip nginx conf/static publish
#   SKIP_CONTAINERS        1 = skip docker compose up
#   SKIP_BUILD             1 = docker compose up without --build
#
# Examples:
#   # Didiloco (space.bsv.direct) - publish app + public page, install specific vhost, update containers
#   NGINX_USER_CONF_DIR=/opt/nginx/user_conf.d \
#   NGINX_CONF_FILES="didiloco.bsv.direct.conf" \
#   NGINX_CONTAINER=nginx \
#   ./deploy/deploy_vps.sh space.bsv.direct:/opt/didiloco
#
#   # Only update containers (no nginx/static changes)
#   SKIP_NGINX=1 ./deploy/deploy_vps.sh space.bsv.direct:/opt/didiloco
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

DEPLOY_TARGET="${DEPLOY_TARGET:-space.bsv.direct}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/didiloco}"
NGINX_USER_CONF_DIR="${NGINX_USER_CONF_DIR:-}"
NGINX_CONF_FILES="${NGINX_CONF_FILES:-}"
PUBLIC_WWW_DIR="${PUBLIC_WWW_DIR:-/var/www/didiloco}"
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
REMOTE_MKDIR_DIRS=("$DEPLOY_PATH" "$PUBLIC_WWW_DIR")
if [[ -n "$NGINX_USER_CONF_DIR" && -n "$NGINX_CONF_FILES" ]]; then
  REMOTE_MKDIR_DIRS+=("$NGINX_USER_CONF_DIR")
fi
# shellcheck disable=SC2029
"${SSH[@]}" "mkdir -p $(printf '%q ' "${REMOTE_MKDIR_DIRS[@]}")"

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
  if [[ -n "$NGINX_USER_CONF_DIR" && -n "$NGINX_CONF_FILES" ]]; then
    log "Installing nginx config(s) to ${NGINX_USER_CONF_DIR}: ${NGINX_CONF_FILES}"
    for f in $NGINX_CONF_FILES; do
      src="$ROOT_DIR/deploy/nginx/$f"
      [[ -f "$src" ]] || die "nginx conf not found: $src"
      "${RSYNC[@]}" "$src" "$DEPLOY_TARGET:$NGINX_USER_CONF_DIR/"
    done
  elif [[ -n "$NGINX_USER_CONF_DIR" ]]; then
    log "NGINX_USER_CONF_DIR set but NGINX_CONF_FILES empty; skipping vhost install (configs available at $DEPLOY_PATH/deploy/nginx/ on remote)"
  else
    log "NGINX_USER_CONF_DIR not set; skipping nginx vhost config install"
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
echo "  curl -sS https://didiloco.bsv.direct/api/status | jq ."
echo "  https://didiloco.bsv.direct"
echo
echo "Notes:"
echo "  - Review $DEPLOY_PATH/deploy/.env on the VPS and set a strong AGENT_TOKEN."
echo "  - Ensure DNS for didiloco.bsv.direct points to the VPS."
echo "  - Nginx: install/adapt deploy/nginx/didiloco.bsv.direct.conf (set NGINX_USER_CONF_DIR and NGINX_CONF_FILES=\"didiloco.bsv.direct.conf\" to auto-install)."
echo "  - Verify ssl_certificate paths in your vhost match the cert issued for this domain."
echo "  - Coordinator config for Docker: configs/docker-coordinator.json (host 0.0.0.0, port 8095)."
