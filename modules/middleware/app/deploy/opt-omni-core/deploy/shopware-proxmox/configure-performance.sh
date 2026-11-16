#!/usr/bin/env bash
# Configure PHP-FPM, OPcache, Redis, MariaDB/MySQL and NGINX tuning based on resources.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

main_init() {
  init_paths
  CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/config.active.yaml}"
  [[ -f "$CONFIG_FILE" ]] || CONFIG_FILE="$SCRIPT_DIR/config.yaml"
  init_runtime
  is_root || die "configure-performance.sh must run as root inside target container."
}

detect_resources() {
  TOTAL_MEM_MB=$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo)
  TOTAL_CPU=$(nproc)
  log_info "Detected resources: ${TOTAL_CPU} vCPU, ${TOTAL_MEM_MB} MB RAM"
}

calc_php() {
  local mem_limit upload post max_exec op_mem op_strings op_files
  mem_limit="$(yaml_get "$CONFIG_FILE" php.memory_limit_mb 512)"
  upload="$(yaml_get "$CONFIG_FILE" php.upload_max_mb 64)"
  post="$(yaml_get "$CONFIG_FILE" php.post_max_mb 64)"
  max_exec="$(yaml_get "$CONFIG_FILE" php.max_execution_time 300)"
  # resource-based adjustments (never overcommit)
  local reserve_mb=$((TOTAL_MEM_MB / 4)); [[ $reserve_mb -lt 1024 ]] && reserve_mb=1024
  local php_budget=$((TOTAL_MEM_MB - reserve_mb - 512)); [[ $php_budget -lt 1024 ]] && php_budget=1024
  # avg child ~120MB
  PHP_MAX_CHILDREN=$((php_budget / 120)); [[ $PHP_MAX_CHILDREN -lt 4 ]] && PHP_MAX_CHILDREN=4; [[ $PHP_MAX_CHILDREN -gt 60 ]] && PHP_MAX_CHILDREN=60
  PHP_START_SERVERS=$((PHP_MAX_CHILDREN / 4)); [[ $PHP_START_SERVERS -lt 2 ]] && PHP_START_SERVERS=2
  PHP_MIN_SPARE=$((PHP_MAX_CHILDREN / 6)); [[ $PHP_MIN_SPARE -lt 1 ]] && PHP_MIN_SPARE=1
  PHP_MAX_SPARE=$((PHP_MAX_CHILDREN / 2)); [[ $PHP_MAX_SPARE -lt 2 ]] && PHP_MAX_SPARE=2
  op_mem="$(yaml_get "$CONFIG_FILE" php.opcache_memory_mb 256)"
  [[ $op_mem -gt $((TOTAL_MEM_MB / 6)) ]] && op_mem=$((TOTAL_MEM_MB / 6))
  [[ $op_mem -lt 128 ]] && op_mem=128
  op_strings="$(yaml_get "$CONFIG_FILE" php.opcache_interned_strings_mb 32)"
  op_files="$(yaml_get "$CONFIG_FILE" php.opcache_max_accelerated_files 20000)"
  PHP_MEM_LIMIT="${mem_limit}M"; PHP_UPLOAD="${upload}M"; PHP_POST="${post}M"; PHP_MAX_EXEC="$max_exec"
  OP_MEM="$op_mem"; OP_STRINGS="$op_strings"; OP_FILES="$op_files"
}

configure_php() {
  local phpv="${PHP_VERSION:-$(ls /etc/php 2>/dev/null | sort -V | tail -n1)}"
  [[ -n "$phpv" ]] || die "No PHP version found in /etc/php"
  local ini="/etc/php/${phpv}/fpm/php.ini" pool="/etc/php/${phpv}/fpm/pool.d/www.conf"
  [[ -f "$ini" ]] || die "PHP ini not found: $ini"
  log_info "Tuning PHP ${phpv} (children=${PHP_MAX_CHILDREN}, mem_limit=${PHP_MEM_LIMIT})"
  crudini_set() { # key value file section(optional)
    python3 - "$@" <<'PY'
import sys,re
key,val,file=sys.argv[1],sys.argv[2],sys.argv[3]
section=sys.argv[4] if len(sys.argv)>4 else None
text=open(file,'r',encoding='utf-8',errors='ignore').read()
pat=re.compile(r'^;?\s*'+re.escape(key)+r'\s*=.*$', re.M)
line=f"{key} = {val}"
if pat.search(text): text=pat.sub(line,text)
else: text += ("\n" if not text.endswith("\n") else "") + line + "\n"
open(file,'w',encoding='utf-8').write(text)
PY
  }
  crudini_set memory_limit "$PHP_MEM_LIMIT" "$ini"
  crudini_set upload_max_filesize "$PHP_UPLOAD" "$ini"
  crudini_set post_max_size "$PHP_POST" "$ini"
  crudini_set max_execution_time "$PHP_MAX_EXEC" "$ini"
  crudini_set max_input_vars 5000 "$ini"
  crudini_set realpath_cache_size 4096K "$ini"
  crudini_set realpath_cache_ttl 600 "$ini"
  crudini_set opcache.enable 1 "$ini"
  crudini_set opcache.enable_cli 1 "$ini"
  crudini_set opcache.memory_consumption "$OP_MEM" "$ini"
  crudini_set opcache.interned_strings_buffer "$OP_STRINGS" "$ini"
  crudini_set opcache.max_accelerated_files "$OP_FILES" "$ini"
  crudini_set opcache.validate_timestamps 0 "$ini"
  crudini_set opcache.save_comments 1 "$ini"
  crudini_set apc.enabled 1 "$ini" || true
  crudini_set apc.shm_size 64M "$ini" || true

  if [[ -f "$pool" ]]; then
    sed -i "s/^pm = .*/pm = dynamic/" "$pool" || true
    sed -i "s/^pm.max_children = .*/pm.max_children = ${PHP_MAX_CHILDREN}/" "$pool" || true
    grep -q '^pm.start_servers' "$pool" && sed -i "s/^pm.start_servers = .*/pm.start_servers = ${PHP_START_SERVERS}/" "$pool" || echo "pm.start_servers = ${PHP_START_SERVERS}" >> "$pool"
    grep -q '^pm.min_spare_servers' "$pool" && sed -i "s/^pm.min_spare_servers = .*/pm.min_spare_servers = ${PHP_MIN_SPARE}/" "$pool" || echo "pm.min_spare_servers = ${PHP_MIN_SPARE}" >> "$pool"
    grep -q '^pm.max_spare_servers' "$pool" && sed -i "s/^pm.max_spare_servers = .*/pm.max_spare_servers = ${PHP_MAX_SPARE}/" "$pool" || echo "pm.max_spare_servers = ${PHP_MAX_SPARE}" >> "$pool"
    grep -q '^pm.max_requests' "$pool" && sed -i "s/^pm.max_requests = .*/pm.max_requests = 1000/" "$pool" || echo "pm.max_requests = 1000" >> "$pool"
  fi
  systemctl restart "php${phpv}-fpm" 2>/dev/null || service "php${phpv}-fpm" restart || true
}

