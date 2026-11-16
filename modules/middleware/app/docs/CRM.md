# Omnichannel CRM & Communication Center

Omni-Core provides a normalized CRM/communication layer for customer identity, unified threads/messages, appointments, and order-linked service actions.

## Scope
- Channels: `shopware`, `amazon`, `ebay`, `email`, `messenger` (normalized ingestion; no fabricated external messages).
- Entities: `crm_customers`, `crm_threads`, `crm_messages`, `crm_appointments` (PostgreSQL/SQLite).
- Sync/webhooks ingest only from configured/authorized channels; ingestion is idempotent by `(channel, external_message_id/idempotency key)`.

## APIs (authenticated, RBAC)
- `GET /api/crm/overview` (`products.read`) – counts and channel breakdown.
- `GET /api/crm/customers?limit=&q=` (`products.read`)
- `GET /api/crm/threads?limit=&channel=&status=` (`products.read`) – enriched with order/payment/fulfillment context and last message.
- `GET /api/crm/threads/{thread_id}/messages` (`products.read`)
- `POST /api/crm/ingest` (`system.configure`) – idempotent webhook payload ingestion `{channel,direction,payload}`.
- `POST /api/crm/sync/{channel}` (`system.configure`) – config-aware channel sync (no-op/error detail when not configured).
- `GET/POST /api/crm/appointments` (`products.read` / `system.configure`)
- `POST /api/crm/actions` (`system.configure`) – guarded manual actions `{action,order_id,payload}`.

UI: `/ui/crm` (CRM & Omnichannel Communication Center).

## Guardrails
- `manual_fulfillment_release`, `manual_approval`, `goodwill_credit` require a fully verified payment status and reject when fulfillment is locked/unverified (server-side enforced).
- Actions are logged/audited as events; external execution happens only through configured systems.
- No fabricated identity verification, messages, or provider calls; missing/unconfigured integrations return honest empty/error states.
