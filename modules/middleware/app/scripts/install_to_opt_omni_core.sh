#!/usr/bin/env bash
set -euo pipefail
SRC="$(cd "$(dirname "$0")/../deploy/opt-omni-core" && pwd)"
DST="/opt/omni-core"
if [ "$(id -u)" -ne 0 ]; then echo "Bitte als root/sudo ausführen."; exit 1; fi
mkdir -p /opt
if [ -d "$DST" ]; then
  ts=$(date +%Y%m%d-%H%M%S)
  mv "$DST" "${DST}.bak.${ts}"
  echo "Backup: ${DST}.bak.${ts}"
fi
mkdir -p "$DST"
rsync -a --delete "$SRC/" "$DST/"
cd "$DST"
docker compose down --remove-orphans 2>/dev/null || true
docker compose up -d --build
echo "---"
docker compose ps
echo "URL: http://localhost:${OMNI_CORE_PORT:-8003}  Login: admin@omni-core.local / Admin1234"
