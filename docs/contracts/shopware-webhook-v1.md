# Shopware <-> OMNI Middleware Webhook Contract v1

## Scope
Inbound Shopware events delivered to OMNI Middleware. Transport is at-least-once; processing must be effect-once via idempotency.

## Endpoint
- `POST /api/webhooks/shopware/{connection_id}`
- Auth: HMAC signature, no browser session/CSRF.
- Content-Type: `application/json; charset=utf-8`

## Required Headers
- `X-Omni-Event`: event name, e.g. `shopware.order.written`
- `X-Omni-Delivery-Id`: UUID per delivery attempt (sender-generated)
- `X-Omni-Idempotency-Key`: stable idempotency key per logical event
- `X-Omni-Timestamp`: Unix epoch seconds
- `X-Omni-Signature`: `sha256=<hex(hmac_sha256(shared_secret, raw_body))>`

## Body
```json
{
  "contract_version": "1.0",
  "event": "shopware.order.written",
  "source": "shopware-pve04",
  "occurred_at": "2026-11-24T12:00:00+00:00",
  "delivery_id": "uuid",
  "idempotency_key": "sha256...",
  "entity": "order",
  "entity_id": "...",
  "operation": "update",
  "payload": {},
  "trace_id": "optional"
}
```

## Verification
- Reject missing/invalid signature with `401`.
- Reject unknown connection or disabled connection with `404/409`.
- Reject malformed JSON/schema with `400`.
- Optional replay window: timestamp +/- 300s; if absent, accept but audit.

## Idempotency / Persistence
- Middleware persists inbox row with `UNIQUE(connection_id, idempotency_key)`.
- Duplicate key returns `200 {"status":"duplicate"}` and does not reprocess.
- Accepted new event returns `202 {"status":"accepted"}` after durable insert.

## Processing
- Inbound handler maps event to internal action (CRM/order/product sync) asynchronously/best-effort after durable ack.
- Failures set inbox status `failed` with error and correlation id; retries are controlled by sender and safe due to dedupe.

## Security
- Secrets per Shopware connection in middleware DB/config, never logged.
- Constant-time signature compare.
- Payload size limit and strict JSON parsing.
- Audit log includes event, connection, idempotency key, status, correlation id; excludes secret/raw sensitive fields.
