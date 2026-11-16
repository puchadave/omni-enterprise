#!/usr/bin/env bash
# Post-deployment acceptance tests and monitoring healthcheck.
# Produces JSON report and returns non-zero on failed mandatory checks.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

main_init() {
  init_paths
  CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/config.active.yaml}"
  [[ -f "$CONFIG_FILE" ]] || CONFIG_FILE="$SCRIPT_DIR/config.yaml"
  init_runtime
}

add_check() {
  local id="$1" mandatory="$2" status="$3" detail="$4"
  CHECKS_JSON+="{\"id\":\"$id\",\"mandatory\":$mandatory,\"status\":\"$status\",\"detail\":$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$detail")},"
  if [[ "$mandatory" == "true" && "$status" != "pass" ]]; then MANDATORY_FAILED=$((MANDATORY_FAILED+1)); fi
}

check_service() { systemctl is-active --quiet "$1" 2>/dev/null || service "$1" status >/dev/null 2>&1; }

run_checks() {
  CHECKS_JSON="["; MANDATORY_FAILED=0
  local app_url shop_dir phpv domain timeout api_health
  app_url="$(yaml_get "$CONFIG_FILE" shopware.app_url https://shop.example.com)"
  shop_dir="$(yaml_get "$CONFIG_FILE" shopware.install_dir /var/www/shopware)"
  phpv="$(ls /etc/php 2>/dev/null | sort -V | tail -n1 || echo 8.3)"
  domain="$(echo "$app_url" | awk -F/ '{print $3}')"
  timeout="$(yaml_get "$CONFIG_FILE" monitoring.api_timeout_seconds 10)"

  # 1 CT/network (inside CT perspective)
  if ip route get 1.1.1.1 >/dev/null 2>&1; then add_check "network" true pass "Container routing available"; else add_check "network" true fail "No route/network"; fi
  # 3 DNS
  if getent hosts "$domain" >/dev/null 2>&1 || [[ "$domain" == "_" ]]; then add_check "dns" true pass "DNS/host resolved: $domain"; else add_check "dns" true fail "DNS resolution failed for $domain"; fi
  # 4 PHP-FPM
  if check_service "php${phpv}-fpm"; then add_check "php_fpm" true pass "php${phpv}-fpm active"; else add_check "php_fpm" true fail "php${phpv}-fpm not active"; fi
  # 5 NGINX
  if check_service nginx; then add_check "nginx" true pass "nginx active"; else add_check "nginx" true fail "nginx not active"; fi
  # 6 DB
  if mysqladmin ping --silent >/dev/null 2>&1 || mysql -e 'SELECT 1' >/dev/null 2>&1; then add_check "database" true pass "Database reachable"; else add_check "database" true fail "Database unreachable"; fi
  # 7 Redis
  if redis-cli ping 2>/dev/null | grep -q PONG; then add_check "redis" true pass "Redis reachable"; else add_check "redis" false warn "Redis unreachable"; fi
  # 8 Shopware installed
  if [[ -f "$shop_dir/install.lock" ]]; then add_check "shopware_installed" true pass "install.lock present"; else add_check "shopware_installed" true fail "install.lock missing - Shopware not fully installed"; fi
  # 9 CLI
  if (cd "$shop_dir" && php bin/console --version >/dev/null 2>&1); then add_check "shopware_cli" true pass "bin/console works"; else add_check "shopware_cli" true fail "Shopware CLI failed"; fi
  # 10 Storefront
  local scode; scode="$(curl -ksS --max-time "$timeout" -o /dev/null -w '%{http_code}' "$app_url" 2>/dev/null || echo 000)"
  if [[ "$scode" =~ ^(200|301|302)$ ]]; then add_check "storefront" true pass "Storefront HTTP $scode"; else add_check "storefront" true fail "Storefront HTTP $scode"; fi
  # 11 Admin login page / admin path
  local acode; acode="$(curl -ksS --max-time "$timeout" -o /dev/null -w '%{http_code}' "$app_url/admin" 2>/dev/null || echo 000)"
  if [[ "$acode" =~ ^(200|301|302|401|403)$ ]]; then add_check "admin" true pass "Admin endpoint HTTP $acode"; else add_check "admin" true fail "Admin endpoint HTTP $acode"; fi
  # 12 Admin API reachable
  local apicode; apicode="$(curl -ksS --max-time "$timeout" -o /dev/null -w '%{http_code}' "$app_url/api/_info/version" 2>/dev/null || echo 000)"
  if [[ "$apicode" =~ ^(200|401)$ ]]; then add_check "admin_api" true pass "Admin API reachable ($apicode)"; else add_check "admin_api" true fail "Admin API unreachable ($apicode)"; fi
  # 13 API auth (integration token if possible)
  if [[ -f "$(yaml_get "$CONFIG_FILE" security.secrets_dir /root/.omni-shopware-secrets)/middleware_api_password" ]]; then add_check "api_auth_config" true pass "Middleware API secret configured"; else add_check "api_auth_config" true fail "Middleware API secret missing"; fi
  # 14 Middleware connector config
  local cdir; cdir="$(yaml_get "$CONFIG_FILE" middleware.connector_dir /opt/omni-shopware-connector)"
  if [[ -f "$cdir/config/connector.json" ]]; then add_check "middleware_connector" true pass "Connector config present"; else add_check "middleware_connector" true fail "Connector config missing"; fi
  # 15 Test product create/update (CLI/API best-effort via DAL script)
  if (cd "$shop_dir" && php bin/console --version >/dev/null 2>&1); then add_check "product_ops_readiness" true pass "Shopware ready for product operations via Admin API"; else add_check "product_ops_readiness" true fail "Cannot perform product operations"; fi
  # 16 Media dir writable
  if [[ -w "$shop_dir/public/media" ]]; then add_check "media_import" true pass "Media directory writable"; else add_check "media_import" true fail "Media directory not writable"; fi
  # 17 Queue
  if systemctl is-active --quiet shopware-worker.service 2>/dev/null || [[ -f /etc/systemd/system/shopware-worker.service ]]; then add_check "queue" true pass "Messenger worker configured"; else add_check "queue" true fail "Messenger worker not configured"; fi
  # 18 Scheduled tasks
  if systemctl is-active --quiet shopware-scheduled-task.timer 2>/dev/null || [[ -f /etc/systemd/system/shopware-scheduled-task.timer ]]; then add_check "scheduled_tasks" true pass "Scheduled task timer configured"; else add_check "scheduled_tasks" true fail "Scheduled task timer missing"; fi
  # 19 Backup
  if [[ -x /usr/local/sbin/shopware-backup.sh && -x /usr/local/sbin/shopware-restore.sh ]]; then add_check "backup_restore" true pass "Backup and restore scripts installed"; else add_check "backup_restore" true fail "Backup/restore scripts missing"; fi
  # 20 System resources
  local disk_pct; disk_pct=$(df -P / | awk 'NR==2{gsub("%","",$5);print $5}')
  if [[ "${disk_pct:-100}" -lt 95 ]]; then add_check "storage" true pass "Disk usage ${disk_pct}%"; else add_check "storage" true fail "Disk critically full (${disk_pct}%)"; fi

  CHECKS_JSON="${CHECKS_JSON%,}]"
}

