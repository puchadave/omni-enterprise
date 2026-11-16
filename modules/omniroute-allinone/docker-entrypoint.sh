#!/bin/sh
set -eu

# --- Keys (all known OmniRoute API keys included for this all-in-one build) ---
# Primary key (used for gateway auth/health). If unset/placeholder, fall back to bundled default.
if [ -z "${OMNIROUTE_API_KEY:-}" ] || [ "${OMNIROUTE_API_KEY}" = "__GENERATE__" ] || [ "${OMNIROUTE_API_KEY}" = "change_me" ]; then
  OMNIROUTE_API_KEY="${OMNIROUTE_API_KEY_DEFAULT:-sk-814c9467fd9c9314-a41c41-72e2d3b0}"
  export OMNIROUTE_API_KEY
fi
# All accepted keys (comma/space separated) - exposed for clients/configs that support multi-key auth
: "${OMNIROUTE_API_KEYS:=sk-814c9467fd9c9314-a41c41-72e2d3b0,test-key}"
export OMNIROUTE_API_KEYS

# Storage encryption key (generate only if truly empty)
if [ -z "${OMNIROUTE_STORAGE_ENCRYPTION_KEY:-}" ] || [ "${OMNIROUTE_STORAGE_ENCRYPTION_KEY}" = "__GENERATE__" ] || [ "${OMNIROUTE_STORAGE_ENCRYPTION_KEY}" = "change_me" ]; then
  OMNIROUTE_STORAGE_ENCRYPTION_KEY="$(openssl rand -hex 32)"
  export OMNIROUTE_STORAGE_ENCRYPTION_KEY
  echo "[omniroute-allinone] generated OMNIROUTE_STORAGE_ENCRYPTION_KEY (persist it in .env)"
fi

# --- Seed bundled configs into runtime dirs (all configs self-contained) ---
mkdir -p /root/.omniroute /data

seed_if_missing() {
  src="$1"; dst="$2"
  if [ -f "$src" ] && [ ! -f "$dst" ]; then
    cp "$src" "$dst"
    echo "[omniroute-allinone] seeded $(basename "$dst")"
  fi
}
seed_if_missing /opt/omniroute/config.json /root/.omniroute/config.json
seed_if_missing /opt/omniroute/omnicore-auto-combos.json /data/omnicore-auto-combos.json
seed_if_missing /opt/omniroute/endpoints.json /data/endpoints.json
# keep a copy of endpoints in config dir too (some setups expect it there)
seed_if_missing /opt/omniroute/endpoints.json /root/.omniroute/endpoints.json

# write effective env snapshot (for reference inside container)
cat > /data/omniroute.effective.env 2>/dev/null <<ENV || true
OMNIROUTE_PORT=${OMNIROUTE_PORT:-20128}
REQUIRE_API_KEY=${REQUIRE_API_KEY:-true}
OMNIROUTE_API_KEY=${OMNIROUTE_API_KEY}
OMNIROUTE_API_KEYS=${OMNIROUTE_API_KEYS}
ENV

echo "[omniroute-allinone] ready on :${OMNIROUTE_PORT:-20128} (keys configured: primary + OMNIROUTE_API_KEYS)"
exec "$@"
