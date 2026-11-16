#!/usr/bin/env bash
# Automated backup system for Shopware (files, config, DB, media, plugins, connector).
# Backups are stored outside the container filesystem path when mounted to host storage.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

main_init() {
  init_paths
  CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/config.active.yaml}"
  [[ -f "$CONFIG_FILE" ]] || CONFIG_FILE="$SCRIPT_DIR/config.yaml"
  init_runtime
  is_root || die "setup-backups.sh must run as root inside target container."
}

prepare_backup_env() {
  local enabled target retention
  enabled="$(yaml_get "$CONFIG_FILE" backup.enabled true)"
  [[ "$enabled" == "true" ]] || { log_warn "Backups disabled in config"; exit 0; }
  target="$(yaml_get "$CONFIG_FILE" backup.target_dir /var/backups/shopware)"
  retention="$(yaml_get "$CONFIG_FILE" backup.retention_days 14)"
  mkdir -p "$target"
  # Warn if target appears to be only on container rootfs (best-effort)
  if ! findmnt -rn -T "$target" >/dev/null 2>&1; then
    log_warn "Backup target $target is not a separate mount. For production, mount host/NFS storage outside container rootfs."
  fi
  BACKUP_TARGET="$target"; RETENTION_DAYS="$retention"
}

write_backup_script() {
  local shop_dir dbname dbuser secrets dbpass connector_dir
  shop_dir="$(yaml_get "$CONFIG_FILE" shopware.install_dir /var/www/shopware)"
  dbname="$(yaml_get "$CONFIG_FILE" database.name shopware)"
  dbuser="$(yaml_get "$CONFIG_FILE" database.user shopware)"
  secrets="$(yaml_get "$CONFIG_FILE" security.secrets_dir /root/.omni-shopware-secrets)"
  dbpass="$(cat "$secrets/db_password" 2>/dev/null || echo "")"
  connector_dir="$(yaml_get "$CONFIG_FILE" middleware.connector_dir /opt/omni-shopware-connector)"
  cat > /usr/local/sbin/shopware-backup.sh <<'EOS'
#!/usr/bin/env bash
set -Eeuo pipefail
TS="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="__TARGET__"
SHOP_DIR="__SHOP_DIR__"
DB_NAME="__DB_NAME__"
DB_USER="__DB_USER__"
DB_PASS="__DB_PASS__"
CONNECTOR_DIR="__CONNECTOR_DIR__"
RETENTION="__RETENTION__"
WORK="$TARGET/.work-$TS"
OUT="$TARGET/shopware-backup-$TS.tar.zst"
mkdir -p "$WORK" "$TARGET"
cleanup(){ rm -rf "$WORK"; }
trap cleanup EXIT

# DB dump
MYSQL_PWD="$DB_PASS" mysqldump -u"$DB_USER" --single-transaction --quick --lock-tables=false "$DB_NAME" > "$WORK/database.sql"

# Files/config/media/plugins/themes/connector
mkdir -p "$WORK/files"
tar -C "$SHOP_DIR" -cf "$WORK/files/shopware-files.tar" \
  --exclude='var/cache/*' --exclude='var/log/*' \
  config custom/plugins custom/static-plugins public/media public/thumbnail files .env .env.local 2>/dev/null || true
if [[ -d "$CONNECTOR_DIR" ]]; then
  tar -C "$(dirname "$CONNECTOR_DIR")" -cf "$WORK/files/connector.tar" "$(basename "$CONNECTOR_DIR")" 2>/dev/null || true
fi

# Manifest and checksums
(cd "$WORK" && find . -type f -print0 | sort -z | xargs -0 sha256sum > manifest.sha256)
tar -C "$WORK" -cf - . | zstd -q -T0 -19 -o "$OUT"
sha256sum "$OUT" > "$OUT.sha256"

# Integrity check
zstd -tq "$OUT"
echo "$TS BACKUP_OK $OUT" >> "$TARGET/backup.log"

# Rotation
find "$TARGET" -maxdepth 1 -type f -name 'shopware-backup-*.tar.zst' -mtime +"$RETENTION" -delete -print >> "$TARGET/backup.log" 2>&1 || true
find "$TARGET" -maxdepth 1 -type f -name 'shopware-backup-*.tar.zst.sha256' -mtime +"$RETENTION" -delete 2>/dev/null || true
EOS
  sed -i "s|__TARGET__|$BACKUP_TARGET|g; s|__SHOP_DIR__|$shop_dir|g; s|__DB_NAME__|$dbname|g; s|__DB_USER__|$dbuser|g; s|__DB_PASS__|$dbpass|g; s|__CONNECTOR_DIR__|$connector_dir|g; s|__RETENTION__|$RETENTION_DAYS|g" /usr/local/sbin/shopware-backup.sh
  chmod 700 /usr/local/sbin/shopware-backup.sh
}

