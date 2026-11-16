#!/usr/bin/env bash
set -euo pipefail
# Read-only inventory snapshot (local host). Does NOT modify or delete anything.
OUT="${1:-/tmp/omni-inventory-$(date +%Y%m%d-%H%M%S).txt}"
{
  echo "# OMNI Inventory $(date -Is)"
  echo; echo "## docker info"; docker info 2>&1 | sed -n '1,120p' || true
  echo; echo "## docker ps -a"; docker ps -a --format 'table {{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>&1 || true
  echo; echo "## volumes"; docker volume ls 2>&1 || true
  echo; echo "## networks"; docker network ls 2>&1 || true
  echo; echo "## secrets"; docker secret ls 2>&1 || true
  echo; echo "## configs"; docker config ls 2>&1 || true
  echo; echo "## compose version"; docker compose version 2>&1 || true
  echo; echo "## zerotier"; (command -v zerotier-cli >/dev/null 2>&1 && zerotier-cli info && zerotier-cli listnetworks) 2>&1 || echo "zerotier-cli not installed"
} > "$OUT"
echo "[ok] inventory written: $OUT"
