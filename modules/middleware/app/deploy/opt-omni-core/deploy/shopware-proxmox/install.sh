#!/usr/bin/env bash
# Proxmox One-Click Shopware Deployment - remote bootstrap installer
# Usage (installation link):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/<owner>/<repo>/main/deploy/shopware-proxmox/install.sh)"
#
# Optional environment variables:
#   REPO_URL=https://github.com/puchadave/omni-core-middleware.git
#   REPO_REF=main
#   CONFIG_URL=https://example.com/my-shopware-config.yaml
#   CONFIG_FILE=/root/shopware-config.yaml
#   DEPLOY_ARGS="--force --skip-tests"

set -Eeuo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log_info()  { echo -e "${GREEN}[INFO]${NC} $*" >&2; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
die() { log_error "$*"; exit 1; }

REPO_URL="${REPO_URL:-https://github.com/puchadave/omni-core-middleware.git}"
REPO_REF="${REPO_REF:-main}"
WORKDIR="${WORKDIR:-/opt/shopware-oneclick-bootstrap}"
CONFIG_URL="${CONFIG_URL:-}"
CONFIG_FILE="${CONFIG_FILE:-}"
DEPLOY_ARGS="${DEPLOY_ARGS:-}"

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"; }

main() {
  [[ "$(id -u)" -eq 0 ]] || die "Run as root on the Proxmox VE host."
  require_cmd bash
  require_cmd curl
  require_cmd git
  require_cmd python3

  if command -v pct >/dev/null 2>&1 && [[ -d /etc/pve ]]; then
    log_info "Proxmox VE host detected."
  else
    log_warn "Proxmox pct tools not detected; deploy may fail unless API mode is fully configured."
  fi

  mkdir -p "$WORKDIR"
  local src="$WORKDIR/src"
  if [[ -d "$src/.git" ]]; then
    log_info "Updating existing bootstrap checkout..."
    git -C "$src" fetch --depth 1 origin "$REPO_REF" || true
    git -C "$src" checkout "$REPO_REF" || true
    git -C "$src" pull --ff-only origin "$REPO_REF" || true
  else
    log_info "Cloning deployment repository..."
    rm -rf "$src"
    git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$src"
  fi

  local deploy_dir="$src/deploy/shopware-proxmox"
  [[ -d "$deploy_dir" ]] || die "Deployment directory not found: $deploy_dir (check REPO_URL/REPO_REF)"
  chmod +x "$deploy_dir"/*.sh "$deploy_dir"/lib/*.sh 2>/dev/null || true

  local cfg="$deploy_dir/config.yaml"
  if [[ -n "$CONFIG_URL" ]]; then
    log_info "Downloading configuration from CONFIG_URL..."
    curl -fsSL "$CONFIG_URL" -o "$WORKDIR/config.yaml"
    cfg="$WORKDIR/config.yaml"
  elif [[ -n "$CONFIG_FILE" ]]; then
    [[ -f "$CONFIG_FILE" ]] || die "CONFIG_FILE not found: $CONFIG_FILE"
    cfg="$CONFIG_FILE"
  else
    log_warn "No CONFIG_URL/CONFIG_FILE provided. Using repository default config.yaml - review and adjust before production use."
  fi

  log_info "Starting deployment: $deploy_dir/deploy-shopware.sh --config $cfg $DEPLOY_ARGS"
  cd "$deploy_dir"
  # shellcheck disable=SC2086
  bash ./deploy-shopware.sh --config "$cfg" $DEPLOY_ARGS
}

main "$@"
