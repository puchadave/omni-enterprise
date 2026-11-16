#!/usr/bin/env bash
# Create and prepare Proxmox LXC container for Shopware.
# Runs on Proxmox host in pct mode; API mode stubbed with validation.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

DRY_RUN=false
FORCE=false

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config) CONFIG_FILE="$2"; shift 2;;
      --dry-run) DRY_RUN=true; shift;;
      --force) FORCE=true; shift;;
      *) die "Unknown argument: $1";;
    esac
  done
}

detect_storage() {
  local requested; requested="$(yaml_get "$CONFIG_FILE" proxmox.storage '')"
  if [[ -n "$requested" ]]; then echo "$requested"; return 0; fi
  if command -v pvesm >/dev/null 2>&1; then
    pvesm status -content rootdir 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}' && return 0
    pvesm status -content images 2>/dev/null | awk 'NR>1 && $3=="active"{print $1; exit}' && return 0
  fi
  echo "local-lvm"
}

select_template() {
  local requested; requested="$(yaml_get "$CONFIG_FILE" container.ostemplate '')"
  local tstore; tstore="$(yaml_get "$CONFIG_FILE" proxmox.template_storage local)"
  if [[ -n "$requested" ]]; then echo "$requested"; return 0; fi
  pveam update >/dev/null 2>&1 || true
  local host_arch tmpl
  case "$(uname -m 2>/dev/null || echo x86_64)" in
    x86_64|amd64) host_arch="amd64" ;;
    aarch64|arm64) host_arch="arm64" ;;
    *) host_arch="amd64" ;;
  esac
  host_arch="${TEMPLATE_ARCH:-${LXC_ARCH:-$host_arch}}"
  log_info "Detected host architecture: $host_arch"
  tmpl="$(pveam available -section system 2>/dev/null | awk -v a="_${host_arch}" '/debian-13-standard/ && index($2,a)>0 {print $2}' | tail -n1 || true)"
  if [[ -z "$tmpl" ]]; then
    tmpl="$(pveam list "$tstore" 2>/dev/null | awk -v a="_${host_arch}" '/debian-13-standard/ && index($0,a)>0 {print $1}' | sed 's#.*vztmpl/##' | tail -n1 || true)"
  fi
  [[ -n "$tmpl" ]] || die "Could not find Debian 13 LXC template for ${host_arch} via pveam. Set container.ostemplate explicitly."
  if [[ "$host_arch" == "amd64" && "$tmpl" == *"_arm64"* ]]; then die "Selected wrong-arch template ($tmpl) for amd64"; fi
  if [[ "$host_arch" == "arm64" && "$tmpl" == *"_amd64"* ]]; then die "Selected wrong-arch template ($tmpl) for arm64"; fi
  local vol="${tstore}:vztmpl/${tmpl}"
  if ! pveam list "$tstore" 2>/dev/null | grep -q "$tmpl"; then
    log_info "Downloading template $tmpl to $tstore..."
    [[ "$DRY_RUN" == "true" ]] || pveam download "$tstore" "$tmpl" >/dev/null
  fi
  echo "$vol"
}

