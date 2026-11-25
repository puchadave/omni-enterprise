# OMNI Enterprise - Server Setup Guide (Shopware Live Sale)

## Ziel
Reproduzierbare Erstinstallation für weitere Server mit Fokus auf **Shopware-Live-Verkauf**. Externe Marktplätze sind standardmäßig isoliert (`ENABLE_EXTERNAL_MARKETPLACES=false`) und können nach dem Live-Start separat aktiviert/entwickelt werden.

## Voraussetzungen
- Linux Server (Ubuntu/Debian empfohlen)
- Docker Engine + Docker Compose Plugin (`docker compose version`)
- `curl`, `python3`
- Offene Ports (Default): 8002 (Middleware), 20128 (OmniRoute), 8088 (Shopware), optional 9000/9443/80/443

## 1-Befehl Neuinstallation
```bash
cp .env.example .env
# Optional: Werte anpassen (Domains, Ports, Shopware API)
./scripts/setup-server.sh core,commerce
./scripts/healthcheck.sh core,commerce
```

`setup-server.sh` ist idempotent:
- erzeugt `.env` aus `.env.example`, falls fehlend
- generiert fehlende Secrets kryptografisch (`openssl rand -hex 32` / Python Fallback)
- setzt `.env` auf `chmod 600`
- prüft Ports
- startet Container (`docker compose --profile ... up -d --build`)
- wartet auf Healthchecks
- versucht Shopware Plugin Bootstrap (`plugin:refresh/install/activate/update/cache:clear`)

## Wichtige Konfiguration (.env)
- **Pflicht/Secrets**: `POSTGRES_PASSWORD`, `SESSION_SECRET`, `INTERNAL_API_KEY`, `SECRET_KEY`, `JWT_SECRET`, `OMNIROUTE_API_KEY`, `OMNIROUTE_STORAGE_ENCRYPTION_KEY`
- **Shopware**: `SHOPWARE_ENABLED=true`, `SHOPWARE_URL`, `SHOPWARE_CLIENT_ID`, `SHOPWARE_CLIENT_SECRET`, `SHOPWARE_SALES_CHANNEL_ID`
- **Live-Isolation**: `ENABLE_EXTERNAL_MARKETPLACES=false` (Default für Live-Start)
- **Import/AI**: `ENABLE_MATTERHORN_IMPORT=true`, `PRODUCT_INTELLIGENCE_ENABLED=true`, `EXPORT_GATE_MODE=review_required`

## Shopware Live-Verkauf Checkliste
1. `./scripts/healthcheck.sh core,commerce` -> `HEALTHCHECK_OK`
2. Middleware erreichbar: `http://<server>:8002/healthz`
3. Shopware erreichbar: `http://<server>:8088`
4. In Shopware Admin: OmniCoreConnector aktiviert, API-URL/API-Key gesetzt
5. Matterhorn Testimport (dry-run/safe) ausführen, Bilder/SEO/EAN prüfen
6. Go-Live Gates prüfen: keine unvollständigen Pflichtdaten, keine fehlenden Bilder, PAngV/Margen-Gates aktiv
7. Nur `channel_scope="shopware"` Workflows freigeben; externe Marktplätze bleiben aus

## Update / Rollout
```bash
./scripts/update-server.sh core,commerce
./scripts/healthcheck.sh core,commerce
```
`update-server.sh` erstellt Vorab-Backup unter `backups/<timestamp>/` (DB-Dump, redacted env metadata) und führt Pull/Build/Up + Shopware Cache/Migration-Bootstrap aus.

## Healthcheck
```bash
./scripts/healthcheck.sh core,commerce
```
Prüft Compose-Status, OmniRoute, Middleware, Shopware (wenn aktiv), Postgres readiness.

## Roadmap: Externe Marktplätze nach Shopware Live
1. Shopware 14 Tage stabil betreiben (Orders, Import, Gates, Audit)
2. `ENABLE_EXTERNAL_MARKETPLACES=true` nur in separater Staging-Umgebung setzen
3. Pro Marktplatz einzeln: Credentials, Mapping, Dry-Run, Audit, Rate-Limits, Rollback testen
4. Erst nach verifiziertem Staging einzeln produktiv schalten

## Sicherheit
- `.env` niemals committen (in `.gitignore` enthalten)
- Secrets rotieren bei Leak
- Nur notwendige Ports exponieren (`SERVICE_BIND_HOST`, Reverse Proxy/TLS)
- Keine destruktiven Docker-Kommandos in Produktivumgebung ohne Backup
