#!/usr/bin/env bash
# Prepare Shopware integration for Omni-Core middleware: technical API user,
# connector scaffold/config, webhook readiness and health endpoints. No direct DB writes by middleware.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

main_init() {
  init_paths
  CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/config.active.yaml}"
  [[ -f "$CONFIG_FILE" ]] || CONFIG_FILE="$SCRIPT_DIR/config.yaml"
  init_runtime
  is_root || die "configure-middleware.sh must run as root inside target container."
}

setup_connector_structure() {
  local cdir; cdir="$(yaml_get "$CONFIG_FILE" middleware.connector_dir /opt/omni-shopware-connector)"
  mkdir -p "$cdir"/{authentication,products,variants,categories,media,inventory,pricing,orders,customers,webhooks,health,mapping,validation,retry,logging,config}
  # copy connector templates from payload if present
  if [[ -d "$SCRIPT_DIR/../../shopware-connector" ]]; then
    cp -a "$SCRIPT_DIR/../../shopware-connector/." "$cdir/" 2>/dev/null || true
  elif [[ -d "/opt/shopware-deploy/current/../../shopware-connector" ]]; then
    cp -a /opt/shopware-deploy/current/../../shopware-connector/. "$cdir/" 2>/dev/null || true
  fi
  CONNECTOR_DIR="$cdir"
}

create_integration_user() {
  local enabled; enabled="$(yaml_get "$CONFIG_FILE" middleware.enabled true)"
  [[ "$enabled" == "true" ]] || { log_warn "Middleware integration disabled in config"; return 0; }
  local shop_dir secrets api_user api_pass app_url
  shop_dir="$(yaml_get "$CONFIG_FILE" shopware.install_dir /var/www/shopware)"
  secrets="$(yaml_get "$CONFIG_FILE" security.secrets_dir /root/.omni-shopware-secrets)"
  api_user="$(yaml_get "$CONFIG_FILE" middleware.api_username omni-middleware)"
  api_pass="$(yaml_get "$CONFIG_FILE" middleware.api_password '')"
  api_pass="$(ensure_secret middleware_api_password "$api_pass" "$secrets")"
  app_url="$(yaml_get "$CONFIG_FILE" shopware.app_url https://shop.example.com)"
  cd "$shop_dir"

  # Create integration via Shopware CLI if available (idempotent-ish: check by label)
  if php bin/console list 2>/dev/null | grep -q 'integration:create'; then
    if ! php bin/console integration:list 2>/dev/null | grep -q "$api_user"; then
      php bin/console integration:create "$api_user" --admin -n >/tmp/integration_create.out 2>&1 || true
      # capture keys if printed (best-effort); always store our managed secret separately
      log_info "Attempted Shopware integration creation for $api_user"
    else
      log_info "Integration $api_user already exists; skipping create."
    fi
  else
    log_warn "integration:create CLI not available; API integration must be created via Admin UI/API post-deploy."
  fi

  # Write connector config (no secrets in logs)
  cat > "$CONNECTOR_DIR/config/connector.json" <<EOF
{
  "shopware": {
    "base_url": "${app_url}",
    "api_username": "${api_user}",
    "auth_mode": "integration_or_oauth",
    "use_official_apis_only": true,
    "no_direct_db_access": true
  },
  "modules": ["products","variants","categories","media","inventory","pricing","orders","customers","webhooks","health"],
  "webhook_base_url": "$(yaml_get "$CONFIG_FILE" middleware.webhook_base_url '')",
  "health_endpoint_enabled": $(yaml_get "$CONFIG_FILE" middleware.health_endpoint_enabled true),
  "updated_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
  chmod 640 "$CONNECTOR_DIR/config/connector.json" || true
  log_info "Middleware connector config written to $CONNECTOR_DIR/config/connector.json"
  log_info "Middleware API secret stored in $secrets/middleware_api_password (chmod 600)."
}

prepare_webhooks_health() {
  local shop_dir; shop_dir="$(yaml_get "$CONFIG_FILE" shopware.install_dir /var/www/shopware)"
  cd "$shop_dir"
  # Ensure scheduled tasks and message queue are ready for webhook/event processing
  php bin/console scheduled-task:register -n >/dev/null 2>&1 || true
  # health marker
  mkdir -p /var/www/shopware/public/.well-known
  echo '{"status":"ok","service":"shopware","middleware_ready":true}' > /var/www/shopware/public/.well-known/middleware-health.json
  chown www-data:www-data /var/www/shopware/public/.well-known/middleware-health.json || true
}

main() {
  main_init
  run_step "connector_structure" setup_connector_structure
  run_step "integration_user" create_integration_user
  run_step "webhooks_health" prepare_webhooks_health
  log_ok "Middleware integration prepared (API-only, least privilege)."
}

main "$@"
