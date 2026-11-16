#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"
ENV_FILE="$ROOT_DIR/.env"

log(){ printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$*"; }
die(){ echo "[ERR] $*" >&2; exit 1; }

randhex(){ (cat /proc/sys/kernel/random/uuid 2>/dev/null; echo; cat /proc/sys/kernel/random/uuid 2>/dev/null) | tr -d '-\n' | cut -c1-${1:-32}; }

ensure_env(){
  if [ ! -f "$ENV_FILE" ]; then
    [ -f .env.example ] || die ".env.example fehlt"
    cp .env.example "$ENV_FILE"
    log ".env aus .env.example erstellt"
  fi
  set_kv(){ local k="$1" v="$2"; if grep -q "^${k}=" "$ENV_FILE"; then sed -i "s|^${k}=.*|${k}=${v}|" "$ENV_FILE"; else echo "${k}=${v}" >> "$ENV_FILE"; fi; }
  gen_if(){ local k="$1" def="$2"; local cur
    cur=$(grep -E "^${k}=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true)
    if [ -z "$cur" ] || [ "$cur" = "change_me" ] || [ "$cur" = "change_me_strong" ] || [[ "$cur" == change_me_* ]]; then
      set_kv "$k" "$def"; log "${k} generiert"; fi
  }
  gen_if POSTGRES_PASSWORD "$(randhex 24)"
  gen_if SESSION_SECRET "$(randhex 48)"
  gen_if SECRET_KEY "$(randhex 48)"
  gen_if JWT_SECRET "$(randhex 48)"
  # ensure paths
  MEDIA_PATH=$(grep -E '^MEDIA_PATH=' "$ENV_FILE" | tail -1 | cut -d= -f2- || echo ./data/media)
  UPLOADS_PATH=$(grep -E '^UPLOADS_PATH=' "$ENV_FILE" | tail -1 | cut -d= -f2- || echo ./data/uploads)
  mkdir -p "$MEDIA_PATH" "$UPLOADS_PATH" ./data ./db/init
}

dc(){ docker compose "$@"; }

cmd="${1:-up}"; shift || true
case "$cmd" in
  up|start)
    ensure_env
    dc up -d --build "$@"
    sleep 3
    dc ps
    PORT=$(grep -E '^OMNI_CORE_PORT=' .env | tail -1 | cut -d= -f2- || echo 8002)
    log "Health:  curl -s http://localhost:${PORT:-8002}/healthz"
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
    PORT=$(grep -E '^OMNI_CORE_PORT=' .env | tail -1 | cut -d= -f2- || echo 8002)
    curl -sS "http://127.0.0.1:${PORT:-8002}/healthz" || curl -sS "http://127.0.0.1:${PORT:-8002}/health" || true
    echo
    ;;
  *) echo "usage: $0 <up|down|restart|logs|status|pull|build|config|health>"; exit 2;;
esac
