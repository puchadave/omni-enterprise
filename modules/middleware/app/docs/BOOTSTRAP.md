# Omni-Core Bootstrap Documentation

## Overview

The `wawi-bootstrap.sh` script provides a complete automated installation, configuration, and startup process for the Omni-Core ERP & BI Middleware system.

### Purpose

- Automated system detection and dependency installation
- Environment configuration with secure secret generation
- Database initialization (PostgreSQL + Redis)
- Matterhorn XML import service setup
- AI Pipeline (OmniRoute Gateway) configuration
- Shopware integration initialization
- Web administrative interface (WebIF) setup
- Health checks and status reporting

### Installation Modes

```bash
# Vollinstallation
bash scripts/wawi-bootstrap.sh install

# System starten
bash scripts/wawi-bootstrap.sh start

# System stoppen
bash scripts/wawi-bootstrap.sh stop

# Status anzeigen
bash scripts/wawi-bootstrap.sh status

# Systemdiagnose
bash scripts/wawi-bootstrap.sh doctor

# Abhängigkeiten prüfen
bash scripts/wawi-bootstrap.sh check

# Datenbankmigrationen
bash scripts/wawi-bootstrap.sh migrate

# Matterhorn Verbindung testen
bash scripts/wawi-bootstrap.sh test-matterhorn

# KI Pipeline initialisieren
bash scripts/wawi-bootstrap.sh ai-pipeline

# Shopware Integration initialisieren
bash scripts/wawi-bootstrap.sh shopware
```

### Systemerkennung

Das Script erkennt automatisch:

| Erkennung | Methode |
|-----------|---------|
| Betriebssystem | `/etc/os-release` |
| Docker verfügbar | `command -v docker` |
| Container-Umgebung | Prozess-CGroup Prüfung |
| CPU-Kerne | `nproc` |
| Gesamter RAM | `free -m` |
| Verfügbarer Speicher | `df -BG` |
| Belegte Ports | `ss -tlnp` |

Unterstützte Betriebssysteme:
- **Alpine Linux** (`apk` Paketmanager)
- **Debian/Ubuntu/Zorin** (`apt` Paketmanager)

### Abhängigkeitsinstallation

Installierte Pakete je nach OS:

**Alpine:**
```bash
apk add bash curl wget git openssl python3 py3-pip postgresql-client redis docker-cli docker-compose nodejs npm
```

**Debian/Ubuntu/Zorin:**
```bash
apt-get install -y bash curl wget git openssl python3 python3-pip postgresql-client redis-server docker-compose nodejs npm
```

### Umgebungskonfiguration

Automatisch erstellte `.env` Datei mit folgenden Sektionen:

#### Datenbank
```
POSTGRES_USER=omnichat
POSTGRES_PASSWORD=<generiert>
POSTGRES_DB=omnicore
```

#### OmniRoute Gateway
```
OMNI_ROUTE_URL=http://host.docker.internal:20128
OMNI_ROUTE_API_KEY=test-key
```

#### Server
```
OMNI_CORE_PORT=8000
FRONTEND_PORT=3000
```

#### Administration
```
ADMIN_EMAIL=admin@omni-core.local
ADMIN_PASSWORD_HASH=<sha256>
```

#### KI und Features
```
SECRET_KEY=<32-zeichen-hex>
JWT_SECRET=<64-zeichen-hex>
ENABLE_MARKETING_ENGINE=true
ENABLE_SALES_AUTOMATION=true
ENABLE_CHANNEL_ADAPTERS=true
ENABLE_MATTERHORN_IMPORT=true
```

### Administrator-Initialisierung

Der Admin-Zugang kann auf zwei Wegen konfiguriert werden:

1. **Interaktiv:**
   ```bash
   bash scripts/wawi-bootstrap.sh install
   # Wird nach Passwort gefragt
   ```

2. **Ümgebungsvariablen (nicht-interaktiv):**
   ```bash
   INTERACTIVE=false \
   ADMIN_EMAIL=admin@company.com \
   ADMIN_PASSWORD=SecurePass123 \
   bash scripts/wawi-bootstrap.sh install
   ```

