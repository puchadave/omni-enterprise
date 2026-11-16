# Omni-Core Implementation Status

Audit scope: the active root FastAPI application and its runtime modules, legacy `omni_core/`, tests, Docker configuration, and deployment documentation. Secret-bearing `.env` files were not read. This report describes the checkout as inspected on 2026-11-15; it is not a production-readiness certification.

## Executive status

The repository is a feature-rich internal FastAPI application with a Jinja-based operator UI and a dual PostgreSQL/SQLite persistence adapter. It is not yet the production-ready multi-channel platform described in the master target. The most urgent blocker is authorization: authentication is global, but a route with no matching permission currently passes authorization. A static AST audit found **88 of 211 route-method handlers** with neither a central RBAC prefix nor an endpoint-level permission dependency. Several connector SDK implementations also return fabricated successful exports.

## Status by category

### DONE — present and exercised

- FastAPI application, Jinja templates, signed browser sessions, CSRF checks for state-changing browser requests, and direct internal API-key authentication: `main.py`, `templates/`.
- PostgreSQL (`asyncpg`) and SQLite WAL (`aiosqlite`) persistence behind a raw-SQL adapter: `database.py`.
- Product catalog, order records, inventory reservation, bundles, a database-backed generic job table, and basic fulfillment flow.
- Matterhorn XML/feed import with preview and validation.
- Shopware and Google Merchant client/export implementations outside the connector SDK.
- AI provider/routing, pricing, competitor/trend, finance-source, and control-center foundations.
- Baseline suite: **130 tests passed**. One existing `aiosqlite` thread/event-loop warning appeared in `tests/test_security.py`.

“Done” here means the foundation exists and is exercised; it does not mean every item is production-complete.

### PARTIALLY IMPLEMENTED

- IAM: user/password login and database sessions exist, but password hashing is PBKDF2 rather than Argon2id; MFA, WebAuthn, password reset/change, account throttling, session administration, complete user/role CRUD, and reliable initial-superadmin lifecycle are missing.
- RBAC: role/permission storage and some rules exist, but most route families lack permission mappings, defaults omit several expected read permissions, and unknown routes are effectively allow-after-authentication.
- Audit: system logs are persisted, but audit coverage and immutability are incomplete.
- Products: a mapping helper exists, but there is no persisted, versioned canonical product aggregate with variants, image ownership, provenance, and channel overrides.
- Matterhorn: XML import and preview exist; current import drops extracted GTIN/EAN and image data, does not persist variants, and numeric parsing can silently default malformed values to zero.
- GTIN: provider registry and enrichment/conflict records exist; production data providers and licensing remain configuration-dependent.
- OmniRoute: clients and routing types exist, but parts of the provider/model control plane are in-memory and lack complete usage/cost/latency persistence.
- Pricing and BI: core calculations exist, but sales endpoints include explicit “requires database integration” responses and a pricing endpoint uses sample constants rather than persisted business inputs.
- Connectors: SDK and registry exist, but multiple registered connectors report successful health/exports without making remote API calls. Their declared capabilities overstate working operations.
- Jobs: generic jobs persist and are claimed from PostgreSQL/SQLite, but there is no separate attempts/dead-letter model, stale-running recovery is incomplete, and unknown job types currently complete as success-shaped no-ops.
- Events: event delivery uses an in-process queue; “persistence” writes event snapshots/status into `system_logs`, while queue and dead-letter state are in memory. There is no transactional outbox or durable replay.
- Fulfillment: order work also uses an in-process `asyncio.Queue`, so accepted work can be lost across process failure.
- Deployment: a non-root Docker image and Compose PostgreSQL/app stack exist. The app port is directly published; middleware-specific production TLS/reverse-proxy, backup/restore, migration and rollback configuration are absent.
- Finance: provider-keyed records and conservative payment/invoice matching exist, but settlement constituent matching, order/product lifecycle links, accounting adapters, e-invoicing, and permission-scoped dashboard reporting remain.

