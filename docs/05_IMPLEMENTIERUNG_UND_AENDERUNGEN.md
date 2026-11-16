# 05 – Implementierung & Änderungen (durchgeführt)

## Durchgeführt (sicher, ohne Eingriff in Live-Prod)
- **Inventar/Docs:** `docs/01_INVENTORY_IST.md`–`04_MODUL_SERVICE_MATRIX.md`, `ZEROTIER_ARCHITECTURE.md`, `shared/networks-and-addressing.md`, `secrets/CHECKLIST.md`, `secrets/templates/.env.secrets.example`.
- **ZeroTier-Integration (Multi-Device/Standalone):** allen Modulen + Root-Compose + Swarm-Env Variablen hinzugefügt (`ZT_ENABLED`, `ZT_NETWORK_ID`, `ZT_NODE_NAME`, `ZEROTIER_IMAGE`, `SERVICE_BIND_HOST`); optionaler `zerotier`-Sidecar-Service als Profil `zt` (host network, TUN/Caps, persistent identity volume, healthcheck) – **opt-in**, Host-Installation bevorzugt (`scripts/zerotier-host-setup.sh`).
- **Netz/Ports:** Port-Publishes parameterisiert via `${SERVICE_BIND_HOST:-0.0.0.0}` (erlaubt Bind an Loopback/ZT-IP für Härtung in Multi-Device-Setups). Traefik `api.insecure` in Root/Edge standardmäßig auf `false` konfigurierbar.
- **Resilienz (bestehend/ausgebaut):** `restart: unless-stopped`, Healthchecks, `depends_on: service_healthy` (core), parametrisierbare Bindings; Doku zu Timeouts/Retries/Degradation/Failure-Domains.
- **Skripte (idempotent, read-only/sicher):** `scripts/inventory.sh`, `check-secrets.sh`, `validate.sh`, `smoke-test.sh`, `backup-volumes.sh` (kein Delete), `zerotier-host-setup.sh`.
- **Konfiguration:** `.env.example` Dateien erweitert (Platzhalter/keine echten Secrets), `.gitignore` schützt `.env`/Secrets/Runtime/Backups.

## Nicht durchgeführt (bewusst / blockiert)
- **Keine** Stop/Delete/Rebuild-Aktionen auf produktiven Containern/Volumes/Netzen (`310b4d4eb7c1`, `1c51e239de30`, wawi/portainer live).
- **Keine** automatische ZeroTier-Netz-Erstellung/Device-Autorisierung (benötigt Freigabe/Netz-ID/Controller).
- **Keine** produktiven Secrets gesetzt/rotiert, kein Swarm-Deploy/Rollout auf pve01–pve04 ohne Verifikation, keine DB-Migrationen auf Prod.
- **Shopware/Vinted** produktiv nicht angebunden (Credentials/Zugang fehlen; Vinted capability-gesteuert `config_required`).

## Hinweise zur Nutzung (ZeroTier, standalone multi-device)
1. Pro Gerät Host-ZT installieren & Netzwerk joinen: `./scripts/zerotier-host-setup.sh --network <ZT_NETWORK_ID>` → in Controller autorisieren, ZT-IP notieren.
2. Pro Modul `.env` setzen: Peer-URLs auf ZT-IPs (`OMNIROUTE_URL=http://<ZT_IP>:20128/v1` etc.), starke Secrets/Keys, ggf. `SERVICE_BIND_HOST` restriktiv + ZT-Flow-Rules.
3. Start: `./start.sh up` (ohne `--profile zt` wenn Host-ZT genutzt wird). Alternativ opt-in Sidecar: `docker compose --profile zt up -d` (erfordert `/dev/net/tun` & Caps).
