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

wait_shopware_console() {
  local tries="${1:-90}"
  for _ in $(seq 1 "$tries"); do
    if "${COMPOSE[@]}" exec -T shopware bash -lc 'cd /var/www/html && php bin/console list --raw >/dev/null 2>&1'; then
      log "Shopware console ready"
      return 0
    fi
    sleep 2
  done
  warn "Shopware console not ready after timeout"
  return 1
}

ensure_storefront_domain() {
  local shopware_id
  shopware_id="$(${COMPOSE[@]} ps -q shopware 2>/dev/null || true)"
  [[ -n "$shopware_id" ]] || { log "Shopware profile not running; skipping storefront domain"; return 0; }

  local storefront_url="${STOREFRONT_URL:-}"
  if [[ -z "$storefront_url" ]]; then
    local host_ip
    host_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    storefront_url="http://${host_ip:-localhost}:${SHOPWARE_PORT:-8088}"
  fi
  storefront_url="${storefront_url%/}"
  log "Ensuring Shopware storefront domain: $storefront_url"

  if ! "${COMPOSE[@]}" exec -T -e STOREFRONT_URL="$storefront_url" shopware php <<'PHP'
<?php
declare(strict_types=1);

$storefrontUrl = rtrim((string)getenv('STOREFRONT_URL'), '/');
if ($storefrontUrl === '') {
    fwrite(STDERR, "STOREFRONT_URL is empty\n");
    exit(2);
}
if (!preg_match('#^https?://#i', $storefrontUrl)) {
    $storefrontUrl = 'http://' . $storefrontUrl;
}
$storefrontUrl = rtrim($storefrontUrl, '/');

$envFile = '/var/www/html/.env';
$dbUrl = '';
if (is_readable($envFile)) {
    foreach (file($envFile, FILE_IGNORE_NEW_LINES) as $line) {
        if (str_starts_with(trim($line), 'DATABASE_URL=')) {
            $dbUrl = trim(substr(trim($line), strlen('DATABASE_URL=')));
            break;
        }
    }
}
if ($dbUrl === '') {
    fwrite(STDERR, "DATABASE_URL not found in $envFile\n");
    exit(2);
}
$parts = parse_url($dbUrl);
if ($parts === false || empty($parts['host'])) {
    fwrite(STDERR, "Invalid DATABASE_URL\n");
    exit(2);
}
$dbName = ltrim((string)($parts['path'] ?? ''), '/');
$dsn = sprintf(
    'mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4',
    $parts['host'],
    (int)($parts['port'] ?? 3306),
    $dbName
);
$pdo = new PDO(
    $dsn,
    rawurldecode((string)($parts['user'] ?? '')),
    rawurldecode((string)($parts['pass'] ?? '')),
    [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION]
);

$tableExists = static function (string $table) use ($pdo): bool {
    $stmt = $pdo->prepare('SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = ?');
    $stmt->execute([$table]);
    return (int)$stmt->fetchColumn() > 0;
};
if (!$tableExists('sales_channel') || !$tableExists('sales_channel_domain')) {
    fwrite(STDERR, "Shopware schema not installed yet; skipping domain registration\n");
    exit(0);
}

$stmt = $pdo->prepare('SELECT COUNT(*) FROM sales_channel_domain WHERE url = ?');
$stmt->execute([$storefrontUrl]);
if ((int)$stmt->fetchColumn() > 0) {
    echo "Storefront domain already registered: $storefrontUrl\n";
    exit(0);
}

$storefrontTypeId = '8a243080f92e4c719546314b577cf82b';
$stmt = $pdo->prepare('SELECT id FROM sales_channel WHERE type_id = UNHEX(?) LIMIT 1');
$stmt->execute([$storefrontTypeId]);
$channelId = $stmt->fetchColumn();
if ($channelId === false) {
    fwrite(STDERR, "No storefront sales channel found; skipping\n");
    exit(0);
}
$channelHex = strtoupper(bin2hex((string)$channelId));

$stmt = $pdo->prepare('SELECT HEX(language_id), HEX(currency_id), HEX(snippet_set_id) FROM sales_channel_domain WHERE sales_channel_id = UNHEX(?) ORDER BY created_at ASC LIMIT 1');
$stmt->execute([$channelHex]);
$template = $stmt->fetch(PDO::FETCH_NUM);
if ($template) {
    [$languageHex, $currencyHex, $snippetSetHex] = $template;
} else {
    $languageHex = $pdo->query('SELECT HEX(id) FROM language WHERE parent_id IS NULL ORDER BY created_at ASC LIMIT 1')->fetchColumn();
    $currencyHex = $pdo->query("SELECT HEX(id) FROM currency WHERE iso_code = 'EUR' LIMIT 1")->fetchColumn();
    $snippetSetHex = $pdo->query('SELECT HEX(id) FROM snippet_set ORDER BY created_at ASC LIMIT 1')->fetchColumn();
}
if (!$languageHex || !$currencyHex || !$snippetSetHex) {
    fwrite(STDERR, "Could not resolve language/currency/snippet set for storefront domain\n");
    exit(2);
}

$stmt = $pdo->prepare('INSERT INTO sales_channel_domain (id, sales_channel_id, language_id, url, currency_id, snippet_set_id, created_at) VALUES (UNHEX(?), UNHEX(?), UNHEX(?), ?, UNHEX(?), UNHEX(?), NOW(3))');
$stmt->execute([
    bin2hex(random_bytes(16)),
    $channelHex,
    $languageHex,
    $storefrontUrl,
    $currencyHex,
    $snippetSetHex,
]);
echo "Registered storefront domain: $storefrontUrl\n";
PHP
  then
    warn "Storefront domain registration failed (see output above)"
    return 0
  fi

  "${COMPOSE[@]}" exec -T shopware bash -lc 'cd /var/www/html && php bin/console cache:clear' \
    || warn "Shopware cache:clear failed after domain registration"
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

  wait_shopware_console 90 || true
  bootstrap_shopware
  ensure_storefront_domain

  log "Setup complete. Next: scripts/healthcheck.sh"
  log "Shopware live path: profile core,commerce ; external marketplaces default disabled (ENABLE_EXTERNAL_MARKETPLACES=false)"
}

main "$@"
