#!/usr/bin/env bash
set -euo pipefail
# Safe helpers: prints/creates backups, NEVER deletes source volumes/data.
# Usage examples:
#   ./scripts/backup-volumes.sh list
#   ./scripts/backup-volumes.sh pg-dump --container omni-enterprise-db --db omnicore --user omnichat --out backups
#   ./scripts/backup-volumes.sh volume --name omni-enterprise_pg_data --out backups
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$ROOT"
mkdir -p backups
cmd="${1:-list}"; shift || true
case "$cmd" in
  list)
    echo "== volumes =="; docker volume ls
    echo "== hint: identify project volumes before backup =="
    ;;
  pg-dump)
    CONTAINER=""; DB=""; USER="postgres"; OUT="backups"
    while [[ $# -gt 0 ]]; do case "$1" in --container) CONTAINER="$2"; shift 2;; --db) DB="$2"; shift 2;; --user) USER="$2"; shift 2;; --out) OUT="$2"; shift 2;; *) echo "unknown $1"; exit 2;; esac; done
    [ -n "$CONTAINER" ] && [ -n "$DB" ] || { echo "need --container and --db"; exit 2; }
    TS=$(date +%Y%m%d-%H%M%S); FILE="$OUT/${DB}-${TS}.sql.gz"
    echo "[backup] pg_dump $DB from $CONTAINER -> $FILE"
    docker exec "$CONTAINER" pg_dump -U "$USER" "$DB" | gzip > "$FILE"
    echo "[ok] $FILE"
    ;;
  volume)
    NAME=""; OUT="backups"
    while [[ $# -gt 0 ]]; do case "$1" in --name) NAME="$2"; shift 2;; --out) OUT="$2"; shift 2;; *) echo "unknown $1"; exit 2;; esac; done
    [ -n "$NAME" ] || { echo "need --name"; exit 2; }
    TS=$(date +%Y%m%d-%H%M%S); FILE="$OUT/${NAME}-${TS}.tar.gz"
    echo "[backup] volume $NAME -> $FILE (read-only mount)"
    docker run --rm -v "${NAME}:/from:ro" -v "$PWD/${OUT}:/to" alpine sh -lc "cd /from && tar czf /to/$(basename "$FILE") ."
    echo "[ok] $FILE"
    ;;
  *)
    echo "usage: $0 <list|pg-dump --container C --db D [--user U] [--out DIR]|volume --name V [--out DIR]>"; exit 2;;
esac
