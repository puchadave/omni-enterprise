#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { cp .env.example .env; echo "[info] .env erstellt - SHOPWARE_ACCESS_KEY_ID/SECRET setzen (aus modules/shopware/integration/credentials.env)"; }
dc(){ docker compose "$@"; }
cmd="${1:-up}"; shift || true
case "$cmd" in
  up|start) dc up -d "$@"; sleep 2; dc ps; echo "[info] Shopware MCP: http://localhost:$(grep -E '^MCP_PORT=' .env 2>/dev/null|tail -1|cut -d= -f2-||echo 7331) (SHOPWARE_URL=$(grep -E '^SHOPWARE_URL=' .env 2>/dev/null|tail -1|cut -d= -f2-||echo ''))";;
  down|stop) dc down "$@";;
  restart) dc restart "$@";;
  logs) dc logs -f --tail=200 "$@";;
  status|ps) dc ps "$@";;
  config) dc config "$@";;
  health) dc ps "$@";;
  *) echo "usage: $0 <up|down|restart|logs|status|config|health>"; exit 2;;
esac
