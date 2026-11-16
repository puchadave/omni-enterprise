#!/usr/bin/env bash
set -euo pipefail
# Idempotent bootstrap: ensures a Shopware 6 Admin API integration exists for the middleware
# and (optionally) registers the connection in the middleware so it can always connect.
#
# Usage:
#   ./scripts/bootstrap-integration.sh [--auto] [--register] [--no-register]
#     [--container <name|id>] [--label omni-middleware]
#     [--middleware-url URL] [--shop-url URL] [--wait <sec>]
#
# Behavior:
# - If credentials already stored in integration/credentials.env (and valid-looking), reuse them.
# - Otherwise creates integration via `bin/console integration:create <label> --admin` in the Shopware container.
# - Persists credentials to integration/credentials.env (+ live/* json), and updates .env keys (idempotent).
# - With --register/--auto tries to register/update connection in middleware (best-effort, retries briefly).
#
# Run from module dir (modules/shopware) or let it resolve paths.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODULE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "$MODULE_DIR"

[[ -f .env ]] && set -a && source .env && set +a || true

LABEL="${SHOPWARE_INTEGRATION_LABEL:-omni-middleware}"
CONTAINER="${SHOPWARE_CONTAINER:-}"
MIDDLEWARE_URL_IN="${MIDDLEWARE_URL:-}"
SHOP_URL_IN="${SHOPWARE_URL:-${SHOPWARE_APP_URL:-}}"
WAIT_SEC="${BOOTSTRAP_WAIT_SEC:-300}"
DO_REGISTER="auto"   # auto|yes|no
AUTO_MODE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --auto) AUTO_MODE=true; DO_REGISTER="auto"; shift;;
    --register) DO_REGISTER="yes"; shift;;
    --no-register) DO_REGISTER="no"; shift;;
    --container) CONTAINER="$2"; shift 2;;
    --label) LABEL="$2"; shift 2;;
    --middleware-url) MIDDLEWARE_URL_IN="$2"; shift 2;;
    --shop-url) SHOP_URL_IN="$2"; shift 2;;
    --wait) WAIT_SEC="$2"; shift 2;;
    -h|--help) sed -n '2,18p' "$0"; exit 0;;
    *) echo "[bootstrap][ERR] unknown arg: $1"; exit 2;;
  esac
done

log(){ echo "[shopware-bootstrap] $*"; }
warn(){ echo "[shopware-bootstrap][WARN] $*" >&2; }
err(){ echo "[shopware-bootstrap][ERR] $*" >&2; }

mkdir -p integration/live

CRED_FILE="integration/credentials.env"
CONN_JSON="integration/live/middleware-connection.json"
CONNECTOR_JSON="integration/live/connector.json"

# defaults
SHOP_URL="${SHOP_URL_IN:-http://localhost:${SHOPWARE_PORT:-8088}}"
MIDDLEWARE_URL="${MIDDLEWARE_URL_IN:-http://localhost:8002}"

have_docker(){ command -v docker >/dev/null 2>&1; }

resolve_container(){
  if [[ -n "$CONTAINER" ]]; then echo "$CONTAINER"; return 0; fi
  local c=""
  if have_docker; then
    c=$(docker compose ps -q shopware 2>/dev/null | head -1 || true)
    if [[ -z "$c" ]]; then c=$(docker ps --format '{{.Names}}\t{{.Image}}' 2>/dev/null | awk '/shopware|dockware/{print $1; exit}' || true); fi
  fi
  [[ -n "$c" ]] && { echo "$c"; return 0; } || return 1
}

wait_ready(){
  local url="${SHOPWARE_APP_URL:-http://localhost:${SHOPWARE_PORT:-8088}}" end=$((SECONDS+WAIT_SEC))
  log "Warte auf Shopware ($url, timeout ${WAIT_SEC}s)..."
  while (( SECONDS < end )); do
    if command -v curl >/dev/null 2>&1; then
      curl -fsS -o /dev/null "$url" 2>/dev/null && { log "Shopware erreichbar."; return 0; }
      curl -fsS -o /dev/null "$url/api/_info/version" 2>/dev/null && { log "Shopware API erreichbar."; return 0; }
    elif have_docker; then
      local c; c=$(resolve_container 2>/dev/null || true)
      [[ -n "$c" ]] && docker exec "$c" sh -lc 'wget -q --spider http://127.0.0.1/ || curl -fsS -o /dev/null http://127.0.0.1/' >/dev/null 2>&1 && { log "Shopware (im Container) erreichbar."; return 0; }
    fi
    sleep 5
  done
  warn "Shopware nicht innerhalb Timeout bereit."
  return 1
}

