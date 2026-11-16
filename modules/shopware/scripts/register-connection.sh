#!/usr/bin/env bash
set -euo pipefail
# Registriert/aktualisiert eine Shopware-Connection in der Middleware.
# Liest Werte aus .env (SHOPWARE_URL/CLIENT_ID/CLIENT_SECRET/SALES_CHANNEL_ID) oder Argumenten.
#
# Nutzung:
#   ./scripts/register-connection.sh [--middleware http://localhost:8002] [--shop-url URL] [--client-id ID] [--client-secret SECRET] [--sales-channel ID] [--name shopware-main]
#   MIDDLEWARE_INTERNAL_API_KEY kann für authentifizierte Endpunkte gesetzt werden (falls aktiviert).

cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a || true

MIDDLEWARE="${MIDDLEWARE_URL:-http://localhost:8002}"
SHOP_URL="${SHOPWARE_URL:-${SHOPWARE_APP_URL:-}}"
CLIENT_ID="${SHOPWARE_CLIENT_ID:-}"
CLIENT_SECRET="${SHOPWARE_CLIENT_SECRET:-}"
SALES_CHANNEL="${SHOPWARE_SALES_CHANNEL_ID:-}"
NAME="${SHOPWARE_CONNECTION_NAME:-shopware-main}"
API_KEY_HDR=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --middleware) MIDDLEWARE="$2"; shift 2;;
    --shop-url) SHOP_URL="$2"; shift 2;;
    --client-id) CLIENT_ID="$2"; shift 2;;
    --client-secret) CLIENT_SECRET="$2"; shift 2;;
    --sales-channel) SALES_CHANNEL="$2"; shift 2;;
    --name) NAME="$2"; shift 2;;
    -h|--help) sed -n '2,12p' "$0"; exit 0;;
    *) echo "Unbekannt: $1"; exit 2;;
  esac
done

[[ -n "${MIDDLEWARE_INTERNAL_API_KEY:-}" ]] && API_KEY_HDR=(-H "X-Internal-Api-Key: ${MIDDLEWARE_INTERNAL_API_KEY}")
command -v curl >/dev/null 2>&1 || { echo "curl benötigt"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 benötigt"; exit 1; }

[[ -n "$SHOP_URL" && -n "$CLIENT_ID" && -n "$CLIENT_SECRET" ]] || { echo "[ERR] SHOPWARE_URL, SHOPWARE_CLIENT_ID, SHOPWARE_CLIENT_SECRET erforderlich (.env oder Args)."; exit 1; }

PAYLOAD=$(python3 - <<PY
import json
print(json.dumps({
  "shop_url": "$SHOP_URL",
  "client_id": "$CLIENT_ID",
  "client_secret": "$CLIENT_SECRET",
  "sales_channel_id": ${SALES_CHANNEL:+"'$SALES_CHANNEL'"} if False else ("$SALES_CHANNEL" or None),
  "api_version": "6.0",
  "sync_interval": 300,
  "enabled": True,
  "name": "$NAME"
}))
PY
)

echo "[register] POST ${MIDDLEWARE%/}/api/shopware/connections (name=$NAME)"
RESP=$(curl -sS -w "\n%{http_code}" -X POST "${MIDDLEWARE%/}/api/shopware/connections" \
  -H 'Content-Type: application/json' "${API_KEY_HDR[@]}" -d "$PAYLOAD" 2>&1 || true)
BODY=$(echo "$RESP" | sed '$d'); CODE=$(echo "$RESP" | tail -n1)
echo "HTTP $CODE"
echo "$BODY"
if [[ "$CODE" =~ ^2 ]]; then
  echo "[register] OK. Test: curl -sS -X POST ${MIDDLEWARE%/}/api/shopware/connections/<id>/test"
else
  echo "[register] Fehlgeschlagen (Auth/Erreichbarkeit/Middleware-API prüfen)."
  exit 1
fi