write_restore_script() {
  local shop_dir dbname dbuser secrets dbpass
  shop_dir="$(yaml_get "$CONFIG_FILE" shopware.install_dir /var/www/shopware)"
  dbname="$(yaml_get "$CONFIG_FILE" database.name shopware)"
  dbuser="$(yaml_get "$CONFIG_FILE" database.user shopware)"
  secrets="$(yaml_get "$CONFIG_FILE" security.secrets_dir /root/.omni-shopware-secrets)"
  dbpass="$(cat "$secrets/db_password" 2>/dev/null || echo "")"
  cat > /usr/local/sbin/shopware-restore.sh <<'EOS'
#!/usr/bin/env bash
set -Eeuo pipefail
ARCHIVE="${1:-}"
TARGET_DB="${2:-__DB_NAME__}"
SHOP_DIR="__SHOP_DIR__"
DB_USER="__DB_USER__"
DB_PASS="__DB_PASS__"
[[ -n "$ARCHIVE" && -f "$ARCHIVE" ]] || { echo "Usage: $0 <backup.tar.zst> [target_database]"; exit 2; }
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
echo "Verifying archive..."
zstd -tq "$ARCHIVE"
sha256sum -c "$ARCHIVE.sha256" 2>/dev/null || echo "WARN: checksum file missing or failed"
tar -I zstd -xf "$ARCHIVE" -C "$TMP"
(cd "$TMP" && sha256sum -c manifest.sha256)

read -r -p "Restore DB to '$TARGET_DB' and files to '$SHOP_DIR'? Type YES to continue: " confirm
[[ "$confirm" == "YES" ]] || { echo "Aborted."; exit 1; }

echo "Restoring database..."
MYSQL_PWD="$DB_PASS" mysql -u"$DB_USER" -e "DROP DATABASE IF EXISTS \`$TARGET_DB\`; CREATE DATABASE \`$TARGET_DB\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
MYSQL_PWD="$DB_PASS" mysql -u"$DB_USER" "$TARGET_DB" < "$TMP/database.sql"

echo "Restoring files..."
mkdir -p "$SHOP_DIR"
tar -xf "$TMP/files/shopware-files.tar" -C "$SHOP_DIR" || true
if [[ -f "$TMP/files/connector.tar" ]]; then tar -xf "$TMP/files/connector.tar" -C /opt || true; fi
chown -R www-data:www-data "$SHOP_DIR" || true
cd "$SHOP_DIR" && php bin/console cache:clear -n || true
echo "Restore completed. Run healthcheck.sh for validation."
EOS
  sed -i "s|__SHOP_DIR__|$shop_dir|g; s|__DB_NAME__|$dbname|g; s|__DB_USER__|$dbuser|g; s|__DB_PASS__|$dbpass|g" /usr/local/sbin/shopware-restore.sh
  chmod 700 /usr/local/sbin/shopware-restore.sh
}

schedule_backups() {
  local cron; cron="$(yaml_get "$CONFIG_FILE" backup.schedule_cron '30 2 * * *')"
  local line="$cron root /usr/local/sbin/shopware-backup.sh >> $(yaml_get "$CONFIG_FILE" deployment.log_dir /var/log/shopware-deploy)/backup-cron.log 2>&1"
  echo "$line" > /etc/cron.d/shopware-backup
  chmod 644 /etc/cron.d/shopware-backup
  systemctl reload cron 2>/dev/null || service cron reload 2>/dev/null || true
  log_info "Backup cron installed: $cron"
}

run_restore_test_if_enabled() {
  local enabled; enabled="$(yaml_get "$CONFIG_FILE" backup.test_restore false)"
  [[ "$enabled" == "true" ]] || return 0
  log_info "Running isolated restore test..."
  /usr/local/sbin/shopware-backup.sh || die "Backup for restore test failed"
  local latest; latest="$(ls -1t "$BACKUP_TARGET"/shopware-backup-*.tar.zst 2>/dev/null | head -n1 || true)"
  [[ -n "$latest" ]] || die "No backup archive found for restore test"
  local tmpdb="shopware_restore_test_$(date +%s)"
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  tar -I zstd -xf "$latest" -C "$TMP"
  local dbuser dbpass secrets; secrets="$(yaml_get "$CONFIG_FILE" security.secrets_dir /root/.omni-shopware-secrets)"
  dbuser="$(yaml_get "$CONFIG_FILE" database.user shopware)"; dbpass="$(cat "$secrets/db_password")"
  MYSQL_PWD="$dbpass" mysql -u"$dbuser" -e "CREATE DATABASE \`$tmpdb\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
  MYSQL_PWD="$dbpass" mysql -u"$dbuser" "$tmpdb" < "$TMP/database.sql"
  MYSQL_PWD="$dbpass" mysql -u"$dbuser" -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='$tmpdb';" >/dev/null
  MYSQL_PWD="$dbpass" mysql -u"$dbuser" -e "DROP DATABASE \`$tmpdb\`;"
  log_ok "Restore test passed (isolated DB)."
}

main() {
  main_init
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq zstd cron mysql-client mariadb-client >/dev/null 2>&1 || true
  run_step "prepare_backup_env" prepare_backup_env
  run_step "write_backup_script" write_backup_script
  run_step "write_restore_script" write_restore_script
  run_step "schedule_backups" schedule_backups
  run_step "restore_test" run_restore_test_if_enabled
  log_ok "Backup and recovery system configured. Backup script: /usr/local/sbin/shopware-backup.sh Restore: /usr/local/sbin/shopware-restore.sh"
}

main "$@"
