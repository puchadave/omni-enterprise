#!/usr/bin/env bash
set -euo pipefail
# Idempotent helper for HOST-side ZeroTier setup (preferred for multi-device modules).
# Does NOT auto-approve devices and does NOT modify Docker resources.
# Usage: ./scripts/zerotier-host-setup.sh [--network <ZT_NETWORK_ID>] [--status]
NET=""; STATUS=0
while [[ $# -gt 0 ]]; do case "$1" in --network) NET="$2"; shift 2;; --status) STATUS=1; shift;; *) echo "unknown $1"; exit 2;; esac; done

if ! command -v zerotier-cli >/dev/null 2>&1; then
  cat <<'MSG'
[info] zerotier-cli not found. Install host agent (recommended):
  - Debian/Ubuntu: curl -s https://install.zerotier.com | sudo bash
  - Alpine: apk add zerotier-one && rc-update add zerotier-one && rc-service zerotier-one start
Then re-run this script. Do NOT run untrusted install commands without review.
MSG
  exit 0
fi

echo "[zt] info:"; zerotier-cli info || true
if [ -n "$NET" ]; then
  echo "[zt] joining network $NET (requires authorization in controller/Central)"
  zerotier-cli join "$NET" || true
fi
echo "[zt] networks:"; zerotier-cli listnetworks || true
if [ $STATUS -eq 1 ]; then echo "[zt] peers:"; zerotier-cli listpeers 2>&1 | head -20 || true; fi
cat <<'MSG'
[next] 1) Authorize this node in ZeroTier Central/controller for the network.
       2) Note assigned Managed IP and record it in shared/networks-and-addressing.md & inventory.
       3) Configure module .env peer URLs to use ZT IPs (e.g. OMNIROUTE_URL=http://<ZT_IP>:20128/v1).
       4) Apply ZeroTier flow rules (deny-by-default, allow required ports/roles only).
MSG