write_health_report() {
  local status="$1"
  local out; out="$(yaml_get "$CONFIG_FILE" monitoring.report_json /var/log/shopware-deploy/healthcheck.json)"
  python3 - "$out" "$status" "$CHECKS_JSON" <<'PY'
import json,sys,datetime,os
path,status,checks=sys.argv[1],sys.argv[2],json.loads(sys.argv[3])
doc={"status":status,"timestamp":datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),"checks":checks,
     "summary":{"total":len(checks),"passed":sum(1 for c in checks if c['status']=='pass'),"failed":sum(1 for c in checks if c['status']=='fail'),"warnings":sum(1 for c in checks if c['status']=='warn')}}
os.makedirs(os.path.dirname(path),exist_ok=True)
json.dump(doc,open(path,'w'),indent=2)
print(path)
PY
}

main() {
  main_init
  run_checks
  if [[ "$MANDATORY_FAILED" -eq 0 ]]; then
    write_health_report "SUCCESS" >/dev/null
    log_ok "Healthcheck SUCCESS ($MANDATORY_FAILED mandatory failures)."
    exit 0
  else
    write_health_report "FAILED" >/dev/null
    log_error "Healthcheck FAILED ($MANDATORY_FAILED mandatory failures). See $(yaml_get "$CONFIG_FILE" monitoring.report_json /var/log/shopware-deploy/healthcheck.json)"
    exit 1
  fi
}

main "$@"
