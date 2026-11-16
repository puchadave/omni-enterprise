#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { cp .env.example .env; echo "[info] .env aus .env.example erstellt (Secrets/URLs prüfen)"; }
dc(){ docker compose "$@"; }
cmd="${1:-up}"; shift || true
case "$cmd" in
  up|start)
    dc up -d --build "$@"
    sleep 3
    dc ps
    echo
    echo "[ok] Middleware:  http://localhost:${MIDDLEWARE_PORT:-8002}  (health: /healthz, UI: /ui/shopware)"
    echo "[ok] OmniRoute:   http://localhost:${OMNIROUTE_PORT:-20128}/v1"
    echo "[ok] Shopware:    http://localhost:${SHOPWARE_PORT:-8088}  (Auto-Integration -> middleware)"
    echo "[ok] Integriert:  social, marketing, content-studio, coding-swarm (verbunden mit middleware+omniroute)"
    ;;
  down|stop) dc down "$@" ;;
  restart) dc down "$@" && dc up -d --build "$@" ;;
  logs) dc logs -f --tail=200 "$@" ;;
  status|ps) dc ps "$@" ;;
  pull) dc pull "$@" ;;
  build) dc build "$@" ;;
  config) dc config "$@" ;;
  health)
    PORT_MW=$(grep -E '^MIDDLEWARE_PORT=' .env 2>/dev/null|tail -1|cut -d= -f2-||echo 8002)
    PORT_OR=$(grep -E '^OMNIROUTE_PORT=' .env 2>/dev/null|tail -1|cut -d= -f2-||echo 20128)
    KEY=$(grep -E '^OMNIROUTE_API_KEY=' .env 2>/dev/null|tail -1|cut -d= -f2-||echo "sk-814c9467fd9c9314-a41c41-72e2d3b0")
    echo "-- middleware /healthz"; curl -fsS "http://localhost:${PORT_MW}/healthz" || echo "FAIL"; echo
    echo "-- omniroute /v1/models"; curl -fsS -H "Authorization: Bearer ${KEY}" "http://localhost:${PORT_OR}/v1/models" | head -c 200 || echo "FAIL"; echo
    echo "-- shopware /"; curl -fsS -o /dev/null -w '%{http_code}' "http://localhost:$(grep -E '^SHOPWARE_PORT=' .env 2>/dev/null|tail -1|cut -d= -f2-||echo 8088)/" || echo " FAIL"; echo
    ;;
  *) echo "usage: $0 <up|down|restart|logs|status|pull|build|config|health> [compose-args]"; exit 2;;
esac
