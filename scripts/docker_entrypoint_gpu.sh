#!/usr/bin/env bash
# GPU container entrypoint: one-command join via JOIN_CODE, or pass-through.
set -euo pipefail

cd "${APP_DIR:-/app}"

if [ -n "${JOIN_CODE:-}" ]; then
  args=(join "$JOIN_CODE"
        --node-id "${NODE_ID:-worker-gpu}"
        --shard-id "${SHARD_ID:-0}"
        --device "${DEVICE:-cuda}")
  if [ "${NO_TUNNEL:-0}" = "1" ] || [ "${NO_TUNNEL:-}" = "true" ]; then
    args+=(--no-tunnel)
  fi
  if [ -n "${STATE_CACHE_DIR:-}" ]; then
    args+=(--out-dir "$STATE_CACHE_DIR")
  fi
  exec python3 scripts/pilot.py "${args[@]}"
fi

exec "$@"
