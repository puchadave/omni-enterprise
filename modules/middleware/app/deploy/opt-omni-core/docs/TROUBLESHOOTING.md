# Omni-Core Troubleshooting Guide

## Frequently Encountered Issues

### 1. Installation bricht bei "Abhängigkeiten prüfen" ab

**Symptom:**
```
E: Sperrdatei /var/lib/dpkg/lock-frontend konnte nicht geöffnet werden.
E: Erlangen der Sperre für die Dpkg-Oberfläche nicht möglich.
```

**Ursache:** Script wird nicht als Root ausgeführt und versucht Paketmanager zu sperren.

**Lösung:**
- Führen Sie das Script als Root (oder mit sudo) aus
- Oder setzen Sie `FORCE_SKIP_PACKAGES=true`, um die Paketinstallation zu überspringen
- Stellen Sie sicher, dass der Benutzer zur `docker` Gruppe gehört

```bash
# Benutzer zur Docker Gruppe hinzufügen
usermod -aG docker $USER
# Neue Sitzung starten (oder neu einloggen)
newgrp docker
```

### 2. PostgreSQL Verbindungsschwierigkeiten

**Symptom:**
```
PostgreSQL: NICHT erreichbar
```

**Mögliche Ursachen:**

a) Docker Compose Services nicht gestartet
```bash
# Lösung: Services neustarten
docker compose down
docker compose up -d
```

b) Falsche .env Konfiguration
```bash
# .env Datei prüfen und korrigieren
cat .env
# Sicherstellen dass POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB übereinstimmen
```

c) Container ist in Fehlerzustand
```bash
# Container neu starten
docker compose restart db
# Logs prüzen
docker compose logs db
```

### 3. Redis Verbindung fehlgeschlagen

**Symptom:**
```
Redis: NICHT erreichbar (kann optional sein)
```

**Lösung:**
```bash
# Redis Container Status prüfen
docker ps | grep redis

# Falls nicht laufend, neustarten
docker compose up -d redis

# Wartezeit geben
# Das Script warte automatisch 10 Sekunden auf Redis Ready
```

### 4. Backend API antwortet nicht

**Symptom:**
```
Backend API: NICHT erreichbar (http://localhost:8000/api/health)
```

**Lösungsschritte:**

a) Container Status prüfen
```bash
docker compose ps omni-core
# Sollte "Up  ... healthy" anzeigen
```

b) Port Mapping prüfen
```bash
# Port sollte von 8000:8000 sein
docker compose port omni-core 8000
```

c) Health Check deaktiviert oder fehlgeschlagen
```bash
# Manuell testen
curl -s http://localhost:8000/api/health
# Oder direkt im Container
docker exec -i omni-core-app python3 -c "from main import app; print('OK')"
```

d) Dienst neustarten
```bash
docker compose restart omni-core
```

### 5. Frontend lädt nicht

**Symptom:**
```
Frontend: NICHT erreichbar (http://localhost:3000)
```

**Lösung:**
```bash
# Frontend Build prüfen
ls -la apps/web/dist/  # Sollte existieren

# Falls nicht, neu bauen
cd apps/web
npm run build

# Oder gesamte Neuinstallation
bash scripts/wawi-bootstrap.sh install
```

### 6. Admin Login fehlgeschlagen

**Symptom:**
- Falsches Passwort akzeptiert nicht
- Login Loop

**Lösung:**
```bash
# Passwort zurücksetzen durch .env Löschen
rm .env
bash scripts/wawi-bootstrap.sh install

# Oder direktes Setzen in .env
# ADMIN_PASSWORD=<neues_passwort>
```

### 7. Matterhorn Import findet keine Artikel

**Symptom:**
- XML Download erfolgreich, aber keine Produkte importiert
- "16 Bytes" XML Inhalt (zu klein)

**Lösung:**
```bash
# Netzwerkverbindung zu Matterhorn prüfen
curl -s -o /dev/null -w "%{http_code}" https://matterhorn-moda.de/xmldata/products_full.php?type=xml

# Alternative Quelle testen
# In .env: MATTERHORN_URL=https://matterhorn-moda.de/xmldata/products_full_dev.php?type=products_xml_ver2

# Firewall oder Proxy prüfen
# Das Script hat bereits Timeout und Retry Mechanismen
```

### 8. OmniRoute Gateway nicht erreichbar

**Symptom:**
```
OmniRoute Gateway nicht direkt erreichbar (kann in LXC/Container sein)
```

**Lösung:**
```bash
# .env Datei prüfen
cat .env
# OMNI_ROUTE_URL und OMNI_ROUTE_API_KEY setzen

# Falls Gateway nicht verfügbar, wird Fallback verwendet
# Die KI-Funktionen stehen dann im "Fallback Modus"

# Gateway Test direkt:
python3 -c "from services.omniroute_client import OmniRouteClient; import os; client = OmniRouteClient(os.getenv('OMNI_ROUTE_URL', 'http://host.docker.internal:20128'), os.getenv('OMNI_ROUTE_API_KEY', 'test-key')); import asyncio; asyncio.run(client.check_health())"
```

### 9. Shopware Connector deaktiviert

**Symptom:**
```
Shopware Zugangsdaten nicht konfiguriert
Shopware Connector ist initialisiert, aber deaktiviert bis Zugangsdaten gesetzt
```

**Lösung:**
```bash
# .env Datei erweitern
# SHOPWARE_URL=https://your-shopware-instance.com
# SHOPWARE_API_KEY=your-api-key

# Nach .env Änderung neu starten
docker compose restart omni-core
# Oder neu installieren
bash scripts/wawi-bootstrap.sh install
```

### 10. Performance Probleme

**Symptome:**
- Langsame Antwortzeiten
- Hohe CPU-/Memennutzung

**Lösungen:**
```bash
# Docker Ressourcen prüfen
docker stats

# Container neu starten mit mehr Ressourcen
# docker compose up --build -d --scale omni-core=2

# Datenbank-Indizes prüfen und optimieren
# PostgreSQL ANALYZE durchführen

# BullMQ Queue Workers prüfen
# Dies ist Konfigurationsabhängig
```

## Emergency Recovery

### Vollständiger Neustart

```bash
# 1. Alle Container stoppen
docker compose down

# 2. Volumes und Datenbanken bereinigen (VORSICHT: Datenverlust!)
docker volume rm omni-core-middleware_pg_data 2>/dev/null || true
docker volume rm omni-core-middleware_app_data 2>/dev/null || true

# 3. Konfigurationsdateien sichern (falls benötigt)
cp .env /tmp/.env.backup

# 4. Vollständige Neuinstallation
bash scripts/wawi-bootstrap.sh install

# 5. System neu starten
bash scripts/wawi-bootstrap.sh start
```

### Logs analysieren

Wichtige Log-Orte:

```bash
# Docker Container Logs
docker compose logs -f omni-core

# Application Logs
cat /app/logs/backend.log  # Falls native Installation

# Nginx/Reverse Proxy Logs (falls vorhanden)
# /var/log/nginx/access.log
# /var/log/nginx/error.log
```

### Support Kontakt

Bei anhaltenden Problemen:

1. Führen Sie `bash scripts/wawi-bootstrap.sh doctor` aus
2. Erfassen Sie die vollständige Output
3. Prüfen Sie `docs/BOOTSTRAP.md` für bekannte Lösungen
4. Stellen Sie sicher dass alle Docker Container "healthy" Status haben
5. Überprüfen Sie die .env Datei auf korrekte Werte