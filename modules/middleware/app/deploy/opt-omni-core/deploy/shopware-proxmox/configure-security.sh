#!/usr/bin/env bash
# Security hardening: firewall, SSH, TLS/SSL, secrets handling, permissions.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

main_init() {
  init_paths
  CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/config.active.yaml}"
  [[ -f "$CONFIG_FILE" ]] || CONFIG_FILE="$SCRIPT_DIR/config.yaml"
  init_runtime
  is_root || die "configure-security.sh must run as root inside target container."
}

configure_firewall() {
  local enabled; enabled="$(yaml_get "$CONFIG_FILE" security.firewall_enabled true)"
  [[ "$enabled" == "true" ]] || { log_warn "Firewall disabled in config"; return 0; }
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw || true
  if command -v ufw >/dev/null 2>&1; then
    ufw --force reset >/dev/null 2>&1 || true
    ufw default deny incoming
    ufw default allow outgoing
    [[ "$(yaml_get "$CONFIG_FILE" security.allow_ssh true)" == "true" ]] && ufw allow "$(yaml_get "$CONFIG_FILE" security.ssh_port 22)/tcp"
    [[ "$(yaml_get "$CONFIG_FILE" security.allow_http true)" == "true" ]] && ufw allow 80/tcp
    [[ "$(yaml_get "$CONFIG_FILE" security.allow_https true)" == "true" ]] && ufw allow 443/tcp
    ufw --force enable
    log_info "UFW firewall configured."
  fi
}

harden_ssh() {
  local cfg="/etc/ssh/sshd_config"
  [[ -f "$cfg" ]] || return 0
  local pw_auth root_login port
  pw_auth="$(yaml_get "$CONFIG_FILE" security.ssh_password_auth false)"
  root_login="$(yaml_get "$CONFIG_FILE" security.ssh_permit_root_login prohibit-password)"
  port="$(yaml_get "$CONFIG_FILE" security.ssh_port 22)"
  sed -i "s/^#*Port .*/Port ${port}/" "$cfg" || echo "Port ${port}" >> "$cfg"
  local pwv="no"; [[ "$pw_auth" == "true" ]] && pwv="yes"
  grep -q '^PasswordAuthentication' "$cfg" && sed -i "s/^PasswordAuthentication.*/PasswordAuthentication ${pwv}/" "$cfg" || echo "PasswordAuthentication ${pwv}" >> "$cfg"
  grep -q '^PermitRootLogin' "$cfg" && sed -i "s/^PermitRootLogin.*/PermitRootLogin ${root_login}/" "$cfg" || echo "PermitRootLogin ${root_login}" >> "$cfg"
  grep -q '^X11Forwarding' "$cfg" && sed -i "s/^X11Forwarding.*/X11Forwarding no/" "$cfg" || echo "X11Forwarding no" >> "$cfg"
  systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || service ssh reload 2>/dev/null || true
  log_info "SSH hardened (password_auth=${pwv}, root_login=${root_login})."
}

configure_ssl() {
  local enabled mode domain email staging app_url secrets
  enabled="$(yaml_get "$CONFIG_FILE" ssl.enabled true)"
  mode="$(yaml_get "$CONFIG_FILE" ssl.mode letsencrypt)"
  app_url="$(yaml_get "$CONFIG_FILE" shopware.app_url https://shop.example.com)"
  domain="$(echo "$app_url" | awk -F/ '{print $3}')"
  email="$(yaml_get "$CONFIG_FILE" ssl.email admin@example.com)"
  staging="$(yaml_get "$CONFIG_FILE" ssl.staging false)"
  secrets="$(yaml_get "$CONFIG_FILE" security.secrets_dir /root/.omni-shopware-secrets)"

  [[ "$enabled" == "true" ]] || { log_warn "SSL disabled in config"; return 0; }

  case "$mode" in
    letsencrypt)
      [[ -n "$domain" && "$domain" != "_" ]] || die "Valid domain required in shopware.app_url for Let's Encrypt."
      # DNS/reachability check - must not report false success
      local dns_ok=false
      if getent hosts "$domain" >/dev/null 2>&1; then dns_ok=true; fi
      if [[ "$dns_ok" != "true" ]]; then
        if [[ "$(yaml_get "$CONFIG_FILE" deployment.require_dns_for_ssl true)" == "true" ]]; then
          die "DNS for $domain not resolvable. Cannot issue Let's Encrypt certificate; set ssl.mode=existing_reverse_proxy/selfsigned/disabled or fix DNS." 76
        else
          log_warn "DNS for $domain not resolvable; skipping cert issuance."
          return 0
        fi
      fi
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq certbot python3-certbot-nginx || die "Failed to install certbot"
      local args=(--nginx -d "$domain" --non-interactive --agree-tos -m "$email" --redirect)
      [[ "$staging" == "true" ]] && args+=(--staging)
      if ! certbot "${args[@]}"; then
        die "Certbot failed for $domain. Deployment cannot report SSL success." 77
      fi
      systemctl enable --now certbot.timer 2>/dev/null || true
      log_ok "Let's Encrypt certificate issued for $domain."
      ;;
    existing_reverse_proxy)
      log_info "SSL mode existing_reverse_proxy: container will serve HTTP; trusting proxy headers."
      local conf="/etc/nginx/conf.d/proxy-trust.conf"
      {
        echo "set_real_ip_from 10.0.0.0/8;"
        echo "set_real_ip_from 172.16.0.0/12;"
        echo "set_real_ip_from 192.168.0.0/16;"
        echo "real_ip_header X-Forwarded-For;"
      } > "$conf"
      nginx -t && systemctl reload nginx || true
      ;;
    selfsigned)
      mkdir -p /etc/ssl/shopware
      if [[ ! -f /etc/ssl/shopware/selfsigned.crt ]]; then
        openssl req -x509 -nodes -days 365 -newkey rsa:2048 -keyout /etc/ssl/shopware/selfsigned.key -out /etc/ssl/shopware/selfsigned.crt -subj "/CN=${domain}"
      fi
      log_warn "Self-signed certificate installed (not trusted)."
      ;;
    disabled)
      log_warn "SSL explicitly disabled.";;
    *) die "Unknown ssl.mode: $mode";;
  esac
}

configure_fail2ban_updates() {
  if [[ "$(yaml_get "$CONFIG_FILE" security.fail2ban_enabled true)" == "true" ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq fail2ban || true
    systemctl enable --now fail2ban 2>/dev/null || true
  fi
  if [[ "$(yaml_get "$CONFIG_FILE" security.unattended_upgrades true)" == "true" ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq unattended-upgrades || true
  fi
}

secure_permissions() {
  local dir; dir="$(yaml_get "$CONFIG_FILE" shopware.install_dir /var/www/shopware)"
  chown -R www-data:www-data "$dir" || true
  find "$dir" -type f -exec chmod 640 {} \; 2>/dev/null || true
  find "$dir" -type d -exec chmod 750 {} \; 2>/dev/null || true
  chmod -R 770 "$dir/var" "$dir/public/media" "$dir/public/thumbnail" "$dir/public/sitemap" 2>/dev/null || true
  local secrets; secrets="$(yaml_get "$CONFIG_FILE" security.secrets_dir /root/.omni-shopware-secrets)"
  mkdir -p "$secrets"; chmod 700 "$secrets"
}

main() {
  main_init
  run_step "firewall" configure_firewall
  run_step "ssh_hardening" harden_ssh
  run_step "ssl" configure_ssl
  run_step "fail2ban_updates" configure_fail2ban_updates
  run_step "permissions" secure_permissions
  log_ok "Security configuration complete."
}

main "$@"
