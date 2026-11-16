#!/usr/bin/env bash
set -euo pipefail
# Erstellt eine Shopware-6 Integration (API-Zugang) für die Middleware.
# Modi:
#   console  - via bin/console integration:create im laufenden Shopware-Container (empfohlen, gibt Keys aus)
#   api      - via Admin-API (benötigt Admin-Login)
#
# Nutzung:
#   ./scripts/create-integration.sh console [--container <name>] [--label omni-middleware]
#   ./scripts/create-integration.sh api --url http://localhost:8088 --admin-user admin --admin-pass shopware [--label omni-middleware]
#
# Ausgabe: Access key ID (CLIENT_ID) und Secret access key (CLIENT_SECRET) sicher notieren.

MODE="${1:-console}"; shift || true
CONTAINER=""
LABEL="omni-middleware"
URL="${SHOPWARE_URL:-http://localhost:8088}"
ADMIN_USER="${SHOPWARE_ADMIN_USER:-admin}"
ADMIN_PASS="${SHOPWARE_ADMIN_PASSWORD:-shopware}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --container) CONTAINER="$2"; shift 2;;
    --label) LABEL="$2"; shift 2;;
    --url) URL="$2"; shift 2;;
    --admin-user) ADMIN_USER="$2"; shift 2;;
    --admin-pass) ADMIN_PASS="$2"; shift 2;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) echo "Unbekannt: $1"; exit 2;;
  esac
done

log(){ echo "[shopware-integration] $*"; }
die(){ echo "[shopware-integration][ERR] $*" >&2; exit 1; }

resolve_container(){
  if [[ -n "$CONTAINER" ]]; then echo "$CONTAINER"; return; fi
  # try compose service name in this module
  local c
  c=$(docker compose ps -q shopware 2>/dev/null | head -1 || true)
  if [[ -n "$c" ]]; then echo "$c"; return; fi
  # fallback common names
  c=$(docker ps --format '{{.Names}}' | grep -E 'shopware' | head -1 || true)
  [[ -n "$c" ]] && echo "$c" || return 1
}

case "$MODE" in
  console)
    command -v docker >/dev/null 2>&1 || die "docker benötigt"
    C=$(resolve_container) || die "Shopware-Container nicht gefunden (läuft er? ./start.sh up). --container angeben."
    log "Erzeuge Integration '$LABEL' via bin/console in Container: $C"
    # Shopware 6 integration:create (admin flag for full access; least-privilege in Prod anpassen)
    set +e
    OUT=$(docker exec "$C" bash -lc "cd /var/www/html 2>/dev/null || cd /sw6 2>/dev/null || cd /var/www/shopware 2>/dev/null; php bin/console integration:create '$LABEL' --admin 2>&1" 2>&1)
    RC=$?
    set -e
    echo "$OUT"
    if [[ $RC -ne 0 ]]; then
      log "Hinweis: CLI-Erstellung fehlgeschlagen/abweichend. Prüfe obenstehende Ausgabe."
      log "Alternativ: Admin UI > Einstellungen > System > Integrationen > Integration anlegen"
      log "oder: $0 api --url $URL --admin-user <user> --admin-pass <pass>"
      exit $RC
    fi
    log "Fertig. Access key ID und Secret access key sicher übernehmen -> SHOPWARE_CLIENT_ID / SHOPWARE_CLIENT_SECRET."
    ;;
  api)
    command -v curl >/dev/null 2>&1 || die "curl benötigt"
    command -v python3 >/dev/null 2>&1 || command -v jq >/dev/null 2>&1 || die "python3 oder jq benötigt"
    URL="${URL%/}"
    log "Hole Admin-OAuth Token von $URL ..."
    TOKEN_RESP=$(curl -sS -X POST "$URL/api/oauth/token" \
      -H 'Content-Type: application/json' \
      -d "{\"grant_type\":\"password\",\"client_id\":\"administration\",\"scopes\":\"write\",\"username\":\"$ADMIN_USER\",\"password\":\"$ADMIN_PASS\"}")
    TOKEN=$(python3 - <<'PY' "$TOKEN_RESP" 2>/dev/null || true
import sys,json
try:
  print(json.loads(sys.argv[1]).get('access_token',''))
except Exception:
  print('')
PY
)
    [[ -n "$TOKEN" ]] || { echo "$TOKEN_RESP"; die "Kein access_token erhalten (URL/Credentials prüfen)."; }
    log "Erzeuge Integration '$LABEL' via Admin-API..."
    RESP=$(curl -sS -X POST "$URL/api/integration" \
      -H "Authorization: Bearer $TOKEN" \
      -H 'Content-Type: application/json' \
      -d "{\"label\":\"$LABEL\",\"admin\":true,\"writeAccess\":true}")
    echo "$RESP"
    log "Prüfe Antwort: id/accessKey/secretAccessKey (Feldnamen je Version) -> als CLIENT_ID/CLIENT_SECRET übernehmen."
    ;;
  *)
    echo "usage: $0 <console|api> [options]"; exit 2;;
esac