### BROKEN — demonstrated behavior contradicts the intended contract

- A route for which `_required_permission()` returns `None` is allowed for any authenticated principal because `_has_permission()` treats no required permission as success.
- Many registry connectors return `ExportResult(success=True)` and synthetic remote IDs without a network call; several `test_connection()` methods treat presence of credentials as proof of connectivity.
- `/api/sales/overview` and `/api/sales/product-scores` return placeholder messages; pricing recommendations use hard-coded sample values.
- `main.py` remains a large monolithic application file; the legacy application and database under `omni_core/` remain in the repository.
- PostgreSQL compatibility migrations catch and ignore all statement errors, then log migration completion.

### MISSING

- Versioned, reproducible database migrations and rollback/upgrade policy.
- Full user/role/permission/session management and protection against removing the final superadmin.
- Argon2id, MFA, WebAuthn/passkeys, password reset, identity providers, actual eID integration, and a validated BundID adapter.
- Central Shopware identity/account linking and customer identity lifecycle.
- Complete canonical product model, persistent channel overrides, and verified source/confidence/timestamp provenance.
- Fully implemented production adapters for every named marketplace; seller/partner approvals and credentials are external prerequisites.
- Transactional outbox and durable event processing/replay/dead-letter storage.
- Persistent order/fulfillment job handoff and worker recovery.
- Complete security endpoint matrix, rate limiting, production HTTPS enforcement, security headers, production CORS policy, and secret encryption/key rotation.
- Middleware-specific production compose/reverse proxy, backup/restore procedure, migration job, rollback and health/readiness split.
- CI/type/lint pipeline and PostgreSQL integration test environment.
- DATEV/e-invoice, cashflow, expense/procurement, complete analytics and other finance/ERP domains described in the target.

### DUPLICATED

- Root `main.py`/`database.py` are the active application; `omni_core/main.py`, `omni_core/database.py`, and `omni_core/services/` contain a second implementation.
- Connector logic exists in `services/channel_adapters.py`, `services/connectors/`, `services/connector_sdk.py`, and direct Shopware/Google export managers.
- Multiple pricing implementations exist (`price_engine.py`, `pricing_engine.py`, competitor pricing).
- The root service set and `omni_core/` each contain trend, fulfillment, inventory, and Matterhorn implementations.
- Order work, generic persistent jobs, and the in-process fulfillment queue overlap without a single durable execution contract.

### DEPRECATED — decision, not deletion

- Treat `omni_core/` as legacy and **DEPRECATE** it; do not delete until imports, tests, deploy scripts, and persisted-data compatibility are verified.
- Treat `services/channel_adapters.py` as legacy channel-feed code and **MERGE** its still-used functionality into the connector SDK before removal.
- Treat the Connector SDK/Registry as the target connector boundary, but **MERGE** it with the real Shopware/Google client implementations and disable operations that have no real adapter.
- Keep `database.py` as the current persistence boundary during migration; **MIGRATE** its runtime schema creation to versioned migrations before removing compatibility DDL.
- Keep Jinja templates as the current WebUI; move route ownership incrementally rather than replacing the UI or removing existing operations.

### SECURITY RISK

- Public exclusions include `/docs`, `/redoc`, `/openapi.json`, and `/api/health`; the health response reveals database type, dependency health/latency, queue size, and uptime.
- `_auth_secret()` falls back to the fixed value `dev-insecure-secret` if neither `SESSION_SECRET` nor `INTERNAL_API_KEY` exists.
- The internal API key grants `*`/SUPERADMIN access and can be used to mint a browser superadmin session; it is a shared break-glass credential, not ordinary IAM.
- `/api/auth/logout` is exempt from authentication and CSRF; the legacy `/logout` endpoint mutates session state via GET.
- Session records store a token hash, but resolution currently looks up the session ID without checking the presented token against that hash.
- No login throttling/brute-force control was found; `mfa_required` is stored but not enforced.
- Missing per-route permissions make authenticated low-privilege users able to access unclassified operations.
- Startup secret generation falls back to time/host-derived strings when OpenSSL is absent.
- No middleware production TLS termination or HTTPS enforcement is configured. Cookie `Secure` is derived from the request scheme, which needs a trusted proxy/deployment policy.
- API documentation is public; no centralized security headers or explicit CORS policy were found.

