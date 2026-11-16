#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
ENV_FILE="$ROOT_DIR/.env"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"; }
die(){ echo "[ERR] $*" >&2; exit 1; }

ensure_env(){
  if [ ! -f "$ENV_FILE" ]; then
    [ -f .env.example ] || die ".env.example fehlt"
    cp .env.example "$ENV_FILE"
    log ".env aus .env.example erstellt"
  fi
  # generate secrets if placeholders
  if grep -q '^OMNIROUTE_API_KEY=__GENERATE__' "$ENV_FILE" || ! grep -q '^OMNIROUTE_API_KEY=.' "$ENV_FILE"; then
    KEY="omni-$( (cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "$(date +%s)-$$") | tr -d '-' | cut -c1-24)"
    if grep -q '^OMNIROUTE_API_KEY=' "$ENV_FILE"; then sed -i "s|^OMNIROUTE_API_KEY=.*|OMNIROUTE_API_KEY=$KEY|" "$ENV_FILE"; else echo "OMNIROUTE_API_KEY=$KEY" >> "$ENV_FILE"; fi
    log "OMNIROUTE_API_KEY generiert"
  fi
  if grep -q '^OMNIROUTE_STORAGE_ENCRYPTION_KEY=__GENERATE__' "$ENV_FILE" || ! grep -q '^OMNIROUTE_STORAGE_ENCRYPTION_KEY=.' "$ENV_FILE"; then
    SKEY="$( (cat /proc/sys/kernel/random/uuid 2>/dev/null; echo; cat /proc/sys/kernel/random/uuid 2>/dev/null) | tr -d '-\n' | cut -c1-48)"
    if grep -q '^OMNIROUTE_STORAGE_ENCRYPTION_KEY=' "$ENV_FILE"; then sed -i "s|^OMNIROUTE_STORAGE_ENCRYPTION_KEY=.*|OMNIROUTE_STORAGE_ENCRYPTION_KEY=$SKEY|" "$ENV_FILE"; else echo "OMNIROUTE_STORAGE_ENCRYPTION_KEY=$SKEY" >> "$ENV_FILE"; fi
    log "OMNIROUTE_STORAGE_ENCRYPTION_KEY generiert"
  fi
}

dc(){ docker compose "$@"; }

cmd="${1:-up}"; shift || true
case "$cmd" in
  up|start)
    ensure_env
    mkdir -p config data logs
    dc up -d "$@"
    sleep 2
    dc ps
    PORT=$(grep -E '^OMNIROUTE_PORT=' .env | tail -1 | cut -d= -f2- || echo 20128)
    log "Endpoint: http://localhost:${PORT:-20128}/v1"
    ;;
  down|stop) ensure_env; dc down "$@" ;;
  restart)   "$0" down; "$0" up "$@" ;;
  logs)      ensure_env; dc logs -f --tail=200 "$@" ;;
  status|ps) ensure_env; dc ps "$@" ;;
  pull)      ensure_env; dc pull "$@" ;;
  build)     ensure_env; dc build "$@" ;;
  config)    ensure_env; dc config "$@" ;;
  health)
    ensure_env
    KEY=$(grep -E '^OMNIROUTE_API_KEY=' .env | tail -1 | cut -d= -f2- || true)
    PORT=$(grep -E '^OMNIROUTE_PORT=' .env | tail -1 | cut -d= -f2- || echo 20128)
    curl -sS -o /dev/null -w "http=%{http_code}\n" -H "Authorization: Bearer $KEY" "http://127.0.0.1:${PORT:-20128}/v1/models" || true
    ;;
  *) echo "usage: $0 <up|down|restart|logs|status|pull|build|config|health>"; exit 2;;
esac
