# Shopware 6 – Middleware-Integration (vollständige Anleitung)

Diese Anleitung verbindet **Shopware 6** (dieses Modul) mit der **Omni-Core Middleware** über die offizielle Admin-API (OAuth2 Integration). Keine Direktzugriffe auf die Shopware-DB.

## Voraussetzungen
- Shopware läuft und ist erreichbar (siehe `README.md` / `./start.sh up`).
- Middleware läuft und ist erreichbar (z.B. `http://localhost:8002`, Health: `/healthz`).
- Netzwerkpfad: Middleware muss `SHOPWARE_URL` erreichen (bei gemeinsamem Docker-Netz z.B. `http://shopware:80`, sonst Host/IP/ZeroTier-IP).

## 1) Shopware starten
```bash
cd modules/shopware
cp .env.example .env   # Ports/URL ggf. anpassen
./start.sh up
./scripts/wait-ready.sh
```
- Dev/Demo (dockware): Storefront/Admin je nach Image unter `http://<host>:8088` bzw. `/admin`. Default-Admin (imageabhängig, nur Dev): oft `admin` / `shopware` – **in Prod ändern**.

## 2) API-Integration in Shopware anlegen
Integration mit Admin-API-Zugriff erzeugen; **Access key ID** = `SHOPWARE_CLIENT_ID`, **Secret access key** = `SHOPWARE_CLIENT_SECRET` (Secret nur einmal sichtbar → sicher speichern).

### Option A – per Konsole (empfohlen)
```bash
./scripts/create-integration.sh console --label omni-middleware
# Ausgabe enthält Access key ID / Secret access key
```

### Option B – per Admin UI
`Einstellungen > System > Integrationen > Integration anlegen` → Name vergeben, „Administrator“-Rechte (oder minimal nötige Rechte) aktivieren → Schlüssel notieren.

### Option C – per Admin-API Script
```bash
./scripts/create-integration.sh api --url http://localhost:8088 --admin-user admin --admin-pass shopware --label omni-middleware
```

## 3) Sales Channel ID ermitteln
Benötigt für Storefront-Kontext/Zuordnung (falls Middleware/Flow es erfordert):
- Admin UI: `Verkaufskanäle` → Kanal öffnen → ID aus URL/Details, oder
- API: `GET /api/sales-channel` (authentifiziert) und passende ID wählen.

## 4) Connection in der Middleware registrieren
Werte in `modules/shopware/.env` setzen (`SHOPWARE_URL`, `SHOPWARE_CLIENT_ID`, `SHOPWARE_CLIENT_SECRET`, `SHOPWARE_SALES_CHANNEL_ID`, `MIDDLEWARE_URL`) und registrieren:
```bash
./scripts/register-connection.sh
```
Oder manuell:
```bash
curl -sS -X POST "$MIDDLEWARE_URL/api/shopware/connections" \
  -H 'Content-Type: application/json' \
  -d @integration/middleware-connection.example.json
```
Relevante Middleware-Endpunkte (siehe Implementierung): `POST/GET /api/shopware/connections`, `POST /api/shopware/connections/{id}/test`, `.../mappings`, `POST/GET /api/shopware/export`, `/ui/shopware`.

## 5) Verbindung testen & Export
```bash
# Connection testen (ID aus Registrierung)
curl -sS -X POST "$MIDDLEWARE_URL/api/shopware/connections/<CONNECTION_ID>/test"
# UI
open "$MIDDLEWARE_URL/ui/shopware"
```
Danach Produkte/Mappings/Export über Middleware-UI oder -API ausführen (nur mit gültigen Credentials und bestätigtem Ziel).

## Netzwerk-Hinweise (Standalone vs. gemeinsam)
- **Gemeinsames Compose/Netz (empfohlen):** Middleware erreicht Shopware unter `http://shopware:80` (Service-Name) → `SHOPWARE_URL=http://shopware:80`.
- **Separate Module/Hosts:** erreichbare IP/Host/ZeroTier-IP + Port verwenden (z.B. `http://192.168.178.x:8088` oder `http://<zt-ip>:8088`), `SERVICE_BIND_HOST`/Firewall/ZT-Flow-Rules passend setzen.
- **TLS:** In Prod `https://`-Shop-URL verwenden (Reverse Proxy/Edge).

## Konfigurationsdateien (Vorlagen)
- `integration/connector.json.example` – Connector-Konfigurationsvorlage (ohne Secrets committen)
- `integration/middleware-connection.example.json` – Payload für Middleware-Connection

## Sicherheit & Betrieb
- Secrets (`CLIENT_SECRET`, API-Keys) nie committen/teilen; in Prod rotieren und Rechte minimal halten.
- Keine Shopware-DB-Direktzugriffe durch Middleware; nur offizielle API.
- Vor Produktivschaltung: gültige Credentials, korrektes Sales-Channel/Steuer/Währung-Mapping, Test-Export/Order-Flow verifizieren.
