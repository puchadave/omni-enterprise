#!/usr/bin/env bash
set -euo pipefail
# Runs bootstrap-integration.sh repeatedly until it succeeds (for init-container use).
# Exits 0 on success, keeps retrying on recoverable failures (e.g. shopware/middleware not ready).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTERVAL="${BOOTSTRAP_RETRY_INTERVAL:-15}"
MAX_ATTEMPTS="${BOOTSTRAP_MAX_ATTEMPTS:-0}" # 0 = infinite
ATTEMPT=0
echo "[shopware-bootstrap-loop] starting (interval=${INTERVAL}s, max=${MAX_ATTEMPTS})"
while :; do
  ATTEMPT=$((ATTEMPT+1))
  echo "[shopware-bootstrap-loop] attempt $ATTEMPT"
  if "$SCRIPT_DIR/bootstrap-integration.sh" --auto "$@"; then
    echo "[shopware-bootstrap-loop] success"
    exit 0
  fi
  if [[ "$MAX_ATTEMPTS" -gt 0 && "$ATTEMPT" -ge "$MAX_ATTEMPTS" ]]; then
    echo "[shopware-bootstrap-loop] giving up after $ATTEMPT attempts" >&2
    exit 1
  fi
  sleep "$INTERVAL"
done
