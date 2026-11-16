#!/usr/bin/env bash
# One-click Shopware 6 production deployment for Proxmox VE (LXC).
# Orchestrates container provisioning, installation, hardening, middleware prep,
# backups, monitoring and acceptance tests.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: deploy-shopware.sh [--config FILE] [--dry-run] [--skip-tests] [--force] [--help]

Options:
  --config FILE   Path to config.yaml (default: ./config.yaml)
  --dry-run       Validate and print planned actions without changes
  --skip-tests    Skip post-install acceptance tests (deployment cannot be SUCCESS)
  --force         Proceed even if target CTID exists (idempotent reconfigure path)
  -h, --help      Show help

Environment:
  PROXMOX_API_TOKEN_SECRET  API token secret for Proxmox API mode
  SHOPWARE_ADMIN_PASSWORD   Override admin password (stored securely, not logged)
  SHOPWARE_DB_PASSWORD      Override DB password
EOF
}

DRY_RUN=false
SKIP_TESTS=false
FORCE=false

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config) CONFIG_FILE="$2"; shift 2;;
      --dry-run) DRY_RUN=true; shift;;
      --skip-tests) SKIP_TESTS=true; shift;;
      --force) FORCE=true; shift;;
      -h|--help) usage; exit 0;;
      *) die "Unknown argument: $1";;
    esac
  done
}

preflight() {
  require_cmd bash
  require_cmd python3
  require_cmd curl
  is_root || die "Run as root on Proxmox host (or privileged management node with API mode)."
  init_runtime
  acquire_lock

  local mode; mode="$(yaml_get "$CONFIG_FILE" proxmox.mode auto)"
  if [[ "$mode" == "auto" ]]; then
    if is_proxmox_host; then mode="pct"; else mode="api"; fi
  fi
  PROXMOX_MODE="$mode"
  log_info "Proxmox mode: $PROXMOX_MODE"

  case "$PROXMOX_MODE" in
    pct)
      require_cmd pct
      require_cmd pveam
      ;;
    api)
      require_cmd curl
      [[ -n "$(yaml_get "$CONFIG_FILE" proxmox.api_url '')" ]] || die "proxmox.api_url required for API mode"
      [[ -n "$(yaml_get "$CONFIG_FILE" proxmox.api_token_id '')" ]] || die "proxmox.api_token_id required for API mode"
      [[ -n "${PROXMOX_API_TOKEN_SECRET:-$(yaml_get "$CONFIG_FILE" proxmox.api_token_secret '')}" ]] || die "API token secret required (PROXMOX_API_TOKEN_SECRET or config)"
      ;;
    *) die "Unsupported proxmox.mode: $PROXMOX_MODE";;
  esac

  local ctid; ctid="$(yaml_get "$CONFIG_FILE" container.ctid 210)"
  CTID="$ctid"
  if [[ "$PROXMOX_MODE" == "pct" ]] && pct status "$CTID" >/dev/null 2>&1; then
    if [[ "$FORCE" == "true" ]]; then
      log_warn "Container $CTID exists; continuing in idempotent reconfigure mode (--force)."
      EXISTING_CT=true
    else
      die "Container CTID $CTID already exists. Use --force to reconfigure or choose another CTID." 17
    fi
  else
    EXISTING_CT=false
  fi

  validate_network_dns
}

validate_network_dns() {
  local ip gw bridge
  ip="$(yaml_get "$CONFIG_FILE" container.ip dhcp)"
  gw="$(yaml_get "$CONFIG_FILE" container.gateway '')"
  bridge="$(yaml_get "$CONFIG_FILE" proxmox.bridge vmbr0)"
  if [[ "$PROXMOX_MODE" == "pct" && "$ip" != "dhcp" && -z "$gw" ]]; then
    die "Static container IP requires container.gateway in config."
  fi
  if [[ "$PROXMOX_MODE" == "pct" && ! -d "/sys/class/net/$bridge" && "$EXISTING_CT" != "true" ]]; then
    die "Network bridge not found on host: $bridge"
  fi
  if ! getent hosts github.com >/dev/null 2>&1; then
    log_warn "DNS/host resolution check for github.com failed on host; container install may fail later."
  fi
}

