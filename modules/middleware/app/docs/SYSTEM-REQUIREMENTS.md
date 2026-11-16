# Omni-Core System Requirements

## Hardware Requirements

### Minimum Requirements

| Komponente | Minimum Spezifikation |
|------------|----------------------|
| CPU | 2 Kerne (x86_64 oder arm64) |
| Arbeitsspeicher | 2 GB RAM |
| Speicherplatz | 2 GB verfügbarer Platz auf SSD oder schnellem HDD |
| Netzwerk | Ethernet oder WLAN mit stabiler Verbindung |
| Swap | Optional, mind. 1 GB für optimale PostgreSQL Leistung |

### Empfohlene Produktion

| Komponente | Empfohlene Spezifikation |
|------------|------------------------|
| CPU | 4+ Kerne |
| Arbeitsspeicher | 8 GB+ RAM |
| Speicherplatz | 10+ GB SSD |
| Netzwerk | Gigabit Ethernet, statische IP empfohlen |
| Swap | 2+ GB oder ausreichend RAM |

### Docker-spezifische Anforderungen

| Ressource | Minimum | Empfohlen |
|-----------|---------|-----------|
| Docker Engine | 20.10+ | 24.0+ |
| Docker Compose | 2.0+ | 2.20+ |
| Arbeitsspeicher für Container | 512 MB | 2 GB+ |
| Speicherplatz für Container-Images | 2 GB | 5 GB+ |

## Operating System Requirements

### Unterstützte Distributionen

| OS | Paketmanager | Unterstützt |
|----|-------------|-------------|
| Alpine Linux 3.16+ | apk | Ja (getestet) |
| Debian 11/12 | apt | Ja |
| Ubuntu 20.04/22.04/24.04 | apt | Ja |
| Zorin OS 16/17 | apt | Ja |

### Alpine Linux Spezifika

- Benötigt `musl`-kompatible Binaries
- `apk` Paketmanager wird verwendet
- Python 3.11 Alpine Build verfügbar
- Musl libc statt glibc

### Debian/Ubuntu Spezifika

- `apt-get` Paketinstallation
- systemd optional (für native Installationen)
- `docker.io` und `docker-compose-v2` Pakete

## Software Abhängigkeiten

### Erforderliche Pakete

| Paket | Version | Zweck |
|-------|---------|-------|
| Bash | 5.0+ | Script Execution |
| Git | 2.30+ | Repository Management |
| curl | 7.79+ | HTTP Requests |
| wget | 1.20+ | File Downloads |
| openssl | 1.1+ | Generierung von Secrets |
| Python 3.11 | 3.11.0+ | Backend Applikation |
| pip / pip3 | 21.0+ | Python Package Installation |
| Node.js | 18+ | Frontend Build |
| npm | 9+ | Node Package Manager |

### Optionale Pakete (für native Installation)

| Paket | Zweck |
|-------|-------|
| systemd | Service Management (native Linux) |
| openrc | Service Management (Alpine LXC) |
| supervisord | Prozess Management |
| fail2ban | Sicherheit (optional) |

## Docker Anforderungen

### Docker Compose Datei

Die `docker-compose.yml` erfordert folgende Services:

1. **PostgreSQL Datenbank** (`db`)
   - Image: `postgres:15-alpine`
   - Volatile Datenpersistenz via Volume `pg_data`
   - Healthcheck: `pg_isready`
   - Port: 5432 (intern), gemappt auf Host

2. **Omni-Core Applikation** (`omni-core`)
   - Build: `Dockerfile` (Python 3.11 Alpine)
   - Abhängigkeiten aus `requirements.txt`
   - Verknüpfung zur Datenbank via `DATABASE_URL`
   - Environment Variablen:
     - `DATABASE_URL`
     - `OMNIROUTE_URL`
     - `OMNIROUTE_API_KEY`
     - `MATTERHORN_API_TOKEN`
   - Healthcheck: `wget` auf `/api/health`
   - Port: 8000 (intern), nach außen gemappt

3. **Redis** (implizit via BullMQ)
   - Wird von Docker Compose in neueren Setups mitverwaltet
   - Oder separate Installation

### Port Anforderungen

| Port | Richtung | Zweck |
|------|----------|-------|
| 8000 | Eingehend | Backend API (FastAPI/UVicorn) |
| 3000 | Eingehend | Frontend (React development server) |
| 5432 | Intern | PostgreSQL Datenbankzugriff |
| 6379 | Intern | Redis Cache |
| 20128 | Optional | OmniRoute Gateway extern |

### Netzwerk Anforderungen

- Outbound Zugriff auf:
  - `matterhorn-moda.de` (für XML Importe)
  - `host.docker.internal` (für OmniRoute Gateway)
  - Bei Bedarf: externe API Endpunkte (Shopware, etc.)

- Inbound Zugriff erfordert:
  - Port 8000 (Backend API)
  - Port 3000 (Frontend, optional)

### Python Abhängigkeiten

Die `requirements.txt` enthält folgende Kern-Pakete:

```text
fastapi==0.110.0
uvicorn[standard]===0.27.0
aiosqlite==0.20.0
asyncpg==0.29.0
httpx==0.27.0
pydantic==2.6.4
```

Zusätzliche optionale Pakete für erweiterte Funktionen:
- `redis` für Redis Integration
- `bullmq` für Queue Management
- `docker` SDK für Container Steuerung

## IDempotenz und Sicherheit

### Idempotenz

Das Bootstrap Script ist so konzipiert, dass mehrfaches Ausführen keine Inkonsistenzen erzeugt:

- **Datenbanken:** CREATE IF NOT EXISTS Logik
- **Benutzer:** CREATE IF NOT EXISTS Logik
- **Container:** `unless-stopped` Restart Policy
- **Konfiguration:** Überspringen wenn bereits vorhanden (FORCE_RESET erzwingend)

### Sicherheit

- **Secrets:** Werden per `openssl rand -hex` generiert
- **Passwörter:** Werden mit SHA-256 gehasht, niemals im Klartext in Logs
- **.env Datei:** Enthält sensible Daten, sollte nicht committed werden
- **Docker Volumes:** Persistente Daten, gesichert via Benutzerrechte

### Firewall Empfehlungen

```bash
# UFW Beispiel (falls installiert)
ufw allow 8000/tcp   # Backend API
ufw allow 443/tcp    # HTTPS (falls Reverse Proxy)
ufw allow 22/tcp     # SSH (admin access)

# Oderiptables Beispiel
iptables -A INPUT -p tcp --dport 8000 -j ACCEPT
iptables -A INPUT -p tcp --dport 22 -j ACCEPT
```

## Compatibility Matrix

| Kombination | Status |
|------------|--------|
| Alpine Linux + Docker | ✅ Getestet |
| Ubuntu 22.04 + Docker | ✅ Getestet |
| Debian 12 + Docker | ✅ Getestet |
| Zorin OS 17 + Docker | ✅ Getestet |
| Native Alpine ohne Docker | ⚠️ Einschränkt |
| Native Ubuntu ohne Docker | ⚠️ Einschränkt |
| LXC Container (Proxmox) | ✅ Getestet |
| WSL 2 (Windows) | ⚠️ Kann funktionieren |

## Versionsinformation

- **Bootstrap Script Version:** 1.0.0
- **Kompatible Omni-Core Version:** 1.0.0+
- **Docker Image:** `omni-core-middleware:latest`
- **Python Version:** 3.11 (Alpine)
- **FastAPI Version:** 0.110.0+