Passwort-Hashing erfolgt mittels SHA-256. Standardpasswörter werden nicht verwendet.

### Datenbank-Initialisierung

#### PostgreSQL

- Datenbankverfügbarkeit wird geprüft
- Datenbank wird bei Bedarf erstellt
- Benutzer und Berechtigungen werden eingerichtet
- Verbindung wird getestet
- Drizzle-Migrationen werden ausgeführt (falls vorhanden)
- Schema wird validiert

Das Script ist idempotent - existiert die Datenbank bereits, werden keine doppelten Einträge erstellt.

#### Redis

- Verbindung wird geprüft
- BullMQ wird initialisiert
- Queue-Konfiguration wird überprüft
- Worker-Konfiguration wird validiert

### Matterhorn Import Initialisierung

**Standard-Quelle:**
```
https://matterhorn-moda.de/xmldata/products_full.php?type=xml
```

**Entwicklungs-Quelle:**
```
https://matterhorn-moda.de/xmldata/products_full_dev.php?type=products_xml_ver2
```

**Funktionen:**
- XML-Download und -Parser
- Import-Scheduler
- Import-Queue
- Datenvalidierung
- SKU-Mapping
- EAN-Mapping
- Hersteller-Mapping
- Kategorie-Mapping
- Größen-Mapping
- Farb-Mapping
- Preis-Mapping
- Bestands-Mapping
- Bildimport
- Duplikaterkennung
- Aktualisierung bestehender Artikel

**Wichtig:** Der Bootstrap startet keinen automatischen Import. Er initialisiert den Importdienst, testet die Verbindung und schaltet den ersten Import im WebIF frei.

### KI Pipeline Initialisierung

Konfiguration für das OmniRoute Gateway:

- **API-Endpunkt:** `OMNI_ROUTE_URL` (aus .env konfigurierbar)
- **API-Key:** `OMNI_ROUTE_API_KEY`
- **Verfügbare Modelle:** Wird bei Health Check automatisch entdeckt
- **Modell-Routing:** Konfigurierbar pro Anfrage
- **Fallback-Modelle:** Bei Hauptmodell-Ausfall aktivierbar
- **Timeout:** 30 Sekunden standardmäßig
- **Retry-Mechanismus:** 3 Versuche bei Transient-Fehlern
- **Rate Limits:** Wird von Gateway bereitgestellt

Die KI-Pipeline wird initialisiert und ihre Erreichbarkeit wird geprüft. Fehlende Zugangsdaten werden im WebIF als "nicht konfiguriert" angezeigt.

### Shopware Integration

**Initialisierung:**
- Shopware API Connector wird konfiguriert
- API-Authentifizierung wird eingerichtet
- Zugangsdatenverwaltung im .env File
- Produktmapping wird initialisiert
- Kategoriezuordnung wird eingerichtet
- Variantenmapping wird konfiguriert
- Medienmapping wird prepariert
- Preisübertragung wird konfiguriert
- Bestandsübertragung wird konfiguriert
- Synchronisationsqueue wird erstellt
- Fehlerprotokollierung wird aktiviert

**Wichtig:** Die eigentliche Shopware-Synchronisierung darf erst nach erfolgreicher Einrichtung der Zugangsdaten aktiviert werden. Ohne konfigurierte SHOPWARE_URL und SHOPWARE_API_KEY ist der Connector deaktiviert.

### WebIF Initialisierung

Nach dem Bootstrap ist das administrative Control Center erreichbar:

**Zustände:**
- **GREEN:** Betriebsbereit - Alle Systeme funktionieren
- **YELLOW:** Teilweise konfiguriert - Einige Komponenten noch nicht eingerichtet
- **RED:** Kritischer Fehler - Dienstleistung eingeschränkt
- **BLUE:** Initialisierung läuft - System startet gerade

**Bereitzustellende Zustände:**
- Backend API Status
- Frontend Status
- Datenbankverbindung
- Redis-Verbindung
- Importverwaltung
- Produktverwaltung
- KI-Einstellungen
- Plattformverwaltung
- Shopware-Verwaltung
- Preisberechnungscenter
- Audit-Logs

### Service Management

#### Docker

