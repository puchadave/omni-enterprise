#!/usr/bin/env bash
set -euo pipefail
PVE_TARGET_STORAGE="${PVE_TARGET_STORAGE:-local-data}"
SHARED_MEDIA_ROOT="${SHARED_MEDIA_ROOT:-/mnt/local-data/omni-assets}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE=(docker compose)
PROFILE="${1:-core,commerce}"
ENV_FILE=".env"
ENV_EXAMPLE=".env.example"

log() { printf '[setup] %s\n' "$*"; }
warn() { printf '[setup][warn] %s\n' "$*" >&2; }
die() { printf '[setup][error] %s\n' "$*" >&2; exit 1; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"; }

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    python3 - <<'PY'
import secrets
print(secrets.token_hex(32))
PY
  fi
}

ensure_env() {
  if [[ ! -f "$ENV_EXAMPLE" ]]; then
    die "Missing $ENV_EXAMPLE"
  fi
  if [[ ! -f "$ENV_FILE" ]]; then
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    log "Created $ENV_FILE from $ENV_EXAMPLE"
  fi

  # Fill __GENERATE__ or empty required secrets
  python3 - "$ENV_FILE" <<'PY'
import secrets, sys, pathlib, re
path = pathlib.Path(sys.argv[1])
text = path.read_text()
required = [
  'POSTGRES_PASSWORD','SESSION_SECRET','INTERNAL_API_KEY','SECRET_KEY','JWT_SECRET',
  'OMNIROUTE_API_KEY','OMNIROUTE_STORAGE_ENCRYPTION_KEY','ADMIN_PASSWORD'
]
for key in required:
    m = re.search(rf'^{re.escape(key)}=(.*)$', text, re.M)
    if not m:
        text += f"\n{key}={secrets.token_hex(32)}\n"
        continue
    val = m.group(1).strip()
    if val in ('', '__GENERATE__', 'change_me', 'change_me_strong_db_password'):
        text = re.sub(rf'^{re.escape(key)}=.*$', f'{key}={secrets.token_hex(32)}', text, flags=re.M)
path.write_text(text)
PY
  chmod 600 "$ENV_FILE" || true
  log "Ensured secrets in $ENV_FILE (chmod 600)"
}

check_ports() {
  python3 - "$ENV_FILE" <<'PY'
import os, re, socket, sys
env = {}
for line in open(sys.argv[1]):
    line=line.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    k,v=line.split('=',1); env[k]=v
ports = []
for k in ['MIDDLEWARE_PORT','OMNIROUTE_PORT','SHOPWARE_PORT','PORTAINER_HTTP_PORT','PORTAINER_HTTPS_PORT','TRAEFIK_HTTP_PORT','TRAEFIK_HTTPS_PORT']:
    v=env.get(k,'').strip()
    if v.isdigit(): ports.append((k,int(v)))
busy=[]
for name,port in ports:
    s=socket.socket(); s.settimeout(0.2)
    try:
        s.connect(('127.0.0.1', port))
        busy.append(f'{name}={port}')
    except Exception:
        pass
    finally:
        s.close()
if busy:
    print('Busy ports (check before live start): ' + ', '.join(busy))
PY
}

wait_http() {
  local url="$1"; local name="$2"; local tries="${3:-60}"
  for _ in $(seq 1 "$tries"); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      log "$name ready: $url"
      return 0
    fi
    sleep 2
  done
  warn "$name not ready after timeout: $url"
  return 1
}

compose_up() {
  local profile_args=()
  IFS=',' read -r -a profiles <<< "$PROFILE"
  for p in "${profiles[@]}"; do
    [[ -n "$p" ]] && profile_args+=(--profile "$p")
  done
  log "Starting compose profiles: $PROFILE"
  "${COMPOSE[@]}" "${profile_args[@]}" up -d --build
}

bootstrap_shopware() {
  local shopware_id
  shopware_id="$(${COMPOSE[@]} ps -q shopware 2>/dev/null || true)"
  [[ -n "$shopware_id" ]] || { log "Shopware profile not running; skipping Shopware bootstrap"; return 0; }

  log "Bootstrapping Shopware plugin/config"
  "${COMPOSE[@]}" exec -T shopware bash -lc 'set -e
    cd /var/www/html || exit 0
    # dockware/dev usually has shopware installed; be defensive
    if [ -f bin/console ]; then
      php bin/console plugin:refresh || true
      php bin/console plugin:install --activate OmniCoreConnector || php bin/console plugin:activate OmniCoreConnector || true
      php bin/console plugin:update OmniCoreConnector || true
      php bin/console cache:clear || true
    fi
  ' || warn "Shopware bootstrap commands failed (check shopware container logs)"

  # Push middleware->shopware config defaults via middleware env; actual secrets in .env
  log "Shopware bootstrap finished (plugin install attempted, migrations handled by plugin lifecycle)"
}

main() {
  need_cmd docker
  need_cmd python3
  docker compose version >/dev/null 2>&1 || die "docker compose plugin required"
  ensure_env
  check_ports

  compose_up

  # Wait for core services
  source "$ENV_FILE"
  wait_http "http://127.0.0.1:${OMNIROUTE_PORT:-20128}/health" "omniroute" 60 || true
  wait_http "http://127.0.0.1:${MIDDLEWARE_PORT:-8002}/healthz" "middleware" 90 || true

  bootstrap_shopware

  log "Setup complete. Next: scripts/healthcheck.sh"
  log "Shopware live path: profile core,commerce ; external marketplaces default disabled (ENABLE_EXTERNAL_MARKETPLACES=false)"
}

main "$@"