ensure_container() {
  local ctid cores mem swap disk ip gw dns host bridge storage tmpl unpriv features onboot
  ctid="$(yaml_get "$CONFIG_FILE" container.ctid 210)"
  cores="$(yaml_get "$CONFIG_FILE" container.cores 4)"
  mem="$(yaml_get "$CONFIG_FILE" container.memory_mb 8192)"
  swap="$(yaml_get "$CONFIG_FILE" container.swap_mb 2048)"
  disk="$(yaml_get "$CONFIG_FILE" container.disk_gb 64)"
  ip="$(yaml_get "$CONFIG_FILE" container.ip dhcp)"
  gw="$(yaml_get "$CONFIG_FILE" container.gateway '')"
  dns="$(yaml_get "$CONFIG_FILE" container.dns '1.1.1.1 8.8.8.8')"
  host="$(yaml_get "$CONFIG_FILE" container.hostname shopware-prod)"
  bridge="$(yaml_get "$CONFIG_FILE" proxmox.bridge vmbr0)"
  unpriv="$(yaml_get "$CONFIG_FILE" proxmox.unprivileged true)"
  features="$(yaml_get "$CONFIG_FILE" container.features 'nesting=1')"
  onboot="$(yaml_get "$CONFIG_FILE" container.onboot true)"
  storage="$(detect_storage)"
  tmpl="$(select_template)"

  log_info "Using storage=$storage template=$tmpl"

  if pct status "$ctid" >/dev/null 2>&1; then
    log_warn "CT $ctid exists; applying idempotent reconfiguration."
  else
    local net0="name=eth0,bridge=${bridge},ip=${ip}"
    [[ "$ip" != "dhcp" && -n "$gw" ]] && net0+=",gw=${gw}"
    local cmd=(pct create "$ctid" "$tmpl" --hostname "$host" --cores "$cores" --memory "$mem" --swap "$swap" \
      --rootfs "${storage}:${disk}" --net0 "$net0" --nameserver "$dns" --ostype debian --features "$features")
    [[ "$unpriv" == "true" ]] && cmd+=(--unprivileged 1) || cmd+=(--unprivileged 0)
    [[ "$onboot" == "true" ]] && cmd+=(--onboot 1)
    log_info "Creating container: ${cmd[*]}"
    if [[ "$DRY_RUN" != "true" ]]; then
      local out
      if ! out=$("${cmd[@]}" 2>&1); then
        log_error "pct create failed for CT $ctid"
        log_error "$out"
        log_error "Hints: verify storage '$storage' supports rootdir, template '$tmpl' exists, bridge '$bridge' exists, CTID $ctid free."
        log_error "Storages: $(pvesm status 2>/dev/null | awk 'NR==1 || $3=="active"{print}' | tr '
' ' ')"
        die "Container creation failed" 21
      fi
    fi
  fi

  # reconfigure core resources (idempotent)
  if [[ "$DRY_RUN" != "true" ]]; then
    pct set "$ctid" --hostname "$host" --cores "$cores" --memory "$mem" --swap "$swap" --nameserver "$dns" --features "$features" >/dev/null || true
    [[ "$onboot" == "true" ]] && pct set "$ctid" --onboot 1 >/dev/null || true
    # resize rootfs if larger requested (grow only)
    pct resize "$ctid" rootfs "${disk}G" >/dev/null 2>&1 || true
  fi
}

start_container() {
  local ctid; ctid="$(yaml_get "$CONFIG_FILE" container.ctid 210)"
  if [[ "$DRY_RUN" == "true" ]]; then log_info "[dry-run] would start CT $ctid"; return 0; fi
  if ! pct status "$ctid" 2>/dev/null | grep -q running; then
    local out
    if ! out=$(pct start "$ctid" 2>&1); then
      log_warn "pct start failed: $out"
      if echo "$out" | grep -qi "Failed to spawn\|sync_wait"; then
        log_warn "Retrying start without features (nesting/keyctl) compatibility fallback..."
        pct set "$ctid" --features "" >/dev/null 2>&1 || true
        out=$(pct start "$ctid" 2>&1) || { log_error "$out"; die "Container $ctid failed to start. Check storage supports rootdir, unprivileged userns/AppArmor, and run 'pct start $ctid' manually." 22; }
      else
        log_error "$out"
        die "Container $ctid failed to start" 22
      fi
    fi
  fi
  log_info "Waiting for container networking..."
  local i
  for i in $(seq 1 60); do
    if pct exec "$ctid" -- bash -lc 'getent hosts deb.debian.org >/dev/null 2>&1 || ping -c1 -W1 1.1.1.1 >/dev/null 2>&1' >/dev/null 2>&1; then
      log_ok "Container network ready"; return 0
    fi
    sleep 2
  done
  log_warn "Network readiness check timed out; continuing (DNS may be configured later)."
}

bootstrap_os() {
  local ctid; ctid="$(yaml_get "$CONFIG_FILE" container.ctid 210)"
  [[ "$DRY_RUN" == "true" ]] && return 0
  log_info "Bootstrapping base OS packages in container..."
  safe_pct_exec "$ctid" "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq; apt-get install -y -qq ca-certificates curl wget gnupg lsb-release apt-transport-https sudo git jq unzip openssl cron logrotate python3 python3-yaml locales systemd-timesyncd"
  safe_pct_exec "$ctid" "sed -i 's/^# en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen || true; sed -i 's/^# de_DE.UTF-8/de_DE.UTF-8/' /etc/locale.gen || true; locale-gen || true; update-locale LANG=en_US.UTF-8 || true"
}

main() {
  init_paths
  parse_args "$@"
  init_runtime
  local mode; mode="$(yaml_get "$CONFIG_FILE" proxmox.mode auto)"
  [[ "$mode" == "auto" ]] && { if is_proxmox_host; then mode=pct; else mode=api; fi; }
  [[ "$mode" == "pct" ]] || die "install-container.sh currently supports pct mode on Proxmox host."
  is_root || die "install-container.sh must run as root."

  run_step "select_template" select_template >/dev/null
  run_step "ensure_container" ensure_container
  run_step "start_container" start_container
  run_step "bootstrap_os" bootstrap_os
  log_ok "Container provisioning complete (CT $(yaml_get "$CONFIG_FILE" container.ctid 210))."
}

main "$@"
