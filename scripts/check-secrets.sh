#!/usr/bin/env bash
set -euo pipefail
# Scans .env files for placeholders/empty critical secrets. Read-only. Exit 1 if issues found.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
shopt -s nullglob
FAIL=0
FILES=("$ROOT/.env" "$ROOT/stacks/swarm/.env" "$ROOT"/modules/*/.env)
CRIT_KEYS=(POSTGRES_PASSWORD SESSION_SECRET INTERNAL_API_KEY SECRET_KEY JWT_SECRET OMNIROUTE_API_KEY OMNIROUTE_STORAGE_ENCRYPTION_KEY)
BAD_RE='(^$|^change_me|^__GENERATE__|^example|^CHANGEME)'
echo "[check-secrets] scanning .env files (if present)"
for f in "${FILES[@]}"; do
  [ -f "$f" ] || continue
  echo "-- $f"
  for k in "${CRIT_KEYS[@]}"; do
    v=$(grep -E "^${k}=" "$f" 2>/dev/null | tail -1 | cut -d= -f2- || true)
    [ -z "${v+x}" ] && continue
    # ignore commented examples in .env? .env normally not commented
    if [[ "$v" =~ $BAD_RE ]]; then
      echo "  [WARN] $k looks placeholder/empty"
      FAIL=1
    fi
  done
done
if [ $FAIL -eq 0 ]; then echo "[ok] no obvious placeholder secrets found in existing .env files"; else echo "[fail] placeholders found – rotate/set before production use"; fi
exit $FAIL
