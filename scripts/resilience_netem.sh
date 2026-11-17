#!/usr/bin/env bash
# P2.7 resilience runner with optional kernel-level link impairment (tc netem).
#
# Runs the in-process fault-injection suite (tests/test_resilience.py) and an
# end-to-end run through the WAN proxy (scripts/run_resilience_wan.py). If the
# host supports it and the caller passes --netem, latency/jitter/loss/bandwidth
# are applied to loopback with `tc netem` for the duration of the E2E run, then
# removed. Without root/tc, the proxy still provides data-plane impairment.
#
# Usage:
#   ./scripts/resilience_netem.sh                       # in-process suite + proxy E2E
#   ./scripts/resilience_netem.sh --netem               # + tc netem on lo
#   DELAY_MS=80 LOSS=0.05 ./scripts/resilience_netem.sh --netem
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${VENV_PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY="python3"

DELAY_MS="${DELAY_MS:-40}"
JITTER_MS="${JITTER_MS:-15}"
LOSS="${LOSS:-0.02}"
BANDWIDTH_KBPS="${BANDWIDTH_KBPS:-0}"
ROUNDS="${ROUNDS:-5}"
WORKERS="${WORKERS:-2}"
CHURN_AFTER_ROUND="${CHURN_AFTER_ROUND:-1}"

USE_NETEM=0
for arg in "$@"; do
  case "$arg" in
    --netem) USE_NETEM=1 ;;
    *) echo "unknown arg: $arg" >&2; exit 2 ;;
  esac
done

cleanup_netem() {
  if [ "$USE_NETEM" -eq 1 ] && [ -n "${TC_DEV:-}" ]; then
    echo "==> removing tc netem on ${TC_DEV}"
    tc qdisc del dev "$TC_DEV" root 2>/dev/null || true
  fi
}
trap cleanup_netem EXIT

if [ "$USE_NETEM" -eq 1 ]; then
  if ! command -v tc >/dev/null 2>&1; then
    echo "tc not found; skipping kernel impairment (proxy still active)" >&2
    USE_NETEM=0
  elif [ "$(id -u)" -ne 0 ]; then
    echo "not root; skipping kernel impairment (proxy still active)" >&2
    USE_NETEM=0
  else
    TC_DEV="${TC_DEV:-lo}"
    echo "==> applying tc netem on ${TC_DEV}: delay=${DELAY_MS}ms jitter=${JITTER_MS}ms loss=${LOSS}"
    tc qdisc del dev "$TC_DEV" root 2>/dev/null || true
    tc qdisc add dev "$TC_DEV" root netem \
      delay "${DELAY_MS}ms" "${JITTER_MS}ms" \
      loss "${LOSS}" \
      rate "${BANDWIDTH_KBPS}kbps" 2>/dev/null || {
        # Some kernels reject rate=0; retry without it.
        tc qdisc del dev "$TC_DEV" root 2>/dev/null || true
        tc qdisc add dev "$TC_DEV" root netem \
          delay "${DELAY_MS}ms" "${JITTER_MS}ms" loss "${LOSS}"
      }
  fi
fi

echo "==> in-process resilience suite"
"$PY" -m unittest tests.test_resilience -v

echo "==> end-to-end resilience run through WAN proxy"
"$PY" scripts/run_resilience_wan.py \
  --workers "$WORKERS" \
  --rounds "$ROUNDS" \
  --churn-after-round "$CHURN_AFTER_ROUND" \
  --delay-ms "$DELAY_MS" \
  --jitter-ms "$JITTER_MS" \
  --loss "$LOSS" \
  --bandwidth-kbps "$BANDWIDTH_KBPS"

echo "==> RESILIENCE OK"
