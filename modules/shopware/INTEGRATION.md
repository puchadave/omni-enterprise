# Shopware 6 – Middleware-Integration (vollständige Anleitung)

Diese Anleitung verbindet **Shopware 6** (dieses Modul) mit der **Omni-Core Middleware** über die offizielle Admin-API (OAuth2 Integration). Keine Direktzugriffe auf die Shopware-DB.

## Auto-Integration (empfohlen, „immer verbindbar“)
`./start.sh up` startet zusätzlich den Service **`shopware-bootstrap`**, der automatisch:
1. wartet bis Shopware bereit ist,
2. eine API-Integration (`omni-middleware`, Admin) erstellt **oder** bestehende gespeicherte Credentials wiederverwendet (idempotent),
3. Credentials in `integration/credentials.env` + `.env` persistiert und `integration/live/middleware-connection.json` erzeugt,
4. die Connection in der Middleware registriert (wenn `MIDDLEWARE_URL` erreichbar; best-effort mit Retries).

**Wichtig – URLs richtig setzen (damit Middleware verbinden kann):**
- `SHOPWARE_URL`: die Adresse, unter der die **Middleware** den Shop erreicht.
  - gemeinsames Docker-Netz/Root-Compose: `http://shopware:80`
  - getrennte Hosts/Module: `http(s)://<host-ip|dns|zt-ip>:8088`
- `MIDDLEWARE_URL`: erreichbare Middleware (z.B. `http://middleware:8000` im gemeinsamen Netz oder `http://<host>:8002`).

Manuell/erneut (idempotent): `./start.sh bootstrap` bzw. `./scripts/bootstrap-integration.sh --auto`.
Logs: `./start.sh logs shopware-bootstrap`. Artefakte: `integration/credentials.env`, `integration/live/*` (gitignored).

> Hinweis: Existiert die Integration in Shopware bereits, aber das Secret wurde nie gespeichert, ist es nicht erneut abrufbar. Lösung: Integration in Shopware löschen/rotieren und Bootstrap erneut ausführen.

## Voraussetzungen
- Shopware läuft und erreichbar (`./start.sh up`, `./scripts/wait-ready.sh`).
- Middleware läuft/erreichbar (Health: `/healthz`), sofern Auto-Registrierung gewünscht.
- Netzwerkpfad Middleware → `SHOPWARE_URL` offen (Docker-Netz/Host/ZT, Firewall/Ports).

## Manuelle Schritte (alternativ/Ergänzung)

### 1) Shopware starten
```bash
cp .env.example .env   # Ports/URLs anpassen
./start.sh up
./scripts/wait-ready.sh
```
Dev/Demo (dockware): Storefront/Admin unter `http://<host>:8088` bzw. `/admin` (Default imageabhängig, oft `admin`/`shopware` – **nur Dev, in Prod ändern**).

### 2) API-Integration anlegen (falls nicht automatisch)
**Access key ID = `SHOPWARE_CLIENT_ID`, Secret access key = `SHOPWARE_CLIENT_SECRET`** (sicher speichern).
- Konsole (empfohlen): `./scripts/create-integration.sh console --label omni-middleware`
- Admin UI: `Einstellungen > System > Integrationen > Integration anlegen` (Admin-API-Zugriff)
- Admin-API: `./scripts/create-integration.sh api --url http://localhost:8088 --admin-user admin --admin-pass shopware --label omni-middleware`

### 3) Sales Channel ID ermitteln (optional/je nach Flow)
Admin UI `Verkaufskanäle` oder `GET /api/sales-channel` (authentifiziert) → `SHOPWARE_SALES_CHANNEL_ID`.

### 4) Connection in Middleware registrieren
`.env` setzen (`SHOPWARE_URL`, `SHOPWARE_CLIENT_ID/SECRET`, `SALES_CHANNEL_ID`, `MIDDLEWARE_URL`) dann:
```bash
./scripts/register-connection.sh
# oder: curl -X POST $MIDDLEWARE_URL/api/shopware/connections -H 'Content-Type: application/json' -d @integration/middleware-connection.example.json
```

### 5) Verbindung testen & nutzen
```bash
curl -sS -X POST "$MIDDLEWARE_URL/api/shopware/connections/<ID>/test"
# UI: $MIDDLEWARE_URL/ui/shopware
```
Middleware-Endpunkte: `POST/GET /api/shopware/connections`, `POST /api/shopware/connections/{id}/test`, `.../mappings`, `POST/GET /api/shopware/export`, `/ui/shopware`.

## Netzwerk-Hinweise
- **Gemeinsames Netz:** `http://shopware:80` als `SHOPWARE_URL`.
- **Separat/ZeroTier:** erreichbare IP/Host/ZT-IP+Port, `SERVICE_BIND_HOST`/Firewall/ZT-Flow-Rules passend, in Prod `https://` (TLS/Edge).

## Konfigurationsdateien
- `integration/connector.json.example` – Connector-Vorlage (ohne Secrets committen)
- `integration/middleware-connection.example.json` – Request-Payload Vorlage
- `integration/credentials.env`, `integration/live/*` – **generiert, gitignored**

## Sicherheit & Betrieb
- Secrets nie committen/teilen; in Prod rotieren, Rechte minimal halten.
- Keine Shopware-DB-Direktzugriffe durch Middleware; nur offizielle API.
- Vor Produktivbetrieb: gültige Credentials, korrektes Sales-Channel/Steuer/Währung-Mapping, Test-Export/Order-Flow verifizieren.