read_creds_file(){
  local id="" secret="" sales=""
  if [[ -f "$CRED_FILE" ]]; then
    # shellcheck disable=SC1090
    set +u; source "$CRED_FILE" 2>/dev/null || true; set -u
    id="${SHOPWARE_CLIENT_ID:-${CLIENT_ID:-${ACCESS_KEY_ID:-}}}"
    secret="${SHOPWARE_CLIENT_SECRET:-${CLIENT_SECRET:-${SECRET_ACCESS_KEY:-}}}"
    sales="${SHOPWARE_SALES_CHANNEL_ID:-${SALES_CHANNEL_ID:-}}"
  fi
  CLIENT_ID="$id"; CLIENT_SECRET="$secret"; SALES_CHANNEL_ID="${sales:-${SHOPWARE_SALES_CHANNEL_ID:-}}"
}

valid_creds(){ [[ -n "${CLIENT_ID:-}" && -n "${CLIENT_SECRET:-}" && "${CLIENT_ID}" != "<"* ]]; }

parse_console_output(){
  # args: output -> sets CLIENT_ID/CLIENT_SECRET
  local out="$1"
  local id secret
  # common formats: "Access key ID: ..." / "Secret access key: ..." or table/json-like
  id=$(echo "$out" | grep -iE 'Access key ID|accessKeyId|access_key' | head -1 | sed -E 's/.*:\s*"?([^"\r\n ]+)"?.*/\1/' | tr -d '\r')
  secret=$(echo "$out" | grep -iE 'Secret access key|secretAccessKey|secret_access_key|userSecret' | head -1 | sed -E 's/.*:\s*"?([^"\r\n ]+)"?.*/\1/' | tr -d '\r')
  # fallback: try json parse
  if [[ -z "$id" || -z "$secret" ]] && command -v python3 >/dev/null 2>&1; then
    local pj
    pj=$(python3 - <<'PY' "$out" 2>/dev/null || true
import sys, json, re
s=sys.argv[1]
# try find json object
m=re.search(r'\{.*\}', s, re.S)
if m:
  try:
    o=json.loads(m.group(0))
    print(o.get('accessKey') or o.get('accessKeyId') or o.get('id',''))
    print(o.get('secretAccessKey') or o.get('userSecret') or o.get('secret',''))
    raise SystemExit
  except Exception: pass
print(''); print('')
PY
)
    [[ -z "$id" ]] && id=$(echo "$pj" | sed -n '1p')
    [[ -z "$secret" ]] && secret=$(echo "$pj" | sed -n '2p')
  fi
  [[ -n "$id" ]] && CLIENT_ID="$id"
  [[ -n "$secret" ]] && CLIENT_SECRET="$secret"
}

create_integration_console(){
  have_docker || { err "docker CLI benötigt für console-Erstellung."; return 1; }
  local c; c=$(resolve_container) || { err "Shopware-Container nicht gefunden."; return 1; }
  log "Erzeuge/ermittle Integration '$LABEL' via bin/console im Container $c ..."
  local out rc
  set +e
  out=$(docker exec "$c" sh -lc "cd /var/www/html 2>/dev/null || cd /sw6 2>/dev/null || cd /var/www/shopware 2>/dev/null || true; php bin/console integration:create '$LABEL' --admin 2>&1" 2>&1)
  rc=$?
  set -e
  echo "$out"
  if [[ $rc -eq 0 ]]; then
    parse_console_output "$out"
    if valid_creds; then
      log "Integration erstellt (Keys geparst)."
      return 0
    else
      warn "Integration erstellt, aber Keys konnten nicht geparst werden. Bitte Keys aus Ausgabe übernehmen und in $CRED_FILE speichern."
      return 2
    fi
  fi
  # if already exists, console may fail - try to detect and inform (secret not recoverable)
  if echo "$out" | grep -qiE 'already|exists|duplicate|unique|Integrity constraint'; then
    warn "Integration '$LABEL' existiert vermutlich bereits, Secret ist nicht erneut abrufbar."
    warn "Falls keine gespeicherten Credentials vorliegen: alte Integration in Shopware löschen/rotieren und Bootstrap erneut ausführen."
    return 3
  fi
  warn "integration:create fehlgeschlagen (rc=$rc)."
  return 1
}