copy_payload_into_container() {
  local target="/opt/shopware-deploy"
  log_info "Copying deployment payload into CT $CTID:$target"
  if [[ "$DRY_RUN" == "true" ]]; then return 0; fi
  safe_pct_exec "$CTID" "mkdir -p '$target'"
  local tmp; tmp="$(mktemp /tmp/swdeploy.XXXXXX.tar)"
  tar -C "$SCRIPT_DIR" -cf "$tmp" .
  pct push "$CTID" "$tmp" "$target/payload.tar" >/dev/null
  rm -f "$tmp"
  safe_pct_exec "$CTID" "cd '$target' && rm -rf current && mkdir -p current && tar -xf payload.tar -C current && chmod +x current/*.sh current/lib/*.sh 2>/dev/null || true"
  pct push "$CTID" "$CONFIG_FILE" "$target/current/config.active.yaml" >/dev/null
}

run_remote_phase() {
  local script="$1"; shift
  local phase_name="$1"; shift || true
  log_info "Running remote phase: $phase_name ($script)"
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[dry-run] would execute in CT $CTID: $script"
    return 0
  fi
  pct exec "$CTID" -- bash -lc "set -Eeuo pipefail; cd /opt/shopware-deploy/current; CONFIG_FILE=/opt/shopware-deploy/current/config.active.yaml bash '$script'" 
}

main() {
  init_paths
  parse_args "$@"
  preflight

  log_info "Starting Shopware Proxmox deployment (version $SCRIPT_VERSION, dry-run=$DRY_RUN)"
  write_json_report "RUNNING" "Deployment started" >/dev/null || true

  run_step "provision_container" bash "$SCRIPT_DIR/install-container.sh" --config "$CONFIG_FILE" ${DRY_RUN:+--dry-run} ${FORCE:+--force}

  if [[ "$PROXMOX_MODE" == "pct" ]]; then
    run_step "copy_payload" copy_payload_into_container
    run_step "install_shopware" run_remote_phase "install-shopware.sh" "shopware-install"
    run_step "configure_performance" run_remote_phase "configure-performance.sh" "performance"
    run_step "configure_security" run_remote_phase "configure-security.sh" "security"
    if [[ "$(yaml_get "$CONFIG_FILE" middleware.enabled false)" == "true" ]]; then
      run_step "configure_middleware" run_remote_phase "configure-middleware.sh" "middleware"
    else
      log_info "Skipping middleware integration prep (middleware.enabled=false). Shopware server only."
      record_step "configure_middleware" "skipped" "middleware.enabled=false" 0
    fi
    run_step "setup_backups" run_remote_phase "setup-backups.sh" "backups"
  else
    die "API-mode remote orchestration requires SSH target configuration; use pct mode on Proxmox host or extend api executor." 78
  fi

  local final_status="PARTIAL_SUCCESS"
  if [[ "$SKIP_TESTS" == "true" ]]; then
    log_warn "Tests skipped; cannot report SUCCESS."
    write_json_report "PARTIAL_SUCCESS" "Deployment completed but tests skipped" >/dev/null
  else
    if run_step "acceptance_tests" run_remote_phase "healthcheck.sh" "acceptance"; then
      final_status="SUCCESS"
      write_json_report "SUCCESS" "All mandatory checks passed" >/dev/null
    else
      final_status="FAILED"
      write_json_report "FAILED" "Acceptance tests failed" >/dev/null
    fi
  fi

  log_info "Deployment finished with status: $final_status"
  [[ "$final_status" == "SUCCESS" ]] || [[ "$final_status" == "PARTIAL_SUCCESS" && "$SKIP_TESTS" == "true" ]] || exit 1
}

main "$@"
