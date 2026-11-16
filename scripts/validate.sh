#!/usr/bin/env bash
set -euo pipefail
# Validates compose/yaml configs without starting anything. Read-only.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
FAIL=0
run(){ echo "+ $*"; if "$@"; then echo "  -> OK"; else echo "  -> FAIL"; FAIL=1; fi; }

echo "== root compose (env example) =="
run docker compose -f docker-compose.yml --env-file .env.example config >/dev/null

echo "== modules =="
for m in modules/*/docker-compose.yml; do
  printf -- "-- %s: " "$m"
  if docker compose -f "$m" config >/dev/null 2>&1; then echo OK; else echo FAIL; FAIL=1; fi
done

echo "== swarm yaml parse =="
run python3 - <<'PY'
import yaml,glob
for p in sorted(glob.glob('stacks/swarm/*.yml')+glob.glob('stacks/swarm/*.yaml')):
    yaml.safe_load(open(p))
print('yaml ok')
PY

echo "== bash syntax =="
run bash -n start-all.sh scripts/*.sh modules/*/start.sh 2>&1 || true

if [ $FAIL -eq 0 ]; then echo "[ok] validation passed"; else echo "[fail] validation issues"; fi
exit $FAIL
