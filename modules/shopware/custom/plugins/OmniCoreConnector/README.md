# OmniCoreConnector (Shopware 6.7)

Shopware-seitiges Gegenstück zum OMNI-Middleware-Connector. Es sendet **signierte, idempotente** Outbound-Events an die Middleware und stellt einen Healthcheck bereit.

## Vertrag (aus Middleware abgeleitet)

- **Middleware -> Shopware (bestehend):** OAuth2 Integration Client Credentials (`/api/oauth/token` fallback `/oauth/token`), Admin API `/api/*`, Health via `/api/_info/version` o.ä., Import über `POST /api/product` mit `productNumber/taxId/price/active`.
- **Shopware -> Middleware (dieses Plugin):** POST JSON an `middlewareBaseUrl` (empfohlen: `https://<middleware>/api/webhooks/shopware`).
  - Header: `Content-Type: application/json`, `X-Omni-Event`, `X-Omni-Idempotency-Key`, `X-Omni-Signature: sha256=<hmac_sha256(body, sharedSecret)>`
  - Body: `{event, source, occurred_at, payload}`
  - Idempotenz: `UNIQUE(idempotency_key)` in `omnicore_connector_event_outbox`; Duplikate werden nicht erneut gesendet.

## Sicherheit

- Secret nur über Shopware SystemConfig (`OmniCoreConnector.config.sharedSecret`), nie hardcoden.
- HMAC SHA-256 über Raw Body.
- Keine DB-Direktzugriffe durch Middleware; Plugin nutzt DAL/DBAL im Shopware-Kontext.
- Input/Config werden defensiv geprüft; Fehler werden geloggt, keine stillen Exceptions.

## Installation (dockware/dev)

```bash
docker cp modules/shopware/custom/plugins/OmniCoreConnector omni-shopware-app:/var/www/html/custom/plugins/
docker exec omni-shopware-app bin/console plugin:refresh
docker exec omni-shopware-app bin/console plugin:install --activate OmniCoreConnector
docker exec omni-shopware-app bin/console cache:clear
docker exec omni-shopware-app bin/console omnicore:connector:health
```

## Konfiguration

Shopware Admin > Extensions > My extensions > OmniCoreConnector > Configure:
- Enabled
- Middleware Base URL (`/api/webhooks/shopware`)
- HMAC Shared Secret
- Source ID

## Offene Middleware-Gegenstelle

Middleware hat aktuell **keinen** verifizierten `/api/webhooks/shopware`-Empfänger. Vor Produktiv-Inbound muss Middleware implementieren: Signaturprüfung, Idempotency-Key-Dedupe, Schema-Validierung, 2xx/4xx/Retry-Semantik, Dead-Letter/Logging.
