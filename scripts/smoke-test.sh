#!/usr/bin/env bash
set -euo pipefail
# Basic reachability/health smoke tests. Safe/read-only HTTP checks.
# Usage: ./scripts/smoke-test.sh [--middleware URL] [--omniroute URL] [--omniroute-key KEY]
MW_URL="${MW_URL:-http://127.0.0.1:8002}"
OR_URL="${OR_URL:-http://127.0.0.1:20128}"
OR_KEY="${OR_KEY:-${OMNIROUTE_API_KEY:-}}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --middleware) MW_URL="$2"; shift 2;;
    --omniroute) OR_URL="$2"; shift 2;;
    --omniroute-key) OR_KEY="$2"; shift 2;;
    *) echo "unknown arg $1"; exit 2;;
  esac
done
FAIL=0
check(){ local name="$1"; shift; echo -n "[check] $name ... "; if "$@" >/dev/null 2>&1; then echo OK; else echo FAIL; FAIL=1; fi; }
check "middleware ${MW_URL}/healthz" curl -fsS --max-time 5 "${MW_URL%/}/healthz"
if [ -n "$OR_KEY" ]; then
  check "omniroute ${OR_URL}/v1/models (auth)" curl -fsS --max-time 5 -H "Authorization: Bearer $OR_KEY" "${OR_URL%/}/v1/models"
else
  check "omniroute ${OR_URL}/v1/models (no-auth probe 200/401/403)" bash -lc "code=\$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 '${OR_URL%/}/v1/models'); echo \$code | grep -qE '^(200|401|403)$'"
fi
if [ $FAIL -eq 0 ]; then echo "[ok] smoke tests passed"; else echo "[fail] some checks failed"; fi
exit $FAIL
