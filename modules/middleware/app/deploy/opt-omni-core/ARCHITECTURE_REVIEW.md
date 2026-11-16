# Omni-Core Architecture Review — 2026-11-15

## Current assessment

Omni-Core is a functional FastAPI ERP/BI middleware with PostgreSQL/SQLite support, Matterhorn import, Shopware integration, Google Merchant operations, OmniRoute provider/model/routing management, competitor intelligence, event bus and a Global Control Center.

The repository is operationally feature-rich but structurally ahead of its hardening layer: the central application file is monolithic, a legacy `omni_core/` implementation coexists with the current root application, and some modules still expose placeholder business logic.

## Target-domain inventory

The proposal is a Commerce + ERP Integration + Finance + BI + AI control plane, not a Shopware-only connector. The active root application already contains substantial parts of that target; these are the current boundaries and gaps:

| Domain | Existing foundation | Main gap |
|---|---|---|
| Core platform | `database.py`, IAM, jobs/scheduler, `services/event_bus.py` | Durable event store, migrations, trace/idempotency guarantees |
| Product intelligence | Matterhorn importer, `services/canonical_product_mapping.py`, GTIN and media services | Persisted versioned canonical product lifecycle and provenance |
| Commerce / orders | Root APIs, `services/order_management.py`, Shopware services | Shared order lifecycle across channels and financial records |
| Sales channels | Connector SDK/registry, channel adapters, Google Merchant | Complete connector coverage, contracts, rate-limit and sync telemetry |
| Inventory / procurement | Inventory guard/sync and stock reservation | Purchase orders, goods receipts, supplier invoices, demand/reorder planning |
| Pricing | Price engine and competitor pricing | Persisted per-channel cost inputs and realized-margin feedback |
| Fulfillment | Fulfillment service and order worker | Returns/shipping carrier lifecycle linked to orders, payments, and invoices |
| Finance | New provider-keyed document, transaction, settlement, and reconciliation records | Settlement-to-transaction matching and links to canonical orders/products |
| Accounting integration | Finance API boundary only | DATEV/other provider adapters, e-invoicing, document archive and validation |
| BI / intelligence | BI engine and Global Control Center | Transaction-backed facts, profitability, cashflow, forecast and anomaly metrics |
| AI | OmniRoute, content, trends, creative/media and agent services | Usage/cost provenance and validated workflows across finance and commerce |
| CRM / marketing | CRM adapters/services and marketing modules | Attribution linked to orders, contribution margin and consent/retention policy |

This is an incremental inventory, not a claim that each listed foundation is production-complete. The canonical data chain still needs explicit IDs/correlation and audited lifecycle transitions across domains.

## Finalized in this revision

- Unified root dashboard as Global Control Center.
- Added browser login using the existing `INTERNAL_API_KEY`.
- Added short-lived HttpOnly signed browser sessions.
- Preserved direct API authentication through `X-Internal-API-Key`.
- Added automatic `INTERNAL_API_KEY` generation to `start.sh`.
- Added runtime uptime/database telemetry to the control-center payload.
- Added operational actions for Matterhorn import and AI trend analysis.
- Added responsive system, AI, marketplace, BI, security and activity views.
- Added browser-session regression tests.
- Corrected the Settings navigation route.
- Documented the control-plane UI.

## Finance foundation added

- Added provider-keyed, idempotent persistence for invoices/credit notes/expenses, payment transactions, and settlements.
- Added authenticated `/api/finance/*` ingestion, listing, overview, and payment-to-invoice reconciliation endpoints.
- Added conservative reconciliation statuses: matched, partial, mismatch, missing document, ambiguous, and review required.
- Amount-only matches are never auto-confirmed; only one exact invoice match with a matching order reference is automatic.
- Added an Accountant role and a permission-protected, currency-grouped finance overview API.
- Kept finance totals out of the general Global Control Center response until its BI permissions are explicitly scoped.
- Contribution margin is calculated only when every required cost input is supplied; this foundation does not create accounting postings or claim tax-compliance.

## Remaining architectural work

### P0 — Production hardening
1. Split `main.py` into FastAPI routers and service composition.
2. Replace the in-process fulfillment queue with a durable queue or database-backed job scheduler.
3. Persist EventBus events in a dedicated event table rather than relying primarily on system logs.
4. Implement real authorization roles instead of one shared internal API secret.
5. Move integration secrets to a proper secret store for production deployments.
6. Encrypt OAuth/API credentials at rest with a dedicated key-management mechanism.

### P1 — Reliability
1. Add idempotency keys to all external write operations.
2. Add retry/circuit-breaker telemetry to the dashboard.
3. Add dead-letter inspection and replay controls to the UI.
4. Add database migration/version management instead of startup-only schema initialization.
5. Add scheduled health checks and integration SLA/error-rate metrics.

### P1 — Commerce intelligence
1. Complete real sales/marketing database integration where placeholder endpoints remain.
2. Connect finance records to order/product/fulfillment facts for evidenced channel-level margin and contribution reporting.
3. Add competitor price snapshots and historical price curves.
4. Add bundle profitability and stock-constrained bundle scoring.
5. Add automated export diffing before marketplace publication.

### P1 — Finance and accounting integrations
1. Add document storage and validation for XRechnung/ZUGFeRD using maintained standards libraries and versioned rules.
2. Implement provider adapters for accounting export (including DATEV) after confirming each provider's current API requirements.
3. Reconcile settlement batches against their constituent payment, fee, refund, and payout transactions.
4. Add expense approvals, tax data, and cashflow projections only after their source data and ownership are modeled.

### P2 — Maintainability
1. Consolidate or explicitly archive the legacy `omni_core/` implementation.
2. Establish a typed domain model for canonical products, orders, channels and AI jobs.
3. Add contract tests for Matterhorn, Shopware, Google Merchant and OmniRoute.
4. Add CI with lint, type checks, unit tests and smoke tests.
5. Version the public API.

## Release position

The system is now materially more usable as an operator-facing control plane. It should be treated as a hardened internal middleware platform, not yet as a fully regulated multi-tenant SaaS control plane.
