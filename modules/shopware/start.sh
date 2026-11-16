#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { cp .env.example .env; echo "[info] .env erstellt"; }
dc(){ docker compose "$@"; }
cmd="${1:-up}"; shift || true
case "$cmd" in
  up|start)
    dc up -d "$@"
    sleep 2
    dc ps
    echo "[info] Shopware: ${SHOPWARE_APP_URL:-http://localhost:$(grep -E '^SHOPWARE_PORT=' .env 2>/dev/null|tail -1|cut -d= -f2-||echo 8088)}"
    echo "[info] Auto-Integration/Bootstrap läuft als Service 'shopware-bootstrap' (Keys in integration/credentials.env). Logs: ./start.sh logs shopware-bootstrap"
    ;;
  down|stop) dc down "$@";;
  restart) dc restart "$@";;
  logs) dc logs -f --tail=200 "$@";;
  status|ps) dc ps "$@";;
  pull) dc pull "$@";;
  config) dc config "$@";;
  wait) ./scripts/wait-ready.sh "$@";;
  bootstrap|integrate) ./scripts/bootstrap-integration.sh "$@";;
  integration-create) ./scripts/create-integration.sh "$@";;
  integration-register) ./scripts/register-connection.sh "$@";;
  *) echo "usage: $0 <up|down|restart|logs|status|pull|config|wait|bootstrap|integrate|integration-create|integration-register> [args]"; exit 2;;
esac
