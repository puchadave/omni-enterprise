# Modul: Shopware 6 (für Omni-Core Middleware-Integration)

Schnellstartbares **Shopware 6**-Modul (Standard: `dockware/dev`, Dev/Demo inkl. integrierter DB) mit **automatischer Middleware-Integration**: beim Start wird eine Shopware-API-Integration erzeugt und (optional) in der Middleware registriert, damit die Middleware immer eine Verbindung aufbauen kann.

## Inhalt
- `docker-compose.yml` – Shopware + `shopware-bootstrap` (Auto-Integration, idempotent)
- `.env.example` – Ports/URLs, Integrations-Label, Middleware-Ziel, Bootstrap-Parameter, ZT
- `integration/connector.json.example`, `integration/middleware-connection.example.json` – Vorlagen
- `scripts/bootstrap-integration.sh` – **idempotente** Auto-Integration (Keys erzeugen/wiederverwenden, in `.env`/`integration/` persistieren, optional in Middleware registrieren)
- `scripts/bootstrap-loop.sh` – Retry-Wrapper (Init-Container)
- `scripts/create-integration.sh`, `scripts/register-connection.sh`, `scripts/wait-ready.sh`
- `INTEGRATION.md` – vollständige Integrationsanleitung

## Quickstart (Auto-Verbindung)
```bash
cp .env.example .env
# Wichtig für Auto-Registrierung: MIDDLEWARE_URL setzen (erreichbare Middleware)
# und SHOPWARE_URL so setzen, wie die MIDDLEWARE den Shop erreicht:
#   - gemeinsames Docker-Netz/Root-Compose: http://shopware:80
#   - separate Hosts: http(s)://<host-oder-zt-ip>:8088
./start.sh up
# Status/Keys: ./start.sh logs shopware-bootstrap
# Persistierte Credentials/Config: integration/credentials.env, integration/live/*.json
```
- `shopware-bootstrap` wartet auf Shopware, erstellt bei Bedarf die Integration (`omni-middleware`, Admin-API), speichert `SHOPWARE_CLIENT_ID/SECRET` in `integration/credentials.env` + aktualisiert `.env`, schreibt `integration/live/middleware-connection.json`, und registriert die Connection in `MIDDLEWARE_URL` (best-effort, Retry). **Idempotent**: vorhandene Credentials werden wiederverwendet.
- Manuell/erneut ausführen: `./start.sh bootstrap` (oder `./scripts/bootstrap-integration.sh --auto`).
- Dev/Demo (dockware): Storefront `http://localhost:8088`, Admin oft `/admin` (Default-Credentials imageabhängig, häufig `admin`/`shopware` – nur Dev, in Prod ändern/absichern).

## Middleware-Integration (Details)
- **Gemeinsames Docker-Netz/Root-Compose:** `SHOPWARE_URL=http://shopware:80` (Service-Name) – Middleware erreicht Shop intern.
- **Separat/andere Hosts:** erreichbare IP/Host oder ZeroTier-IP + Port; `SERVICE_BIND_HOST` einschränken und nur benötigte Ports/Flows freigeben.
- Relevante Middleware-Endpunkte: `POST/GET /api/shopware/connections`, `POST /api/shopware/connections/{id}/test`, `.../mappings`, Export, UI `$MIDDLEWARE_URL/ui/shopware`.
- Falls Auto-Registrierung nicht möglich (Middleware noch nicht erreichbar): Credentials liegen lokal vor → später `./scripts/register-connection.sh` oder `./start.sh bootstrap` erneut.

Details: **[INTEGRATION.md](INTEGRATION.md)**.

## Sicherheit & Hinweise
- Generierte Secrets: `integration/credentials.env`, `integration/live/` sind **gitignored** (nie committen). Rechte restriktiv; in Prod rotieren.
- Bestehende Integration ohne gespeichertes Secret: Secret nicht erneut abrufbar → in Shopware löschen/rotieren und Bootstrap erneut ausführen.
- Keine Shopware-DB-Direktzugriffe durch Middleware; nur offizielle API.

## Produktion
`dockware/dev` ist für Dev/Demo. Für Produktion eigenes Setup (Shopware prod image/build + MariaDB + Redis + ggf. OpenSearch), TLS/Hardening, Backups und offizielle Shopware-Hosting-Doku. Credentials/URLs/Sales-Channel sauber konfigurieren und Exporte/Flows vorab testen.

## Wichtige Befehle
```bash
./start.sh up|down|logs|status|config|pull
./start.sh bootstrap                 # Auto-Integration (idempotent)
./scripts/wait-ready.sh
./scripts/create-integration.sh console|api [...]
./scripts/register-connection.sh
```
