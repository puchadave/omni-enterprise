#!/usr/bin/env bash
# Install Shopware 6, PHP, webserver, database, redis and complete setup inside container.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

main_init() {
  init_paths
  CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/config.active.yaml}"
  [[ -f "$CONFIG_FILE" ]] || CONFIG_FILE="$SCRIPT_DIR/config.yaml"
  init_runtime
  is_root || die "install-shopware.sh must run as root inside target container."
}

apt_install() { DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"; }

resolve_shopware_version() {
  local req; req="$(yaml_get "$CONFIG_FILE" shopware.version latest)"
  if [[ "$req" != "latest" ]]; then echo "$req"; return 0; fi
  local v=""
  v="$(curl -fsSL https://api.github.com/repos/shopware/shopware/releases/latest 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); t=d.get("tag_name","" ); print(t.lstrip("v"))' 2>/dev/null || true)"
  if [[ -z "$v" ]]; then
    v="$(curl -fsSL 'https://repo.packagist.org/p2/shopware/core.json' 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); pk=d.get("packages",{}).get("shopware/core",[]); vers=[x.get("version","" ) for x in pk if x.get("version_normalized") and "dev" not in x.get("version","" )]; print(vers[0].lstrip("v") if vers else "")' 2>/dev/null || true)"
  fi
  [[ -n "$v" ]] || die "Could not resolve latest stable Shopware version. Pin shopware.version in config."
  echo "$v"
}

php_major_minor_for_shopware() {
  local sw="$1"
  # Shopware 6.7+: PHP 8.2/8.3/8.4/8.5 supported; prefer 8.3 for broad extension stability on Debian
  local req; req="$(yaml_get "$CONFIG_FILE" php.version auto)"
  if [[ "$req" != "auto" ]]; then echo "$req"; return 0; fi
  echo "8.3"
}

install_php_stack() {
  local phpv="$1"
  apt_install ca-certificates curl lsb-release apt-transport-https gnupg
  if [[ ! -f /etc/apt/sources.list.d/sury-php.list ]]; then
    curl -fsSL https://packages.sury.org/php/apt.gpg | gpg --dearmor -o /usr/share/keyrings/sury-php.gpg
    echo "deb [signed-by=/usr/share/keyrings/sury-php.gpg] https://packages.sury.org/php/ $(lsb_release -sc) main" > /etc/apt/sources.list.d/sury-php.list
  fi
  apt-get update -qq
  apt_install "php${phpv}-fpm" "php${phpv}-cli" "php${phpv}-common" "php${phpv}-mysql" "php${phpv}-xml" "php${phpv}-gd" \
    "php${phpv}-curl" "php${phpv}-zip" "php${phpv}-intl" "php${phpv}-mbstring" "php${phpv}-opcache" "php${phpv}-redis" \
    "php${phpv}-apcu" "php${phpv}-bcmath" "php${phpv}-soap" "php${phpv}-ldap" || true
  PHP_VERSION="$phpv"
  systemctl enable --now "php${phpv}-fpm" || service "php${phpv}-fpm" start || true
}

install_db_redis_web() {
  local dbtype; dbtype="$(yaml_get "$CONFIG_FILE" database.type mariadb)"
  apt_install nginx redis-server curl unzip git
  if [[ "$dbtype" == "mariadb" ]]; then
    apt_install mariadb-server mariadb-client
    systemctl enable --now mariadb || service mariadb start || true
  else
    apt_install default-mysql-server default-mysql-client
    systemctl enable --now mysql || service mysql start || true
  fi
  systemctl enable --now redis-server || service redis-server start || true
  systemctl enable --now nginx || service nginx start || true
}

install_composer() {
  if command -v composer >/dev/null 2>&1; then return 0; fi
  local EXPECTED CHECKSUM
  EXPECTED="$(curl -fsSL https://composer.github.io/installer.sig)"
  php -r "copy('https://getcomposer.org/installer', 'composer-setup.php');"
  CHECKSUM="$(php -r "echo hash_file('sha384', 'composer-setup.php');")"
  [[ "$EXPECTED" == "$CHECKSUM" ]] || die "Invalid Composer installer checksum"
  php composer-setup.php --install-dir=/usr/local/bin --filename=composer >/dev/null
  rm -f composer-setup.php
}

db_exec() { mysql -uroot -NBe "$1"; }

configure_database() {
  local dbname dbuser dbpass rootpass secrets
  dbname="$(yaml_get "$CONFIG_FILE" database.name shopware)"
  dbuser="$(yaml_get "$CONFIG_FILE" database.user shopware)"
  secrets="$(yaml_get "$CONFIG_FILE" security.secrets_dir /root/.omni-shopware-secrets)"
  dbpass="${SHOPWARE_DB_PASSWORD:-$(yaml_get "$CONFIG_FILE" database.password '')}"
  dbpass="$(ensure_secret db_password "$dbpass" "$secrets")"
  rootpass="$(yaml_get "$CONFIG_FILE" database.root_password '')"
  rootpass="$(ensure_secret db_root_password "$rootpass" "$secrets")"

  mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED BY '${rootpass}'; FLUSH PRIVILEGES;" >/dev/null 2>&1 || true
  mysql -uroot -p"${rootpass}" -e "CREATE DATABASE IF NOT EXISTS \`${dbname}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;" 2>/dev/null || \
    mysql -uroot -e "CREATE DATABASE IF NOT EXISTS \`${dbname}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
  mysql -uroot -p"${rootpass}" -e "CREATE USER IF NOT EXISTS '${dbuser}'@'127.0.0.1' IDENTIFIED BY '${dbpass}'; GRANT ALL PRIVILEGES ON \`${dbname}\`.* TO '${dbuser}'@'127.0.0.1'; CREATE USER IF NOT EXISTS '${dbuser}'@'localhost' IDENTIFIED BY '${dbpass}'; GRANT ALL PRIVILEGES ON \`${dbname}\`.* TO '${dbuser}'@'localhost'; FLUSH PRIVILEGES;" 2>/dev/null || \
    mysql -uroot -e "CREATE USER IF NOT EXISTS '${dbuser}'@'127.0.0.1' IDENTIFIED BY '${dbpass}'; GRANT ALL PRIVILEGES ON \`${dbname}\`.* TO '${dbuser}'@'127.0.0.1'; CREATE USER IF NOT EXISTS '${dbuser}'@'localhost' IDENTIFIED BY '${dbpass}'; GRANT ALL PRIVILEGES ON \`${dbname}\`.* TO '${dbuser}'@'localhost'; FLUSH PRIVILEGES;"
  DB_NAME="$dbname"; DB_USER="$dbuser"; DB_PASS="$dbpass"
}

install_shopware_sources() {
  local ver install_dir
  ver="$1"; install_dir="$(yaml_get "$CONFIG_FILE" shopware.install_dir /var/www/shopware)"
  mkdir -p "$install_dir"
  if [[ -f "$install_dir/composer.json" && -d "$install_dir/vendor" ]]; then
    log_info "Shopware source present at $install_dir; running idempotent update for pinned channel."
  else
    log_info "Creating Shopware production project $ver at $install_dir"
    rm -rf "$install_dir/.gitkeep" 2>/dev/null || true
    if [[ ! -f "$install_dir/composer.json" ]]; then
      COMPOSER_ALLOW_SUPERUSER=1 composer create-project shopware/production:"$ver" "$install_dir" --no-interaction --prefer-dist || \
      COMPOSER_ALLOW_SUPERUSER=1 composer create-project shopware/production "$install_dir" --no-interaction --prefer-dist
    fi
  fi
  cd "$install_dir"
  COMPOSER_ALLOW_SUPERUSER=1 composer install --no-interaction --prefer-dist --optimize-autoloader || true
  chown -R www-data:www-data "$install_dir" || true
  SHOPWARE_DIR="$install_dir"
}

configure_shopware_env() {
  local app_url db_url secrets admin_pass
  app_url="$(yaml_get "$CONFIG_FILE" shopware.app_url https://shop.example.com)"
  secrets="$(yaml_get "$CONFIG_FILE" security.secrets_dir /root/.omni-shopware-secrets)"
  admin_pass="${SHOPWARE_ADMIN_PASSWORD:-$(yaml_get "$CONFIG_FILE" shopware.admin_password '')}"
  admin_pass="$(ensure_secret admin_password "$admin_pass" "$secrets")"
  db_url="mysql://${DB_USER}:${DB_PASS}@127.0.0.1:3306/${DB_NAME}"
  cd "$SHOPWARE_DIR"
  cat > .env.local <<EOF
APP_ENV=$(yaml_get "$CONFIG_FILE" shopware.app_env prod)
APP_URL=${app_url}
DATABASE_URL="${db_url}"
MAILER_DSN=null://null
SHOPWARE_HTTP_CACHE_ENABLED=1
SHOPWARE_HTTP_DEFAULT_TTL=7200
EOF
  chmod 640 .env.local; chown www-data:www-data .env.local || true
  ADMIN_PASS="$admin_pass"
}

run_shopware_install() {
  local shop_name locale currency admin_email admin_user admin_first admin_last
  shop_name="$(yaml_get "$CONFIG_FILE" shopware.shop_name 'Shopware Production')"
  locale="$(yaml_get "$CONFIG_FILE" shopware.shop_locale de-DE)"
  currency="$(yaml_get "$CONFIG_FILE" shopware.shop_currency EUR)"
  admin_email="$(yaml_get "$CONFIG_FILE" shopware.admin_email admin@example.com)"
  admin_user="$(yaml_get "$CONFIG_FILE" shopware.admin_user admin)"
  admin_first="$(yaml_get "$CONFIG_FILE" shopware.admin_firstname Shopware)"
  admin_last="$(yaml_get "$CONFIG_FILE" shopware.admin_lastname Admin)"
  cd "$SHOPWARE_DIR"
  export COMPOSER_ALLOW_SUPERUSER=1
  if [[ ! -f install.lock ]]; then
    log_info "Running Shopware system install..."
    sudo -u www-data php bin/console system:install --create-database=false --drop-database=false --basic-setup \
      --shop-name="$shop_name" --shop-locale="$locale" --shop-currency="$currency" \
      --admin-email="$admin_email" --admin-username="$admin_user" --admin-password="$ADMIN_PASS" \
      --admin-firstname="$admin_first" --admin-lastname="$admin_last" -n || \
    php bin/console system:install --create-database=false --drop-database=false --basic-setup \
      --shop-name="$shop_name" --shop-locale="$locale" --shop-currency="$currency" \
      --admin-email="$admin_email" --admin-username="$admin_user" --admin-password="$ADMIN_PASS" \
      --admin-firstname="$admin_first" --admin-lastname="$admin_last" -n
  else
    log_info "Shopware install.lock present; running schema/update checks."
    php bin/console database:migrate --all -n || true
  fi
  php bin/console cache:clear -n || true
  php bin/console theme:compile -n || true
  php bin/console assets:install -n || true
  php bin/console dal:refresh:index -n || true
  # scheduled tasks registration
  php bin/console scheduled-task:register -n || true
}

configure_nginx() {
  local app_url domain install_dir phpv
  app_url="$(yaml_get "$CONFIG_FILE" shopware.app_url https://shop.example.com)"
  domain="$(echo "$app_url" | awk -F/ '{print $3}')"; domain="${domain:-_}"
  install_dir="$(yaml_get "$CONFIG_FILE" shopware.install_dir /var/www/shopware)"
  phpv="$PHP_VERSION"
  cat > /etc/nginx/sites-available/shopware.conf <<EOF
server {
    listen 80;
    server_name ${domain};
    root ${install_dir}/public;
    client_max_body_size $(yaml_get "$CONFIG_FILE" webserver.client_max_body_size_mb 64)M;

    location / {
        try_files \$uri /index.php\$is_args\$args;
    }

    location ~ ^/(index|shopware-installer\.phar)\.php(/|\$) {
        fastcgi_pass unix:/run/php/php${phpv}-fpm.sock;
        fastcgi_split_path_info ^(.+\.php)(/.*)\$;
        include fastcgi_params;
        fastcgi_param SCRIPT_FILENAME \$realpath_root\$fastcgi_script_name;
        fastcgi_param DOCUMENT_ROOT \$realpath_root;
        fastcgi_param HTTPS \$https if_not_empty;
        fastcgi_read_timeout 300;
        internal;
    }

    location ~* \.(?:css|js|jpg|jpeg|gif|png|svg|ico|webp|woff2?)\$ {
        expires 30d;
        add_header Cache-Control "public";
        try_files \$uri /index.php\$is_args\$args;
    }

    location ~ \.php\$ { return 404; }
}
EOF
  ln -sf /etc/nginx/sites-available/shopware.conf /etc/nginx/sites-enabled/shopware.conf
  rm -f /etc/nginx/sites-enabled/default
  nginx -t
  systemctl reload nginx || service nginx reload
}

configure_workers() {
  local install_dir phpv
  install_dir="$(yaml_get "$CONFIG_FILE" shopware.install_dir /var/www/shopware)"
  phpv="${PHP_VERSION:-8.3}"
  cat > /etc/systemd/system/shopware-worker.service <<EOF
[Unit]
Description=Shopware Messenger Consumer
After=network.target mariadb.service redis-server.service
[Service]
User=www-data
Group=www-data
WorkingDirectory=${install_dir}
ExecStart=/usr/bin/php bin/console messenger:consume async failed --time-limit=60 --memory-limit=512M
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
  cat > /etc/systemd/system/shopware-scheduled-task.service <<EOF
[Unit]
Description=Shopware Scheduled Task Runner
After=network.target mariadb.service
[Service]
Type=oneshot
User=www-data
Group=www-data
WorkingDirectory=${install_dir}
ExecStart=/usr/bin/php bin/console scheduled-task:run --time-limit=55 --memory-limit=512M
EOF
  cat > /etc/systemd/system/shopware-scheduled-task.timer <<EOF
[Unit]
Description=Run Shopware scheduled tasks every minute
[Timer]
OnBootSec=60
OnUnitActiveSec=60
Unit=shopware-scheduled-task.service
[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload || true
  systemctl enable --now shopware-worker.service 2>/dev/null || true
  systemctl enable --now shopware-scheduled-task.timer 2>/dev/null || true
}

main() {
  main_init
  log_info "Resolving Shopware version and PHP compatibility..."
  SW_VERSION="$(resolve_shopware_version)"
  PHP_VERSION="$(php_major_minor_for_shopware "$SW_VERSION")"
  log_info "Selected Shopware=$SW_VERSION PHP=$PHP_VERSION"
  apt-get update -qq
  run_step "install_php" install_php_stack "$PHP_VERSION"
  run_step "install_db_redis_web" install_db_redis_web
  run_step "install_composer" install_composer
  run_step "configure_database" configure_database
  run_step "install_shopware_sources" install_shopware_sources "$SW_VERSION"
  run_step "configure_shopware_env" configure_shopware_env
  run_step "run_shopware_install" run_shopware_install
  run_step "configure_nginx" configure_nginx
  run_step "configure_workers" configure_workers
  log_ok "Shopware base installation completed."
}

main "$@"
