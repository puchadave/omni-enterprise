# Modul: Shopware 6 (für Omni-Core Middleware-Integration)

Schnellstartbares **Shopware 6**-Modul (Standard: `dockware/dev`, Dev/Demo inkl. integrierter DB) mit allem Nötigen zur Anbindung an die Middleware (Admin-API/OAuth2, keine DB-Direktzugriffe).

## Inhalt
- `docker-compose.yml` / `.env.example` – Shopware-Container (Port, URL, Bind-Host, ZT-Profil)
- `integration/connector.json.example` – Connector-Konfigurationsvorlage
- `integration/middleware-connection.example.json` – Payload für Middleware-Connection-API
- `scripts/create-integration.sh` – API-Integration in Shopware anlegen (CLI oder Admin-API)
- `scripts/register-connection.sh` – Connection in Middleware registrieren
- `scripts/wait-ready.sh` – Warten bis Shopware erreichbar
- `INTEGRATION.md` – vollständige Integrationsanleitung

## Quickstart
```bash
cp .env.example .env
./start.sh up
./scripts/wait-ready.sh   # http://localhost:8088 (Default)
```
**Dev/Demo (dockware):** Storefront `http://localhost:8088`, Admin oft `/admin` (Default-Credentials imageabhängig, häufig `admin`/`shopware` – nur Dev, in Prod ändern/absichern).

## Middleware-Integration (Kurzablauf)
1. **Integration anlegen:** `./scripts/create-integration.sh console --label omni-middleware` → `Access key ID` = `SHOPWARE_CLIENT_ID`, `Secret access key` = `SHOPWARE_CLIENT_SECRET` sicher notieren.
2. **`.env` setzen:** `SHOPWARE_URL` (für Middleware erreichbar, im gemeinsamen Netz z.B. `http://shopware:80`), `SHOPWARE_CLIENT_ID/SECRET`, optional `SHOPWARE_SALES_CHANNEL_ID`, `MIDDLEWARE_URL`.
3. **In Middleware registrieren:** `./scripts/register-connection.sh` (POST `/api/shopware/connections`).
4. **Testen:** `POST $MIDDLEWARE_URL/api/shopware/connections/<id>/test` oder UI `$MIDDLEWARE_URL/ui/shopware`.

Details: **[INTEGRATION.md](INTEGRATION.md)**.

## Netz & Multi-Device
- **Gemeinsames Docker-Netz/Root-Compose:** `SHOPWARE_URL=http://shopware:80` (Service-Name).
- **Separat/andere Hosts:** erreichbare IP/Host oder ZeroTier-IP + Port nutzen; `SERVICE_BIND_HOST` einschränken (Loopback/ZT-IP) und nur benötigte Ports/Flows freigeben.
- Optionaler ZT-Sidecar: `docker compose --profile zt up -d` (Host-ZT bevorzugt), siehe Root-Doku `docs/ZEROTIER_ARCHITECTURE.md`.

## Produktion
`dockware/dev` ist für Dev/Demo. Für Produktion eigenes Setup (Shopware prod image/build + MariaDB + Redis + ggf. OpenSearch), TLS/Hardening, Backups und offizielle Shopware-Hosting-Doku befolgen. Middleware bleibt API-only; produktive Credentials/URLs/Sales-Channel sauber konfigurieren und Exporte/Flows vorab testen.

## Wichtige Befehle
```bash
./start.sh up|down|logs|status|config|pull
./scripts/wait-ready.sh
./scripts/create-integration.sh console|api [...]
./scripts/register-connection.sh
```
