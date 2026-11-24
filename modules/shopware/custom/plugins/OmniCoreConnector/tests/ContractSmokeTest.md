# Contract smoke checks (manual/container)

- `bin/console plugin:refresh && plugin:install --activate OmniCoreConnector`
- `bin/console omnicore:connector:health` -> db ok, config flags visible
- `bin/console debug:router | grep omnicore` -> health route registered
- Configure system config and trigger product/order write in Shopware admin/API; verify middleware receives signed POST with headers:
  - `X-Omni-Event`, `X-Omni-Idempotency-Key`, `X-Omni-Signature: sha256=...`
- Replay same event: expect dedupe (`duplicate`) by idempotency key in `omnicore_connector_event_outbox`.
