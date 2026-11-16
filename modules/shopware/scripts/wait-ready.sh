#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a || true
URL="${1:-${SHOPWARE_APP_URL:-http://localhost:${SHOPWARE_PORT:-8088}}}"
TIMEOUT="${TIMEOUT:-300}"
echo "[wait-ready] waiting for $URL (timeout ${TIMEOUT}s)"
end=$((SECONDS+TIMEOUT))
while (( SECONDS < end )); do
  if curl -fsS -o /dev/null "$URL" 2>/dev/null || curl -fsS -o /dev/null "$URL/admin" 2>/dev/null; then
    echo "[wait-ready] ready: $URL"; exit 0
  fi
  sleep 5
done
echo "[wait-ready] timeout" >&2; exit 1