configure_db() {
  local innodb_buffer innodb_log max_conn
  innodb_buffer=$((TOTAL_MEM_MB * 35 / 100)); [[ $innodb_buffer -lt 256 ]] && innodb_buffer=256; [[ $innodb_buffer -gt 4096 ]] && innodb_buffer=4096
  innodb_log=$((innodb_buffer / 4)); [[ $innodb_log -lt 128 ]] && innodb_log=128
  max_conn=$((TOTAL_CPU * 25 + 50)); [[ $max_conn -lt 100 ]] && max_conn=100; [[ $max_conn -gt 300 ]] && max_conn=300
  local cnf="/etc/mysql/mariadb.conf.d/99-shopware.cnf"
  mkdir -p "$(dirname "$cnf")"
  cat > "$cnf" <<EOF
[mysqld]
innodb_buffer_pool_size = ${innodb_buffer}M
innodb_log_file_size = ${innodb_log}M
innodb_flush_log_at_trx_commit = 2
innodb_flush_method = O_DIRECT
max_connections = ${max_conn}
max_allowed_packet = 64M
tmp_table_size = 64M
max_heap_table_size = 64M
table_open_cache = 4000
thread_cache_size = 50
slow_query_log = 1
slow_query_log_file = /var/log/mysql/slow.log
long_query_time = 2
EOF
  systemctl restart mariadb 2>/dev/null || systemctl restart mysql 2>/dev/null || service mariadb restart 2>/dev/null || true
  log_info "DB tuned: innodb_buffer_pool_size=${innodb_buffer}M max_connections=${max_conn}"
}

configure_redis() {
  local maxmem policy
  maxmem=$((TOTAL_MEM_MB * 15 / 100)); [[ $maxmem -lt 128 ]] && maxmem=128; [[ $maxmem -gt 1024 ]] && maxmem=1024
  policy="allkeys-lru"
  local conf="/etc/redis/redis.conf"
  if [[ -f "$conf" ]]; then
    sed -i "s/^#* *maxmemory .*/maxmemory ${maxmem}mb/" "$conf" || echo "maxmemory ${maxmem}mb" >> "$conf"
    sed -i "s/^#* *maxmemory-policy .*/maxmemory-policy ${policy}/" "$conf" || echo "maxmemory-policy ${policy}" >> "$conf"
    sed -i "s/^#* *appendonly .*/appendonly yes/" "$conf" || echo "appendonly yes" >> "$conf"
    systemctl restart redis-server 2>/dev/null || service redis-server restart || true
    log_info "Redis tuned: maxmemory=${maxmem}mb policy=${policy}"
  fi
}

configure_nginx_tuning() {
  local conf="/etc/nginx/conf.d/shopware-tuning.conf"
  cat > "$conf" <<EOF
sendfile on;
tcp_nopush on;
tcp_nodelay on;
keepalive_timeout 65;
types_hash_max_size 2048;
server_tokens off;
gzip on;
gzip_vary on;
gzip_proxied any;
gzip_comp_level 6;
gzip_types text/plain text/css text/xml application/json application/javascript application/xml+rss application/atom+xml image/svg+xml;
fastcgi_buffers 16 16k;
fastcgi_buffer_size 32k;
fastcgi_read_timeout 300;
EOF
  nginx -t && (systemctl reload nginx 2>/dev/null || service nginx reload || true)
}

main() {
  main_init
  detect_resources
  calc_php
  run_step "tune_php" configure_php
  run_step "tune_database" configure_db
  run_step "tune_redis" configure_redis
  run_step "tune_nginx" configure_nginx_tuning
  log_ok "Performance tuning complete."
}

main "$@"
