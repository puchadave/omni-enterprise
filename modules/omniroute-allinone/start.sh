#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
log(){ echo "[omniroute-allinone] $*"; }
cmd="${1:-up}"; shift || true
case "$cmd" in
  up)
    [[ -f .env ]] || { cp .env.example .env; log "created .env from .env.example (all keys/configs preconfigured)"; }
    docker compose up -d --build "$@"
    log "started. endpoint: http://localhost:${OMNIROUTE_PORT:-20128}/v1"
    log "primary key: ${OMNIROUTE_API_KEY:-sk-814c9467fd9c9314-a41c41-72e2d3b0}"
    ;;
  down) docker compose down "$@" ;;
  logs) docker compose logs -f "$@" ;;
  ps) docker compose ps "$@" ;;
  build) docker compose build "$@" ;;
  health)
    KEY=$(grep -E '^OMNIROUTE_API_KEY=' .env 2>/dev/null | tail -1 | cut -d= -f2- || echo "sk-814c9467fd9c9314-a41c41-72e2d3b0")
    PORT=$(grep -E '^OMNIROUTE_PORT=' .env 2>/dev/null | tail -1 | cut -d= -f2- || echo 20128)
    curl -fsS -H "Authorization: Bearer ${KEY}" "http://localhost:${PORT}/v1/models" | head -c 300 || curl -i "http://localhost:${PORT}/v1/models" || true
    echo
    ;;
  *) echo "usage: $0 {up|down|logs|ps|build|health}"; exit 2;;
esac
