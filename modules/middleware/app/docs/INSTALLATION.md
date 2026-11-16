# Omni-Core Installation Guide

## Prerequisites

### Hardware Requirements

| Komponente | Minimum | Empfohlen |
|------------|---------|-----------|
| CPU-Kerne | 2 | 4+ |
| Arbeitsspeicher | 2 GB | 4+ GB |
| Festplattenspeicher | 2 GB | 5+ GB |
| Netzwerk | beliebig | stabiler Internetzugang |

### Software Requirements

- **Betriebssystem:** Linux (Alpine, Debian, Ubuntu, Zorin OS)
- **Docker:** Mindestens Version 20.10
- **Docker Compose:** Mindestens Version 2.0
- **PostgreSQL:** 15+ (falls nativ, nicht Docker)
- **Redis:** 7+ (falls nativ, nicht Docker)

### Infrastruktur-Anforderungen

- Port 8000 für Backend API
- Port 3000 für Frontend
- Port 20128 für OmniRoute Gateway (optional, kann konfiguriert werden)
- Outbound Internetzugang für Matterhorn XML Download

## Installation Schrittschritt

### Schritt 1: Repository klonen

```bash
git clone https://github.com/puchadave/omni-core-middleware.git
cd omni-core-middleware
```

### Schritt 2: Bootstrap Script ausführen

#### Option A: Interaktiv (Empfohlen für erste Installation)

```bash
bash scripts/wawi-bootstrap.sh install
```

Während der Installation werden Sie aufgefordert:
- Admin-E-Mail-Adresse zu bestätigen
- Admin-Passwort festzulegen

#### Option B: Nicht-interaktiv (Für Skripte/CI)

```bash
INTERACTIVE=false \
ADMIN_EMAIL=admin@yourcompany.com \
ADMIN_PASSWORD=YourSecurePassword123 \
bash scripts/wawi-bootstrap.sh install
```

### Schritt 3: System starten

Nach der Installation:

```bash
bash scripts/wawi-bootstrap.sh start
```

Oder falls Docker Compose direkt verwendet werden soll:

```bash
docker compose up -d
```

### Schritt 4: WebIF erreichen

Nach dem Start ist das Web Interface unter folgenden Adressen erreichbar:

- **Backend API:** `http://<IP>:8000`
- **API Dokumentation:** `http://<IP>:8000/docs`
- **Frontend:** `http://<IP>:3000`

Standard-Login:
- **E-Mail:** admin@omni-core.local (oder konfiguriert)
- **Passwort:** Bei erster Anmeldung ändern

### Schritt 5: Matterhorn Import einrichten

1. Gehen Sie im WebIF zum "Import Management" Bereich
2. Geben Sie die Matterhorn API falls vorhanden ein (optional)
3. Klicken Sie auf "Import starten" für die erste Produktladerung
4. Prüfen Sie die Import-Ergebnisse im Dashboard

### Schritt 6: KI Gateway konfigurieren (optional)

1. Bearbeiten Sie die `.env` Datei
2. Setzen Sie `OMNI_ROUTE_API_KEY` falls verfügbar
3. Konfigurieren Sie Modelle im WebIF unter "KI-Einstellungen"

### Schritt 7: Shopware Integration (optional)

1. Bearbeiten Sie die `.env` Datei
2. Setzen Sie `SHOPWARE_URL` auf Ihre Shopware Instanz
3. Setzen Sie `SHOPWARE_API_KEY` mit gültigen Berechtigungen
4. Aktivieren Sie den Shopware Connector im WebIF

## Verwendete Docker Compose Dienste

Nach der Installation sind folgende Dienste verfügbar:

```yaml
services:
  db:
    image: postgres:15-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: ${POSTGRES_DB}
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"]
      interval: 5s
      timeout: 3s
      retries: 20
      start_period: 10s

  omni-core:
    build:
      context: .
      dockerfile: Dockerfile
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@db:5432/${POSTGRES_DB}
      OMNIROUTE_URL: ${OMNI_ROUTE_URL:-http://host.docker.internal:20128}
      OMNIROUTE_API_KEY: ${OMNI_ROUTE_API_KEY:-test-key}
    ports:
      - "${OMNI_CORE_PORT:-8000}:8000"
    volumes:
      - app_data:/app/data
    healthcheck:
      test: ["CMD-SHELL", "wget -q -O /dev/null http://127.0.0.1:8000/healthz || exit 1"]
      interval: 10s
      timeout: 3s
      retries: 12
      start_period: 20s
```

## PostgreSQL Zugriff (falls benötigt)

Direkter PostgreSQL-Zugriff (falls nötig):

```bash
# Über Docker
docker exec -i <container_name> psql -U omnichat -d omnicore

# Oder direkt wenn PostgreSQL nativ läuft
psql "postgresql://omnichat:<password>@localhost:5432/omnicore"
```

## Redis Zugriff

```bash
# Über Docker
docker exec -i wawi_redis.1.* redis-cli ping

# Oder über das Python Modul
redis-cli -h localhost -p 6379 ping
```

## Häufige Probleme und Lösungen

### Problem 1: "Docker ist nicht verfügbar"

**Lösung:** Docker installieren und Dienst starten

**Alpine:**
```bash
apk add docker-cli docker-compose
rc-service docker start
```

**Debian/Ubuntu:**
```bash
apt-get update
apt-get install -y docker.io docker-compose-v2
systemctl start docker
systemctl enable docker
```

### Problem 2: "PostgreSQL ist nicht erreichbar"

**Lösung:** Prüfen ob Docker Compose läuft

```bash
docker compose ps db
# Sollte "Up" anzeigen

# Falls nicht, neu starten
docker compose down
docker compose up -d db
```

### Problem 3: "Redis ist nicht erreichbar"

**Lösung:** Redis Container prüfen

```bash
docker ps | grep redis
# Sollte einen laufenden Redis Container zeigen

# Falls nicht, neu starten
docker compose up -d redis
```

### Problem 4: "Backend API antwortet nicht"

**Lösung:** Prüfen ob Container läuft und Port mapped

```bash
docker compose ps omni-core
# Sollte "Up" und "healthy" anzeigen

# Port forwarding prüfen
docker compose port omni-core 8000
```

### Problem 5: "Admin Login fehlgeschlagen"

**Lösung:** Passwort zurücksetzen

```bash
# .env Datei löschen und neu installieren
rm .env
bash scripts/wawi-bootstrap.sh install
```

### Problem 6: "Matterhorn XML Download fehlgeschlagen"

**Lösung:** Netzwerk prüfen und URL konfigurieren

```bash
# URL in .env anpassen
MATTERHORN_URL=https://matterhorn-moda.de/xmldata/products_full.php?type=xml

# Oder alternative Quelle nutzen
MATTERHORN_URL=https://matterhorn-moda.de/xmldata/products_full_dev.php?type=products_xml_ver2
```

## Update / Upgrade

```bash
# System aktualisieren
bash scripts/wawi-bootstrap.sh update
```

Dies führt folgende Aktionen aus:
1. Abhängigkeiten prüfen und aktualisieren
2. Neue .env Standards setzen (bestehende erhalten bleiben)
3. Migrationen ausführen
4. Dienste neustarten

##rollback

Im Fehlerfall können Sie folgende Schritte durchführen:

```bash
# Container stoppen und entfernen
docker compose down

# Volumes bereinigen (vorsichtig!)
docker volume rm pg_data app_data

# Neustart von vorne
bash scripts/wawi-bootstrap.sh install
```

**Warnung:** Das Löschen von Bereinigen von Volumes führt zum Verlust aller gespeicherten Daten!