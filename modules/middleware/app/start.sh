#!/bin/sh
# Omni-Core ERP & BI: zero-config Docker startup for Linux, macOS, Proxmox and WSL.
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$PROJECT_DIR"

info() { printf '%s\n' "[INFO] $*"; }
fail() { printf '%s\n' "[ERROR] $*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || fail "Docker is not installed. Install Docker Engine/Desktop first."
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required."
docker info >/dev/null 2>&1 || fail "Docker daemon is not available. Start Docker and retry."

if [ ! -f .env ]; then
    command -v openssl >/dev/null 2>&1 || fail "OpenSSL is required to generate secure secrets."
    DB_PASSWORD=$(openssl rand -hex 24)
    INTERNAL_API_KEY=$(openssl rand -hex 32)
    SESSION_SECRET=$(openssl rand -hex 32)
    umask 077
    cat > .env <<EOF
POSTGRES_USER=omnicore
POSTGRES_PASSWORD=${DB_PASSWORD}
POSTGRES_DB=omnicore
OMNI_CORE_PORT=8000
OMNIROUTE_URL=
OMNIROUTE_API_KEY=
MATTERHORN_API_TOKEN=
INTERNAL_API_KEY=${INTERNAL_API_KEY}
SESSION_SECRET=${SESSION_SECRET}
EOF
    info "Created .env with a generated PostgreSQL password."
fi

if [ -z "${SESSION_SECRET:-}" ] && ! grep -Eq '^SESSION_SECRET=.{32,}$' .env; then
    fail "SESSION_SECRET is missing or too short in .env. Add a 32+ character random secret (for example: openssl rand -hex 32)."
fi

info "Building and starting Omni-Core..."
docker compose up --build -d --remove-orphans

attempt=1
while [ "$attempt" -le 60 ]; do
    if docker compose exec -T omni-core wget -q -O /dev/null http://127.0.0.1:8000/healthz 2>/dev/null; then
        printf '\nOmni-Core is ready.\n'
        printf '  Dashboard: http://localhost:%s\n' "$(grep '^OMNI_CORE_PORT=' .env | cut -d= -f2 || printf '8000')"
        printf '  API docs:  http://localhost:%s/docs\n' "$(grep '^OMNI_CORE_PORT=' .env | cut -d= -f2 || printf '8000')"
        printf '  Logs:      docker compose logs -f omni-core\n'
        exit 0
    fi
    sleep 1
    attempt=$((attempt + 1))
done

printf '%s\n' '[ERROR] Stack did not become healthy within 60 seconds.' >&2
docker compose ps >&2 || true
docker compose logs --tail=100 omni-core >&2 || true
exit 1