Alle Dienste werden via Docker Compose verwaltet:

```bash
# Container starten
docker compose up -d

# Container stoppen
docker compose down

# Neustart
docker compose restart

# Logs anzeigen
docker compose logs -f omni-core
```

**Restart Policies:** `unless-stopped` (in docker-compose.yml definiert)

#### Native Installation

Verwendung von Prozessmanager entsprechend der Architektur:

**Alpine LXC:**
- OpenRC wird berücksichtigt
- Keine unnötige systemd-Abhängigkeit

**Start/Stop:**
```bash
# Start
nohup python3 main.py --host 0.0.0.0 --port 8000 > /app/logs/backend.log 2>&1 &

# Stop
pkill -f "python3 main.py"
```

### Health Checks

Nach dem Start werden folgende Komponenten geprüft:

1. **PostgreSQL** - Datenbank erreichbar?
2. **Redis** - Cache/Service erreichbar?
3. **Backend API** - FastAPI antwortet auf `/api/health`?
4. **Frontend** - React App erreichbar?
5. **API** - Generelle Erreichbarkeit
6. **Migrationen** - Datenbankschema gültig?
7. **BullMQ** - Queue System funktionsfähig?
8. **Importengine** - Matterhorn Verbindung initialisiert?
9. **Matterhorn XML** - XML Quelle erreichbar?
10. **Omniroute Gateway** - KI Gateway konfiguriert?
11. **Shopware Connector** - API Zugangsdaten gesetzt?
12. **Admin Authentifizierung** - Login funktioniert?

### Dynamische Zugangsinformationen

Nach erfolgreicher Installation werden folgende Informationen automatisch ausgegeben:

```
Interne IP:       192.168.178.27
Externe IP:       78.94.233.153
WebIF Adresse:    http://192.168.178.27:8000
API Adresse:      http://192.168.178.27:8000/docs
API Port:         8000
Frontend Port:    3000
PostgreSQL:       läuft
Redis:            läuft
KI-Gateway:       konfiguriert (OmniRoute)
Shopware:         nicht konfiguriert

Admin Zugang:     admin@omni-core.local
Passwort:         [nicht in Logs, bei erster Anmeldung ändern]
```

### Installationsmodi

Ausführliche Hilfe verfügbar via:
```bash
bash scripts/wawi-bootstrap.sh help
```

### Fehlertbehandlung

- **Idempotenz:** Mehrfacher Ausführen erzeugt keine doppelten Datenbanken, Benutzer oder Container
- **Rollback:** Bei fehlgeschlagenen Schritten werden teilweise erstellte Ressourcen bereinigt
- **Temporäre Dateien:** Werden im `/tmp`-Verzeichnis erstellt und sicher entfernt
- **Abbruch bei kritischen Fehlern:** Script bricht bei unbehebbaren Problemen ab
- **Konflikte:** Bestehende Konfigurationen werden nicht überschrieben ohne ausdrückliche Erlaubnis (`FORCE_RESET=true`)

### Produktionseinsatz

Für die Produktion empfohlen:

1. `.env` Datei mit echten Werten statt Defaults konfigurieren
2. `ADMIN_PASSWORD` auf ein sicheres Passwort setzen
3. `OMNI_ROUTE_API_KEY` mit gültigem Gateway Key versehen
4. `SHOPWARE_URL` und `SHOPWARE_API_KEY` für die Integration setzen
5. Firewall-Regeln für Ports 8000 (Backend) und 3000 (Frontend) konfigurieren
6. Regelmäßige Health Checks einrichten
7. Docker-Restart Policies `unless-stopped` sicherstellen

### Verwandte Dateien

- `scripts/wawi-bootstrap.sh` - Hauptbootstrap Script
- `docs/INSTALLATION.md` - Detaillierte Installationsanleitung
- `docs/TROUBLESHOOTING.md` - Bekannte Probleme und Lösungen
- `docs/SYSTEM-REQUIREMENTS.md` - Systemanforderungen
- `.env` - Umgebungskonfiguration (wird generiert)
- `docker-compose.yml` - Docker Service Definitionen
- `Dockerfile` - Container Image Build Definition