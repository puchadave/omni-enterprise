# OMNI <-> Shopware Integration Contract v1 (current implementation)

## 1. Middleware -> Shopware (Outbound, authoritative)
- Auth: OAuth2 client credentials; token endpoints `/api/oauth/token` fallback `/oauth/token`.
- Required config: `shop_url`, `api_client_id`, `api_client_secret`, optional `host_header`, `sales_channel_id`, `language_id`, `currency_id`, `tax_id`.
- Health probes: `GET /api/_info/version`, `/api/_action/system-info`, fallback `/store-api/context`.
- Product import: `POST /api/product` (Admin API), payload minimum: `name, productNumber=SKU, stock, taxId, price[{currencyId,gross,net,linked=false}], active=false, description`.
- Readback: `GET /api/product/{id}` or search by `productNumber`.
- Idempotency: SKU/productNumber is external key; readback required before READY_FOR_SALE.

## 2. Shopware -> Middleware (Inbound webhooks)
- Endpoint: `POST /api/webhooks/shopware/{connection_id}`
- Security: `X-Omni-Signature: sha256=<hmac_sha256(secret, raw_body)>`, constant-time compare.
- Required headers: `X-Omni-Event`, `X-Omni-Delivery-Id`, `X-Omni-Idempotency-Key`, `X-Omni-Timestamp`; `X-Correlation-Id` recommended.
- Idempotency: middleware inbox table `shopware_webhook_inbox` unique `(connection_id,idempotency_key)`; duplicates return 200.
- Ack: accepted new event returns 202 after durable insert.
- Processing: `order.*` -> CRM ingest best-effort; `product.*` -> audit-only until mapping contract finalized.

## 3. Failure semantics
- 400 invalid JSON/schema, 401 invalid signature, 404 unknown connection, 413 too large, 500 processing failure (sender may retry; dedupe prevents double effect).
- No secrets in logs. Correlation/idempotency IDs logged.

## 4. Gaps / next hardening
- Per-connection webhook secret persistence in DB (currently runtime/env fallback supported).
- Async retry worker/DLQ for failed inbox rows.
- Product event field mapping and authoritative upsert contract.
- Shopware outbox retry/resend CLI and Messenger transport.
