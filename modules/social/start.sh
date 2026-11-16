#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env
dc(){ docker compose "$@"; }
cmd="${1:-up}"; shift || true
case "$cmd" in
  up|start) dc up -d "$@"; dc ps;;
  down|stop) dc down "$@";;
  restart) dc restart "$@";;
  logs) dc logs -f --tail=200 "$@";;
  status|ps) dc ps "$@";;
  config) dc config "$@";;
  pull) dc pull "$@";;
  *) echo "usage: $0 <up|down|restart|logs|status|config|pull>"; exit 2;;
esac
