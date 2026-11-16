# 08 – Betrieb & Monitoring

## Health & Checks
- Endpunkte: Middleware `/healthz` (bzw. `/health`), OmniRoute `GET /v1/models` (auth), DB `pg_isready`, Webdienste HTTP-Probes.
- Docker: `docker compose ps`, `docker ps`, Swarm `docker service ls/ps`, Logs `docker compose logs -f` / `docker logs`.
- Skripte: `scripts/smoke-test.sh`, `scripts/inventory.sh`, `scripts/check-secrets.sh`, `scripts/validate.sh`.

## Logging
- JSON-File Logging mit Rotation in Compose/Stacks (`max-size`, `max-file`). Für Produktion: zentraler Versand (z.B. Loki/ELK/Fluent Bit) und Aufbewahrung einplanen.

## Monitoring (Empfehlung)
- Metriken: cAdvisor/node-exporter + Prometheus + Alerting (Container-Status, CPU/RAM, Restarts, Disk/Volumes).
- Synthetics: regelmäßige Smoke-Checks (Health, Latenz) inkl. ZeroTier-Pfad/Peer-Erreichbarkeit.
- ZeroTier: `zerotier-cli info/listnetworks/listpeers` überwachen (Dienst/Link-Status) zur Partitionserkennung.

## Betriebsvorgaben
- Keine manuellen Änderungen an produktiven Containern/Volumes ohne Change-Plan.
- Updates: reproduzierbar (Tags/SHAs), erst Test, dann gestaffelt; Backups vorher.
- Zugriff: Admin-UIs (Portainer/Traefik) nur authentifiziert und bevorzugt über ZT/VPN; TLS am Edge.