write_creds(){
  umask 077
  cat > "$CRED_FILE" <<EOF
# Auto-generated by bootstrap-integration.sh ($(date -u +%Y-%m-%dT%H:%M:%SZ)). Do not commit.
SHOPWARE_CLIENT_ID=${CLIENT_ID}
SHOPWARE_CLIENT_SECRET=${CLIENT_SECRET}
SHOPWARE_SALES_CHANNEL_ID=${SALES_CHANNEL_ID:-}
SHOPWARE_INTEGRATION_LABEL=${LABEL}
EOF
  chmod 600 "$CRED_FILE" 2>/dev/null || true

  # update .env keys idempotently
  if [[ -f .env ]]; then
    python3 - "$PWD/.env" "$CLIENT_ID" "$CLIENT_SECRET" "${SALES_CHANNEL_ID:-}" <<'PY' 2>/dev/null || true
import sys, re, pathlib
p=pathlib.Path(sys.argv[1]); cid,sec,sc=sys.argv[2],sys.argv[3],sys.argv[4]
s=p.read_text()
def setkv(s,k,v):
    pat=r'^'+re.escape(k)+r'=.*$'
    if re.search(pat,s,re.M): return re.sub(pat,f'{k}={v}',s,flags=re.M)
    return s.rstrip()+"\n"+f"{k}={v}\n"
for k,v in [('SHOPWARE_CLIENT_ID',cid),('SHOPWARE_CLIENT_SECRET',sec),('SHOPWARE_ENABLED','true')]:
    s=setkv(s,k,v)
if sc: s=setkv(s,'SHOPWARE_SALES_CHANNEL_ID',sc)
p.write_text(s)
PY
  fi

  # write live json artifacts
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$SHOP_URL" "$CLIENT_ID" "$CLIENT_SECRET" "${SALES_CHANNEL_ID:-}" "$MIDDLEWARE_URL" "$CONN_JSON" "$CONNECTOR_JSON" <<'PY'
import json,sys
shop,cid,sec,sc,mw,conn_p,connector_p=sys.argv[1:8]
sc = sc or None
conn={"shop_url":shop,"client_id":cid,"client_secret":sec,"sales_channel_id":sc,"api_version":"6.0","sync_interval":300,"enabled":True,"name":"shopware-main"}
connector={"shopware":{"base_url":shop,"client_id":cid,"client_secret":sec,"sales_channel_id":sc,"api_version":"6.0","auth_mode":"integration_oauth","use_official_apis_only":True,"no_direct_db_access":True},"modules":["products","variants","categories","media","inventory","pricing","orders","customers","webhooks","health"],"webhook_base_url":(mw.rstrip('/')+"/api/webhooks/shopware" if mw else ""), "health_endpoint_enabled":True}
open(conn_p,'w').write(json.dumps(conn,indent=2))
open(connector_p,'w').write(json.dumps(connector,indent=2))
PY
    chmod 600 "$CONN_JSON" "$CONNECTOR_JSON" 2>/dev/null || true
  fi
  log "Credentials/Configs gespeichert: $CRED_FILE, $CONN_JSON"
}

