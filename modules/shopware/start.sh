#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { cp .env.example .env; echo "[info] .env erstellt"; }
dc(){ docker compose "$@"; }
cmd="${1:-up}"; shift || true
case "$cmd" in
  up|start) dc up -d "$@"; sleep 2; dc ps; echo "[info] Shopware: http://localhost:$(grep -E '^SHOPWARE_PORT=' .env|tail -1|cut -d= -f2-||echo 8088)";;
  down|stop) dc down "$@";;
  restart) dc restart "$@";;
  logs) dc logs -f --tail=200 "$@";;
  status|ps) dc ps "$@";;
  pull) dc pull "$@";;
  config) dc config "$@";;
  *) echo "usage: $0 <up|down|restart|logs|status|pull|config>"; exit 2;;
esac
