#!/usr/bin/env bash
set -euo pipefail

# Deploy DiLoCo to a VPS via rsync + docker compose over SSH.
# Pattern: sync project root with excludes (robust to missing optional files), like
# /jardin/MVPs/AnchorChainBaaS/scripts/deploy.sh.
#
# This script:
#   1) Syncs the project to the VPS (rsync ./ with excludes)
#   2) Ensures deploy/.env exists remotely (never overwrites secrets)
#   3) Publishes web/public static files to the nginx web root
#   4) Optionally installs nginx vhost config(s) into nginx user_conf.d
#   5) Ensures external docker network `proxy-net` exists
#   6) Builds/updates DiLoCo containers via docker compose
#   7) Optionally tests/reloads the nginx container
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
#   PUBLIC_WWW_DIR         Remote dir for public static files (default: /var/www/didiloco)
#   NGINX_USER_CONF_DIR    Remote dir mounted to nginx /etc/nginx/user_conf.d (host path)
#   NGINX_CONF_FILES       Space-separated conf filenames from deploy/nginx/ to install
#                          (e.g. "didiloco.bsv.direct.conf"). Empty = do not install confs.
#   NGINX_CONTAINER        Nginx container name/id to test/reload (optional)
#   COMPOSE_FILE           Remote compose path relative to DEPLOY_PATH (default: deploy/docker-compose.diloco.yml)
#   DOCKER_NETWORK         External docker network name (default: proxy-net)
#   SSH_OPTS               Extra ssh/rsync options (e.g. "-p 2222 -i ~/.ssh/id_ed25519")
#   RSYNC_OPTS             Extra rsync options (default: -az --delete)
#   DRY_RUN                1 = rsync dry-run
#   SKIP_NGINX             1 = skip nginx conf/static publish and nginx reload
#   SKIP_CONTAINERS        1 = skip docker compose up
#   SKIP_BUILD             1 = docker compose up without --build
#
# Examples:
#   # space.bsv.direct defaults: sync project + publish web + update containers (no nginx conf/reload)
#   NGINX_CONF_FILES="" NGINX_CONTAINER="" ./deploy/deploy_vps.sh
#
#   # Install/refresh nginx vhost and reload nginx too
#   NGINX_USER_CONF_DIR=/opt/nginx/user_conf.d \
#   NGINX_CONF_FILES="didiloco.bsv.direct.conf" \
#   NGINX_CONTAINER=nginx \
#   ./deploy/deploy_vps.sh
#
#   # Only update containers (no nginx/static changes)
#   SKIP_NGINX=1 ./deploy/deploy_vps.sh
#
#   # Only sync files/static (no containers)
#   SKIP_CONTAINERS=1 NGINX_CONF_FILES="" NGINX_CONTAINER="" ./deploy/deploy_vps.sh

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

# Parse target argument: user@host or user@host:/path
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

[[ -n "$DEPLOY_TARGET" ]] || die "DEPLOY_TARGET is empty (user@host)"
[[ -n "$DEPLOY_PATH" ]] || die "DEPLOY_PATH is empty"

command -v ssh >/dev/null 2>&1 || die "ssh not found"
command -v rsync >/dev/null 2>&1 || die "rsync not found (install rsync locally and on the VPS)"

if [[ -n "$SSH_OPTS" ]]; then
  RSYNC_RSH=(-e "ssh $SSH_OPTS")
  # shellcheck disable=SC2206
  SSH_CMD=(ssh $SSH_OPTS "$DEPLOY_TARGET")
else
  RSYNC_RSH=()
  SSH_CMD=(ssh "$DEPLOY_TARGET")
fi

RSYNC_BASE=(rsync $RSYNC_OPTS "${RSYNC_RSH[@]}")
[[ "$DRY_RUN" == "1" ]] && RSYNC_BASE+=(--dry-run)

log "Target: ${DEPLOY_TARGET}:${DEPLOY_PATH}"
log "Public www: ${PUBLIC_WWW_DIR}"
[[ -n "$NGINX_USER_CONF_DIR" ]] && log "Nginx user_conf.d: ${NGINX_USER_CONF_DIR}" || log "Nginx user_conf.d: (not set, skipping conf install)"
[[ -n "$NGINX_CONF_FILES" ]] && log "Nginx conf files: ${NGINX_CONF_FILES}" || log "Nginx conf files: (none, skipping conf install)"
[[ -n "$NGINX_CONTAINER" ]] && log "Nginx container: ${NGINX_CONTAINER}" || log "Nginx container: (not set, skip reload)"
[[ "$DRY_RUN" == "1" ]] && log "DRY_RUN=1 (rsync dry-run)"

# 1) Create remote directories
log "Creating remote directories"
REMOTE_MKDIR_DIRS=("$DEPLOY_PATH" "$PUBLIC_WWW_DIR" "$DEPLOY_PATH/runs")
if [[ -n "$NGINX_USER_CONF_DIR" && -n "$NGINX_CONF_FILES" ]]; then
  REMOTE_MKDIR_DIRS+=("$NGINX_USER_CONF_DIR")
