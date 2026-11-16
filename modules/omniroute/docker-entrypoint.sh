#!/bin/sh
set -eu

: "${OMNIROUTE_PORT:=20128}"
: "${OMNIROUTE_SERVER_HOST:=0.0.0.0}"

mkdir -p /root/.omniroute /opt/omniroute /data

# generate API key on first start if missing/placeholder (persisted via mounted env/config volume strategy: user should set in .env)
if [ -z "${OMNIROUTE_API_KEY:-}" ] || [ "${OMNIROUTE_API_KEY}" = "__GENERATE__" ] || [ "${OMNIROUTE_API_KEY}" = "change_me" ]; then
  OMNIROUTE_API_KEY="$(openssl rand -hex 32)"
  export OMNIROUTE_API_KEY
  echo "[omniroute] generated OMNIROUTE_API_KEY (persist it in .env): ${OMNIROUTE_API_KEY}"
fi

# ensure storage encryption key
if [ -z "${OMNIROUTE_STORAGE_ENCRYPTION_KEY:-}" ] || [ "${OMNIROUTE_STORAGE_ENCRYPTION_KEY}" = "__GENERATE__" ]; then
  OMNIROUTE_STORAGE_ENCRYPTION_KEY="$(openssl rand -hex 32)"
  export OMNIROUTE_STORAGE_ENCRYPTION_KEY
fi

# write runtime env file for omniroute
cat > /opt/omniroute/runtime.env <<EOF
OMNIROUTE_SERVER_HOST=${OMNIROUTE_SERVER_HOST}
OMNIROUTE_PORT=${OMNIROUTE_PORT}
REQUIRE_API_KEY=${REQUIRE_API_KEY:-true}
OMNIROUTE_API_KEY=${OMNIROUTE_API_KEY}
OMNIROUTE_TIMEOUT_MS=${OMNIROUTE_TIMEOUT_MS:-1200}
OMNIROUTE_FAILOVER_TIMEOUT_MS=${OMNIROUTE_FAILOVER_TIMEOUT_MS:-1400}
OMNIROUTE_RETRY_429=${OMNIROUTE_RETRY_429:-true}
OMNIROUTE_RETRY_502=${OMNIROUTE_RETRY_502:-true}
OMNIROUTE_RETRY_503=${OMNIROUTE_RETRY_503:-true}
OMNIROUTE_CACHE_RESPONSES=${OMNIROUTE_CACHE_RESPONSES:-true}
OMNIROUTE_CACHE_TTL_SECONDS=${OMNIROUTE_CACHE_TTL_SECONDS:-300}
OMNIROUTE_STORAGE_ENCRYPTION_KEY=${OMNIROUTE_STORAGE_ENCRYPTION_KEY}
EOF
chmod 600 /opt/omniroute/runtime.env 2>/dev/null || true

# merge base config with extended combos if present
if [ -f /data/omnicore-auto-combos.json ] && [ -f /root/.omniroute/config.json ]; then
  : # keep user-provided config.json in volume as source of truth
fi

echo "[omniroute] starting on ${OMNIROUTE_SERVER_HOST}:${OMNIROUTE_PORT} (require_api_key=${REQUIRE_API_KEY:-true})"
exec omniroute --env-file /opt/omniroute/runtime.env
