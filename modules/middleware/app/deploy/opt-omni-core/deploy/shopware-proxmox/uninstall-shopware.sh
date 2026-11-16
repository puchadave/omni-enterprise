#!/usr/bin/env bash
# Controlled deinstallation of Shopware deployment artifacts/container.
# NEVER deletes other containers and NEVER performs destructive actions without explicit confirmation.

set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: uninstall-shopware.sh [--config FILE] [--destroy-container] [--purge-backups] [--yes-i-know]
  --config FILE         Config used for deployment (to identify CTID/paths)
  --destroy-container   Stop and destroy ONLY the configured CTID (requires confirmations)
  --purge-backups       Also remove configured backup target (dangerous, requires confirmations)
  --yes-i-know          Skip interactive prompts ONLY if you also set CONFIRM_CTID=<ctid> and CONFIRM_PHRASE=DELETE-SHOPWARE

Safety: Without explicit confirmations this script exits without changes.
EOF
}

DESTROY_CT=false
PURGE_BACKUPS=false
YES=false

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --config) CONFIG_FILE="$2"; shift 2;;
      --destroy-container) DESTROY_CT=true; shift;;
      --purge-backups) PURGE_BACKUPS=true; shift;;
      --yes-i-know) YES=true; shift;;
      -h|--help) usage; exit 0;;
      *) die "Unknown argument: $1";;
    esac
  done
}

confirm() {
  local prompt="$1" expected="$2"
  if [[ "$YES" == "true" ]]; then
    [[ "${NONINTERACTIVE_CONFIRM:-}" == "$expected" ]] || return 1
    return 0
  fi
  read -r -p "$prompt" ans
  [[ "$ans" == "$expected" ]]
}

main() {
  init_paths
  parse_args "$@"
  CONFIG_FILE="${CONFIG_FILE:-$SCRIPT_DIR/config.yaml}"
  init_runtime
  local ctid; ctid="$(yaml_get "$CONFIG_FILE" container.ctid 210)"
  local backup_dir; backup_dir="$(yaml_get "$CONFIG_FILE" backup.target_dir /var/backups/shopware)"

  log_warn "This operation is potentially destructive."
  log_info "Target CTID from config: $ctid"
  log_info "Configured backup dir: $backup_dir"

  if [[ "$DESTROY_CT" == "true" ]]; then
    is_root || die "Must run as root to manage containers."
    command -v pct >/dev/null 2>&1 || die "pct not available on this host."
    pct status "$ctid" >/dev/null 2>&1 || die "CTID $ctid not found; refusing to guess or delete other containers."
    # explicit double confirmation
    if [[ "$YES" == "true" ]]; then
      [[ "${CONFIRM_CTID:-}" == "$ctid" && "${CONFIRM_PHRASE:-}" == "DELETE-SHOPWARE" ]] || die "Non-interactive destroy requires CONFIRM_CTID=$ctid and CONFIRM_PHRASE=DELETE-SHOPWARE"
    else
      confirm "Type CTID ($ctid) to confirm container destruction: " "$ctid" || die "Confirmation failed. No changes made."
      confirm "Type DELETE-SHOPWARE to permanently destroy CT $ctid: " "DELETE-SHOPWARE" || die "Confirmation failed. No changes made."
    fi
    log_info "Stopping CT $ctid..."
    pct stop "$ctid" >/dev/null 2>&1 || true
    log_info "Destroying CT $ctid (ONLY this container)..."
    pct destroy "$ctid" --purge 1 >/dev/null 2>&1 || pct destroy "$ctid" >/dev/null
    log_ok "Container $ctid destroyed."
  else
    log_info "Container destruction not requested (--destroy-container)."
  fi

  if [[ "$PURGE_BACKUPS" == "true" ]]; then
    if [[ "$YES" == "true" ]]; then
      [[ "${CONFIRM_BACKUP_PURGE:-}" == "PURGE-BACKUPS" ]] || die "Non-interactive backup purge requires CONFIRM_BACKUP_PURGE=PURGE-BACKUPS"
    else
      confirm "Type PURGE-BACKUPS to delete backup dir $backup_dir: " "PURGE-BACKUPS" || die "Backup purge not confirmed."
    fi
    [[ -n "$backup_dir" && "$backup_dir" != "/" ]] || die "Refusing unsafe backup path."
    rm -rf "$backup_dir"
    log_ok "Backup directory removed: $backup_dir"
  fi

  log_ok "Uninstall routine completed (no other containers touched)."
}

main "$@"