register_connection(){
  [[ "$DO_REGISTER" == "no" ]] && { log "Registrierung übersprungen (--no-register)."; return 0; }
  if [[ "$DO_REGISTER" == "auto" && -z "$MIDDLEWARE_URL_IN" && -z "${MIDDLEWARE_URL:-}" ]]; then
    log "Keine MIDDLEWARE_URL gesetzt - Registrierung übersprungen (später: ./scripts/register-connection.sh)."
    return 0
  fi
  command -v curl >/dev/null 2>&1 || { warn "curl fehlt - Registrierung nicht möglich."; return 0; }
  command -v python3 >/dev/null 2>&1 || { warn "python3 fehlt - Registrierung nicht möglich."; return 0; }
  local url="${MIDDLEWARE_URL%/}/api/shopware/connections"
  log "Registriere Connection in Middleware: $url"
  # brief reachability/retry loop
  local tries=12 ok=false
  for i in $(seq 1 $tries); do
    if curl -fsS -o /dev/null --max-time 3 "${MIDDLEWARE_URL%/}/healthz" 2>/dev/null || curl -fsS -o /dev/null --max-time 3 "${MIDDLEWARE_URL%/}/health" 2>/dev/null || curl -fsS -o /dev/null --max-time 3 "$url" 2>/dev/null; then
      ok=true; break
    fi
    (( i < tries )) && sleep 5
  done
  if [[ "$ok" != true && "$AUTO_MODE" == true ]]; then
    warn "Middleware nicht erreichbar - Registrierung ausgelassen (erneut mit ./scripts/register-connection.sh)."
    return 0
  fi
  local payload
  payload=$(python3 - <<PY
import json
print(json.dumps({"shop_url":"$SHOP_URL","client_id":"$CLIENT_ID","client_secret":"$CLIENT_SECRET","sales_channel_id":("${SALES_CHANNEL_ID}" or None),"api_version":"6.0","sync_interval":300,"enabled":True,"name":"shopware-main"}))
PY
)
  local hdrs=(-H 'Content-Type: application/json')
  [[ -n "${MIDDLEWARE_INTERNAL_API_KEY:-}" ]] && hdrs+=(-H "X-Internal-Api-Key: ${MIDDLEWARE_INTERNAL_API_KEY}")
  local resp code body
  resp=$(curl -sS -w "\n%{http_code}" --max-time 15 -X POST "$url" "${hdrs[@]}" -d "$payload" 2>&1 || true)
  body=$(echo "$resp" | sed '$d'); code=$(echo "$resp" | tail -n1)
  if [[ "$code" =~ ^2 ]]; then
    log "Middleware-Connection registriert (HTTP $code)."
    echo "$body"
    # try to extract id and test
    local cid
    cid=$(python3 - <<PY "$body" 2>/dev/null || true
import sys,json
try: o=json.loads(sys.argv[1]); print(o.get('id') or o.get('connection_id') or '')
except Exception: print('')
PY
)
    if [[ -n "$cid" ]]; then
      log "Teste Connection $cid ..."
      curl -sS --max-time 20 -X POST "${MIDDLEWARE_URL%/}/api/shopware/connections/${cid}/test" "${hdrs[@]}" || true
      echo
    fi
  else
    warn "Registrierung fehlgeschlagen (HTTP ${code:-?}). Antwort: ${body:0:300}"
    warn "Manuell nachholen: ./scripts/register-connection.sh"
    return 0
  fi
}

main(){
  log "Bootstrap-Integration startet (label=$LABEL, shop_url=$SHOP_URL)"
  wait_ready || true

  read_creds_file
  if valid_creds; then
    log "Vorhandene Credentials gefunden ($CRED_FILE/.env) - wiederverwenden."
  else
    if ! create_integration_console; then
      rc=$?
      if [[ $rc -eq 3 ]]; then
        err "Keine wiederverwendbaren Credentials und bestehende Integration ohne gespeichertes Secret."
        err "Lösung: Integration in Shopware Admin löschen/rotieren, dann erneut: ./scripts/bootstrap-integration.sh"
      else
        err "Konnte keine Integration/Credentials bereitstellen. Siehe Hinweise oben."
      fi
      exit 1
    fi
  fi

  if ! valid_creds; then
    err "Keine gültigen Credentials verfügbar."; exit 1
  fi

  write_creds
  register_connection || true

  log "Fertig. Middleware kann Verbindung aufbauen mit SHOPWARE_URL=$SHOP_URL (CLIENT_ID gesetzt)."
  log "Config: $CONN_JSON"
}

main "$@"