### PRODUCTION BLOCKER

- Do not expose this checkout as a production admin control plane until deny-by-default authorization and the session-secret issues are fixed and regression-tested.
- Disable or explicitly mark unimplemented connector operations; do not accept success-shaped fake remote responses.
- Use a supported migration strategy and test against PostgreSQL before production upgrades.
- Replace in-memory order/event queues with durable state transitions and recovery.
- Complete TLS/reverse-proxy, backup/restore, startup secret validation, and deployment acceptance checks.
- Finish required identity/MFA work before representing the system as complete IAM or enabling eID/BundID.

## Architecture reconciliation

| Component | Decision | Rationale |
|---|---|---|
| Root `main.py` | **MIGRATE** into `app/api/` routers in small, tested groups | Current runtime source; preserve all existing routes while reducing coupling. |
| Root `database.py` | **MIGRATE** schema evolution to versioned migrations; retain as adapter temporarily | It is the active storage contract, but startup DDL and silent compatibility catches are not a reproducible migration system. |
| `omni_core/` | **DEPRECATE**, then merge only verified missing behavior | A second application/database exists and has internal legacy imports; deleting it now risks functionality/data loss. |
| Jinja `templates/` | **MERGE** into the target `web/` ownership only as route migration proceeds | Current UI is functional and should remain available. |
| `services/connector_sdk.py` + registry | **MIGRATE** as the connector contract | It is the clearest common boundary; capabilities must represent real implemented operations only. |
| Direct Shopware/Google clients | **MERGE** behind connector adapters | They contain real API logic; do not replace them with synthetic SDK success responses. |
| `services/channel_adapters.py` | **DEPRECATE** after feed behavior is migrated and tested | It is a parallel channel-specific path and includes SQL construction that needs parameterization. |
| In-process event/fulfillment queues | **MIGRATE** to DB-backed outbox/job processing | Existing queues cannot guarantee delivery/recovery across process failure. |
| `my-agent/`, `media/`, ignored local DB/env files | **KEEP OUT OF SCOPE** unless a dedicated integration requires them | They are local/untracked data or a separate project; preserve without inspecting secrets or deleting. |

## Implementation order

1. Close authorization gaps, remove predictable signing-secret fallback, protect docs/readiness, and add security regression tests.
2. Correct startup secret generation and define a production deployment boundary with authenticated readiness and TLS termination.
3. Disable fake connector capabilities/results; report only real/configured capabilities.
4. Introduce canonical product persistence and correct Matterhorn GTIN/media/variant and malformed-input handling.
5. Add versioned migrations; then move route groups out of `main.py` without deleting old functionality.
6. Make orders/jobs/events recoverable through durable jobs and transactional outbox.
7. Complete IAM/MFA/admin WebUI and provider feasibility boundaries (eID/BundID inactive until verified).
8. Add provider contracts, e-invoice and finance/BI integrations only after data ownership and external specifications are validated.
9. Run PostgreSQL, security, deployment, and UI acceptance tests; update remaining documentation with measured results.

## Checkout and verification notes

- Working checkout: `/home/puchadav/GIT/omni-core-middleware`.
- Branch is 24 commits ahead and 26 behind its configured upstream at audit time. Do not merge, reset, or switch branches as part of this implementation.
- Existing modified files and untracked `media/`, `my-agent/` were preserved. `services/channel_adapters.py` is a concurrent/pre-existing worktree change and must not be overwritten.
- Baseline: `python -m pytest -q` → 130 passed, 1 `aiosqlite` thread/event-loop warning.
- No PostgreSQL service, browser session, eID/BundID credentials, marketplace approvals, or production reverse proxy was validated in this audit.
