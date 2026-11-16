# Omni-Core ERP & BI Enterprise Middleware

## Start in under two minutes

Prerequisite: Docker Desktop (Windows/macOS) or Docker Engine with Compose v2 (Linux, Proxmox, WSL).

Linux, macOS, Proxmox or WSL:

```sh
chmod +x start.sh
./start.sh
```

Windows:

```bat
start.bat
```

The startup script generates a private `.env` with random database, API, and session-signing secrets on first launch, builds the image, waits for PostgreSQL, and verifies liveness.

Open:

- Dashboard / Global Control Center: http://localhost:8000
- Login: use the generated `INTERNAL_API_KEY` from `.env`
- API documentation: http://localhost:8000/docs
- Public liveness endpoint: http://localhost:8000/healthz
- Authenticated readiness endpoint: http://localhost:8000/api/health

## Included stack

- `db`: PostgreSQL 15 Alpine, internal network only, persistent `pg_data` volume.
- `omni-core`: FastAPI/Uvicorn/uvloop service, port 8000, persistent `app_data` volume.
- `omni-net`: isolated Docker bridge network.

The app waits for the database health check. Its own startup migration retries PostgreSQL connection establishment for up to 60 seconds and creates all required tables and JSONB GIN indexes.

## Configuration

To configure external integrations, either edit `.env` before startup or use the Settings page after the stack is running:

```dotenv
OMNIROUTE_URL=http://your-omniroute-host:port/v1
OMNIROUTE_API_KEY=your-key
MATTERHORN_API_TOKEN=your-token
```

`POSTGRES_PASSWORD` must be a strong unique secret for production. It is never committed: `.env` is ignored by Git and Docker build context.

## Operations

```sh
# Follow application logs
docker compose logs -f omni-core

# Check liveness (public; no dependency details)
curl http://localhost:8000/healthz

# Check runtime state (authenticated with X-Internal-API-Key)
docker compose ps

# Stop without losing data
docker compose down

# Destructive reset: removes PostgreSQL and app volumes
docker compose down -v
```

## Core guarantees

- PostgreSQL production backend via `asyncpg`; SQLite WAL fallback when `DATABASE_URL` is absent.
- Atomic inventory reservation and bundle recalculation prevent overselling.
- Supplier stock is imported with a 10% safety buffer.
- A bundle is active only when every component has stock; its quantity equals the minimum component quantity.
- The in-process `asyncio.Queue` is bounded and processed by exactly one worker. It is intentionally not a durable queue; received orders are persisted before queueing.
- No Redis, Celery, Node.js or frontend build chain is required.

## Scope

Omni-Core is an ERP/middleware control plane. It does not implement a public storefront, shopping cart, checkout or payment processing.



## Proxmox one-click installations

**Shopware 6 server (Proxmox LXC, no middleware installed):**

```sh
bash -c "$(curl -fsSL -H 'Cache-Control: no-cache' https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/install.sh)"
```

With custom config:

```sh
CONFIG_URL="https://example.com/shopware-config.yaml" \
bash -c "$(curl -fsSL -H 'Cache-Control: no-cache' https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/install.sh)"
```

Advanced options: `deploy/shopware-proxmox/README.md`, `deploy/shopware-proxmox/config.yaml`.

## Control Plane UI

The root route is the unified Global Control Center. Browser access is authenticated through a short-lived, HttpOnly signed session using the separate `SESSION_SECRET` from `.env`; keep this value private and at least 32 characters long. Direct API clients can continue to authenticate with `X-Internal-API-Key`. Readiness, API docs, and OpenAPI are authenticated; `/healthz` exposes only liveness.

Main operational areas:
- Global Control Center: system, ERP/BI, AI, marketplace and security telemetry.
- Products and Orders: operational administration.
- Settings: integration configuration with masked secrets.
- OmniRoute: provider, model and routing administration.
- Google Merchant: account and export operations.
- Finance API: idempotent document, payment and settlement ingestion plus reviewable payment-to-invoice reconciliation.

The dashboard polls control-center telemetry every 15 seconds and exposes safe operational actions for Matterhorn import and trend analysis.

Finance endpoints, payloads, permissions and current scope are documented in [docs/FINANCE.md](docs/FINANCE.md). The finance layer integrates records with accounting providers; it is not bookkeeping or tax software.

CRM endpoints, guardrails and channel ingestion are documented in [docs/CRM.md](docs/CRM.md). Scene Intelligence (pulse/radar/trends) aggregates stored real records via `/api/scene-intelligence/*` and the News & Events UI; no external signals are fabricated.
