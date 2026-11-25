#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
COMPOSE=(docker compose)
PROFILE="${1:-core,commerce}"
BACKUP_DIR="backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"
log(){ printf '[update] %s\n' "$*"; }
warn(){ printf '[update][warn] %s\n' "$*" >&2; }

IFS=',' read -r -a profiles <<< "$PROFILE"
profile_args=()
for p in "${profiles[@]}"; do [[ -n "$p" ]] && profile_args+=(--profile "$p"); done

log "Backup compose config and env metadata"
cp docker-compose.yml "$BACKUP_DIR/" || true
[ -f .env ] && grep -vE '(SECRET|PASSWORD|KEY|TOKEN)' .env > "$BACKUP_DIR/env.redacted" || true

log "DB backup (if db running)"
if ${COMPOSE[@]} ps -q db >/dev/null 2>&1 && [ -n "$(${COMPOSE[@]} ps -q db)" ]; then
  source .env 2>/dev/null || true
  ${COMPOSE[@]} exec -T db sh -lc 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$BACKUP_DIR/db.sql" || warn "DB backup failed"
fi

log "Pull/build and rolling update"
${COMPOSE[@]} "${profile_args[@]}" pull --ignore-pull-failures || true
${COMPOSE[@]} "${profile_args[@]}" up -d --build

log "Post-update migrations/cache"
${COMPOSE[@]} exec -T shopware bash -lc 'if [ -f /var/www/html/bin/console ]; then cd /var/www/html && php bin/console plugin:refresh && php bin/console plugin:update OmniCoreConnector || true; php bin/console cache:clear || true; fi' || true

log "Update complete. Run scripts/healthcheck.sh"
