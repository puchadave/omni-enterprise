#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
COMPOSE=(docker compose)
PROFILE="${1:-core,commerce}"
IFS=',' read -r -a profiles <<< "$PROFILE"
profile_args=()
for p in "${profiles[@]}"; do [[ -n "$p" ]] && profile_args+=(--profile "$p"); done

ok(){ printf '[ok] %s\n' "$*"; }
bad(){ printf '[fail] %s\n' "$*"; FAIL=1; }
FAIL=0

source .env 2>/dev/null || true

${COMPOSE[@]} "${profile_args[@]}" ps || bad "compose ps failed"

check_url(){
  local url="$1" name="$2"
  if curl -fsS "$url" >/dev/null 2>&1; then ok "$name $url"; else bad "$name $url"; fi
}

check_url "http://127.0.0.1:${OMNIROUTE_PORT:-20128}/health" "omniroute"
check_url "http://127.0.0.1:${MIDDLEWARE_PORT:-8002}/healthz" "middleware"
check_url "http://127.0.0.1:${MIDDLEWARE_PORT:-8002}/api/health" "middleware-api" || true

if ${COMPOSE[@]} ps -q shopware >/dev/null 2>&1 && [ -n "$(${COMPOSE[@]} ps -q shopware)" ]; then
  check_url "http://127.0.0.1:${SHOPWARE_PORT:-8088}/" "shopware" || true
fi

# DB basic check
if ${COMPOSE[@]} ps -q db >/dev/null 2>&1 && [ -n "$(${COMPOSE[@]} ps -q db)" ]; then
  if ${COMPOSE[@]} exec -T db sh -lc 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; then
    ok "postgres ready"
  else
    bad "postgres not ready"
  fi
fi

if [ "$FAIL" -eq 0 ]; then
  printf 'HEALTHCHECK_OK\n'
  exit 0
fi
printf 'HEALTHCHECK_FAILED\n'
exit 1