fi
# shellcheck disable=SC2029
"${SSH_CMD[@]}" "mkdir -p $(printf '%q ' "${REMOTE_MKDIR_DIRS[@]}")"

# 2) Sync project files needed for build/runtime (entire repo with excludes; robust if optional files are missing)
log "Syncing project files to ${DEPLOY_TARGET}:${DEPLOY_PATH}"
"${RSYNC_BASE[@]}" \
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
  ./ "${DEPLOY_TARGET}:${DEPLOY_PATH}/"

# Preserve remote .env if missing (do not overwrite secrets)
if [[ "$DRY_RUN" != "1" ]]; then
  log "Ensuring deploy/.env exists on remote (not overwriting)"
  "${SSH_CMD[@]}" "cd '$DEPLOY_PATH' && [ -f deploy/.env ] || cp deploy/.env.example deploy/.env"
  "${SSH_CMD[@]}" "mkdir -p '$DEPLOY_PATH/runs'"
else
  log "DRY_RUN: skipping remote deploy/.env initialization"
fi

# 3) Publish nginx configs and public static files
if [[ "$SKIP_NGINX" != "1" ]]; then
  if [[ -n "$NGINX_USER_CONF_DIR" && -n "$NGINX_CONF_FILES" ]]; then
    log "Installing nginx config(s) to ${NGINX_USER_CONF_DIR}: ${NGINX_CONF_FILES}"
    for f in $NGINX_CONF_FILES; do
      src="./deploy/nginx/$f"
      [[ -f "$src" ]] || die "nginx conf not found: $src"
      "${RSYNC_BASE[@]}" "$src" "${DEPLOY_TARGET}:${NGINX_USER_CONF_DIR}/"
    done
  elif [[ -n "$NGINX_USER_CONF_DIR" || -n "$NGINX_CONF_FILES" ]]; then
    log "NGINX_USER_CONF_DIR and NGINX_CONF_FILES must both be set to install vhosts; skipping conf install"
  else
    log "No nginx vhost install requested (configs available on remote at ${DEPLOY_PATH}/deploy/nginx/)"
  fi

  log "Publishing public static files to ${PUBLIC_WWW_DIR}"
  "${RSYNC_BASE[@]}" ./web/public/ "${DEPLOY_TARGET}:${PUBLIC_WWW_DIR}/"
else
  log "SKIP_NGINX=1; skipping nginx/static publish"
fi

# 4) Ensure external docker network exists
if [[ "$SKIP_CONTAINERS" != "1" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: would ensure docker network '${DOCKER_NETWORK}' and run docker compose (${COMPOSE_FILE})"
  else
    log "Ensuring docker network '${DOCKER_NETWORK}' exists on VPS"
    "${SSH_CMD[@]}" "docker network inspect '$DOCKER_NETWORK' >/dev/null 2>&1 || docker network create '$DOCKER_NETWORK'"

    # 5) Build and update containers
    log "Building/updating DiLoCo containers"
    BUILD_FLAG=""
    [[ "$SKIP_BUILD" != "1" ]] && BUILD_FLAG="--build"
    # shellcheck disable=SC2029
    "${SSH_CMD[@]}" "cd '$DEPLOY_PATH' && docker compose -f '$COMPOSE_FILE' --env-file deploy/.env up -d ${BUILD_FLAG} --remove-orphans"

    log "Container status"
    # shellcheck disable=SC2029
    "${SSH_CMD[@]}" "cd '$DEPLOY_PATH' && docker compose -f '$COMPOSE_FILE' ps"
  fi
else
  log "SKIP_CONTAINERS=1; skipping docker compose"
fi

# 6) Reload nginx (optional)
if [[ "$SKIP_NGINX" != "1" && -n "$NGINX_CONTAINER" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    log "DRY_RUN: would test/reload nginx container '${NGINX_CONTAINER}'"
  else
    log "Testing and reloading nginx container '${NGINX_CONTAINER}'"
    # shellcheck disable=SC2029
    "${SSH_CMD[@]}" "docker exec '$NGINX_CONTAINER' nginx -t && docker exec '$NGINX_CONTAINER' nginx -s reload" || {
      echo "warning: nginx reload failed; check container name/logs" >&2
    }
  fi
fi

log "Done."
echo
echo "Verify:"
echo "  curl -sS https://didiloco.bsv.direct/api/status | jq ."
echo "  https://didiloco.bsv.direct"
echo
echo "Notes:"
echo "  - Review ${DEPLOY_PATH}/deploy/.env on the VPS and set a strong AGENT_TOKEN."
echo "  - Ensure DNS for didiloco.bsv.direct points to the VPS."
echo "  - Nginx: install/adapt deploy/nginx/didiloco.bsv.direct.conf (set NGINX_USER_CONF_DIR and NGINX_CONF_FILES=\"didiloco.bsv.direct.conf\" to auto-install)."
echo "  - Verify ssl_certificate paths in your vhost match the cert issued for this domain."
echo "  - Coordinator config for Docker: configs/docker-coordinator.json (host 0.0.0.0, port 8095)."
