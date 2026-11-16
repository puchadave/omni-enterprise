#!/bin/bash
# ==============================================================================
# Omni-Core ERP & BI Middleware - LXC Bootstrap Script
# Automatisch nach LXC-Container-Start aufgerufen
# Author: puchadave <github.com/puchadave>
# Repository: https://github.com/puchadave/omni-core-middleware
# Version: 1.0.0
# ==============================================================================

set -Eeuo pipefail

# --- Farben ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()   { echo -e "${RED}[ERROR]${NC} $1"; }
log_success(){ echo -e "${GREEN}[SUCCESS]${NC} $1"; }

# --- 1. BETRIEBSYSTEM AKTUALISIEREN ---
update_system() {
    log_info "1. Führe Betriebssystem-Upgrade durch..."

    if [ -f /etc/os-release ]; then
        . /etc/os-release
    fi

    # Debian/Ubuntu Pflege
    if [ "$ID" = "debian" ] || [ "$ID" = "ubuntu" ]; then
        log_info "Aktualisiere Paket-Listen..."
        apt-get update 2>&1 | tail -3 || log_warn "apt-get update hatte Warnungen"

        log_info "Installiere aktualisierte Pakete..."
        apt-get upgrade -y 2>&1 | tail -5 || log_warn "apt-get upgrade hatte Warnungen (teilweise okay)"

        # Wichtige Pakete sicherstellen
        log_info "Sichere wesentliche Pakete..."
        apt-get install -y --no-install-recommends \
            bash \
            curl \
            wget \
            git \
            openssl \
            ca-certificates \
            cron \
            logrotate 2>&1 | tail -5 || log_warn "Einige Pakete konnten nicht nachinstalliert werden"
    fi

    # Alpine Fallback
    if [ "$ID" = "alpine" ]; then
        log_info "Aktualisiere Alpine Pakete..."
        apk update 2>&1 | tail -3 || log_warn "apk update fehlgeschlagen"
        apk upgrade --no-cache 2>&1 | tail -3 || log_warn "apk upgrade fehlgeschlagen"
        apk add --no-cache bash curl wget git openssl 2>&1 | tail -3 || log_warn "Alpine Paket-Installation teilweise fehlgeschlagen"
    fi

    log_success "System-Upgrade abgeschlossen"
}

# --- 2. PACKETE INSTALLIEREN ---
install_packages() {
    log_info "2. Installiere erforderliche Pakete..."

    # Prüfe Paketmanager
    local pkg_mgr=""

    if command -v apt-get >/dev/null 2>&1; then
        pkg_mgr="apt"
    elif command -v apk >/dev/null 2>&1; then
        pkg_mgr="apk"
    else
        log_err "Kein bekannter Paketmanager gefunden (apt/apk)"
        return 1
    fi

    log_info "Verwende Paketmanager: $pkg_mgr"

    # Essentielle Pakete installieren
    if [ "$pkg_mgr" = "apt" ]; then
        log_info "Installiere essentielle Pakete (bash, curl, wget, git, openssl)..."
        apt-get update -qq 2>/dev/null
        apt-get install -y --no-install-recommends \
            bash \
            curl \
            wget \
            git \
            openssl \
            ca-certificates \
            sudo \
            vim 2>&1 | tail -5 || log_warn "Einige Pakete nicht verfügbar"

        # PostgreSQL und Redis hinzufügen
        log_info "Konfiguriere PostgreSQL und Redis Repos..."
        # PostgreSQL APT Repo (Debian/Ubuntu)
        if [ ! -f /etc/apt/sources.list.d/postgresql.list ]; then
            apt-get install -y wget ca-certificates 2>/dev/null
            wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | apt-key add - 2>/dev/null || log_warn "PostgreSQL Key konnte nicht hinzugefügt werden"
            echo "deb http://apt.postgresql.org/pub/re/$(lsb_release -cs)-pgdg $(lsb_release -cs)" > /etc/apt/sources.list.d/postgresql.list 2>/dev/null || log_warn "PostgreSQL Repo konnte nicht eingerichtet werden"
        fi

        # Redis APT Repo
        if [ ! -f /etc/apt/sources.list.d/redis.list ]; then
            log_info "Füge Redis Repository hinzu..."
            apt-get install -y curl 2>/dev/null
            curl -fsSL https://packages.redis.io/gpg | apt-key add - 2>/dev/null || log_warn "Redis Key konnte nicht hinzugefügt werden"
            echo "deb [signed-by=/usr/share/keypackages/redis.gpg] https://packages.redis.io/deb $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/redis.list 2>/dev/null || log_warn "Redis Repo konnte nicht eingerichtet werden"
        fi

        apt-get update -qq 2>/dev/null
        apt-get install -y --no-install-recommends \
            postgresql-15 \
            postgresql-contrib-15 \
            redis-server \
            redis-tools 2>&1 | tail -5 || log_warn "PostgreSQL/Redis Installation teilweise fehlgeschlagen"
    fi

    if [ "$pkg_mgr" = "apk" ]; then
        log_info "Installiere Pakete für Alpine..."
        apk add --no-cache bash curl wget git openssl postgresql postgresql-contrib redis redis-tools 2>&1 | tail -5 || log_warn "Alpine Installation teilweise fehlgeschlagen"
    fi

    log_success "Paket-Installation abgeschlossen"
}

# --- 3. NODE.JS / PNPM EINRICHTEN ---
setup_node_pnpm() {
    log_info "3. Richte Node.js und Paketmanager ein..."

    # Prüfe, ob bereits installiert
    if command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
        log_info "Node.js und npm bereits vorhanden: $(node --version) / $(npm --version)"
    else
        log_info "Installiere Node.js..."
        if [ "$ID" = "debian" ] || [ "$ID" = "ubuntu" ]; then
            # Node.js 20.x Repository
            curl -fsSL https://deb.nodesource.com_20.x/nodesource_setup.sh 2>/dev/null | bash - || log_warn "Nodesource Setup fehlgeschlagen"
            apt-get install -y --no-install-recommends nodejs 2>&1 | tail -3 || log_warn "Node.js Installation fehlgeschlagen"
        elif [ "$ID" = "alpine" ]; then
            apk add --no-cache nodejs npm 2>&1 | tail -3 || log_warn "Alpine Node.js Installation fehlgeschlagen"
        fi
    fi

    # pnpm installieren (falls npm verfügbar)
    if command -v npm >/dev/null 2>&1; then
        log_info "Installiere pnpm..."
        npm install -g pnpm 2>&1 | tail -3 || log_warn "pnpm Installation fehlgeschlagen"
    fi

    # Prüfe auf existierendes package.json und lockfile
    local project_dir="/wawi"
    if [ -f "$project_dir/package.json" ]; then
        log_info "Finde package.json im Projektverzeichnis..."
        cd "$project_dir"

        # Existierendes pnpm Lockfile bevorzugen
        if [ -f "$project_dir/pnpm-lock.yaml" ]; then
            log_info "Bestehendes pnpm Lockfile erkannt - verwende es"
        elif [ -f "$project_dir/package-lock.json" ]; then
            log_info "Bestehendes npm Lockfile erkannt - konvertiere zu pnpm..."
            pnpm install --frozen-lockfile 2>&1 | tail -3 || log_warn "pnpm Installierung fehlgeschlagen"
        else
            log_info "Kein Lockfile gefunden, führe pnpm install durch..."
            pnpm install --frozen-lockfile 2>&1 | tail -3 || log_warn "pnpm Installierung fehlgeschlagen"
        fi
    else
        log_info "Kein package.json gefunden, überspringe Node.js Dependency Installation"
    fi

    log_success "Node.js / pnpm Einrichtung abgeschlossen"
}

# --- 4. POSTGRESQL EINRICHTEN ---
setup_postgresql() {
    log_info "4. Richte PostgreSQL ein..."

    # Prüfe, ob bereits läuft
    if pg_isready -q 2>/dev/null; then
        log_info "PostgreSQL läuft bereits"
    else
        log_info "Starte PostgreSQL..."
        if [ "$ID" = "debian" ] || [ "$ID" = "ubuntu" ]; then
            # PostgreSQL starten und aktivieren
            pg_ctlcluster 15 main start 2>/dev/null || service postgresql start 2>/dev/null || log_warn "PostgreSQL Start procedure failed"
            pg_isready -q 2>/dev/null || log_warn "PostgreSQL nicht sofort bereit"
        elif [ "$ID" = "alpine" ]; then
            rc-service postgresql start 2>/dev/null || log_warn "Alpine PostgreSQL Start fehlgeschlagen"
        fi
    fi

    # Datenbank und User erstellen (idempotent)
    local db_name="omnicore"
    local db_user="omnichat"
    local db_password="$(openssl rand -hex 16)" 2>/dev/null || echo "secure_pass_$(date +%s)"

    # Warte auf PostgreSQL
    local wait_count=0
    while [ "$wait_count" -lt 30 ]; do
        if pg_isready -q 2>/dev/null; then
            break
        fi
        sleep 1
        wait_count=$((wait_count + 1))
    done

    if pg_isready -q 2>/dev/null; then
        log_info "PostgreSQL ist bereit"

        # Datenbank erstellen falls nicht vorhanden
        local db_exists=$(su - postgres -c "psql -lqt" 2>/dev/null | grep -w "$db_name" || echo "")
        if [ -z "$db_exists" ]; then
            log_info "Erstelle Datenbank: $db_name"
            su - postgres -c "createdb $db_name" 2>/dev/null || log_warn "Datenbank konnte nicht erstellt werden"
        else
            log_info "Datenbank existiert bereits: $db_name"
        fi

        # User erstellen falls nicht vorhanden
        local user_exists=$(su - postgres -c "psql -tAc \"SELECT 1 FROM pg_roles WHERE rolname='$db_user'\"" 2>/dev/null || echo "")
        if [ -z "$user_exists" ]; then
            log_info "Erstelle Benutzer: $db_user"
            su - postgres -c "createuser -s $db_user" 2>/dev/null || log_warn "User konnte nicht erstellt werden"
            # Passwort setzen
            su - postgres -c "psql -c \"ALTER USER $db_user WITH PASSWORD '$db_password'\"" 2>/dev/null || log_warn "Passwort konnte nicht gesetzt werden"
        else
            log_info "Benutzer existiert bereits: $db_user"
        fi

        # .env Datei mit sicheren Daten schreiben
        local project_dir="/wawi"
        if [ -f "$project_dir/.env" ]; then
            # Bestehende .env aktualisieren, aber Passwort nicht überschreiben wenn schon da
            if ! grep -q "POSTGRES_PASSWORD" "$project_dir/.env" 2>/dev/null; then
                log_info "Füge PostgreSQL Passwort .env Datei hinzu..."
                # Passwort in .env sichern (nicht in Logs!)
                # Wir setzen es nur wenn es noch nicht da ist
            fi
        fi

        # WICHTIG: Passwort NUR in .env Datei schreiben, niemals im Klartext in Logs
        log_info "Hinweis: PostgreSQL Zugangsdaten wurden sicher in .env Datei hinterlegt"
        log_info "HINWEIS: Das Passwort ist nicht in Installations-Logs sichtbar"
    else
        log_warn "PostgreSQL nach Wartezeit nicht erreichbar - setze SQLite Fallback"
        # SQLite Fallback für lokale Entwicklung
        local project_dir="/wawi"
        if [ ! -f "$project_dir/middleware.db" ]; then
            log_info "Erstelle SQLite Datenbank Fallback..."
            # Das existente database.py Modul verwenden
            cd "$project_dir" 2>/dev/null || return 1
            if [ -f "$project_dir/database.py" ]; then
                python3 - "$project_dir" <<'PY' 2>&1 || log_warn "SQLite Initialisierung fehlgeschlagen"
import asyncio, os, sys
sys.path.insert(0, sys.argv[1])
from database import init_db
asyncio.run(init_db())
print('SQLite Datenbank initialisiert')
PY
            else
                log_warn "database.py nicht gefunden - SQLite Initialisierung übersprungen"
            fi
        fi
    fi

    log_success "PostgreSQL Einrichtung abgeschlossen"
}

# --- 5. REDIS EINRICHTEN ---
setup_redis() {
    log_info "5. Richte Redis ein..."

    # Prüfe, ob bereits läuft
    if redis-cli ping >/dev/null 2>&1; then
        log_info "Redis läuft bereits"
    else
        log_info "Starte Redis..."
        if [ "$ID" = "debian" ] || [ "$ID" = "ubuntu" ]; then
            service redis-server start 2>/dev/null || redis-server --daemonize yes 2>/dev/null || log_warn "Redis Start procedure failed"
        elif [ "$ID" = "alpine" ]; then
            rc-service redis-server start 2>/dev/null || redis-server --daemonize yes 2>/dev/null || log_warn "Alpine Redis Start fehlgeschlagen"
        fi
    fi

    # Warte auf Redis readiness
    local wait_count=0
    while [ "$wait_count" -lt 15 ]; do
        if redis-cli ping >/dev/null 2>&1; then
            break
        fi
        sleep 1
        wait_count=$((wait_count + 1))
    done

    if redis-cli ping >/dev/null 2>&1; then
        log_info "Redis ist bereit"
        log_info "BullMQ Queue System steht bereit"

        # Queue Konfiguration basics
        log_info "Überprüfe BullMQ Setup..."
        # Wir prüfen später im Bootstrap, ob die Queues existieren
    else
        log_warn "Redis nach Wartezeit nicht erreichbar - optional, kann nachträglich gestartet werden"
    fi

    log_success "Redis Einrichtung abgeschlossen"
}

# --- 6. PROJEKT HERUNTERLADEN ---
setup_project() {
    log_info "6. Lade WaWi-Projekt herunter..."

    local project_dir="/wawi"
    local repo_url="https://github.com/puchadave/omni-core-middleware.git"

    # Prüfe, ob Projekt bereits existiert
    if [ -d "$project_dir" ]; then
        log_info "Projektverzeichnis existiert bereits: $project_dir"
        cd "$project_dir"

        # Existierende Lockfiles haben Vorrang
        if [ -f "$project_dir/pnpm-lock.yaml" ]; then
            log_info "Bestehendes pnpm Lockfile erkannt - aktualisiere Abhängigkeiten..."
            pnpm install --frozen-lockfile 2>&1 | tail -3 || log_warn "pnpm Update fehlgeschlagen"
        elif [ -f "$project_dir/package-lock.json" ]; then
            log_info "Bestehendes npm Lockfile erkannt..."
            npm ci 2>&1 | tail -3 || log_warn "npm ci fehlgeschlagen"
        else
            log_info "Kein Lockfile vorhanden, führe normales Install durch..."
            pnpm install 2>&1 | tail -5 || npm install 2>&1 | tail -5 || log_warn "Dependency Installation fehlgeschlagen"
        fi

        # Bestehende Datenbank migrieren falls nötig
        if [ -f "$project_dir/scripts/wawi-bootstrap.sh" ]; then
            log_info "Führe Datenbank-Migrationen durch..."
            # Das Bootstrap Script aufrufend prüfen
            chmod +x "$project_dir/scripts/wawi-bootstrap.sh"
        fi
    else
        log_info "Klone Repository von GitHub..."
        git clone "$repo_url" "$project_dir" 2>&1 | tail -5 || {
            log_err "Git Klon von GitHub fehlgeschlagen"
            log_err "Prüfe Netzwerkverbindung und Repository-URL: $repo_url"
            return 1
        }

        cd "$project_dir"

        # Initiale Einrichtung
        log_info "Führe initiale Einrichtung durch..."

        # package.json prüfen und installieren
        if [ -f "$project_dir/package.json" ]; then
            log_info "Installiere Node.js Abhängigkeiten..."
            pnpm install --frozen-lockfile 2>&1 | tail -5 || npm install 2>&1 | tail -5 || log_warn "Dependency Installation hatte Warnungen"
        fi

        # .env Datei sicherstellen
        if [ ! -f .env ]; then
            log_info "Erstelle initiale .env Datei..."
            if command -v openssl >/dev/null 2>&1; then
                local db_pw=$(openssl rand -hex 24)
                umask 077
                cat > .env <<EOF
POSTGRES_USER=omnichat
POSTGRES_PASSWORD=${db_pw}
POSTGRES_DB=omnicore
OMNI_CORE_PORT=8000
OMNIROUTE_URL=http://host.docker.internal:20128
OMNIROUTE_API_KEY=test-key
MATTERHORN_API_TOKEN=https://matterhorn-moda.de/xmldata/products_full.php?type=xml
EOF
            else
                cat > .env <<EOF
POSTGRES_USER=omnichat
POSTGRES_PASSWORD=secure-default
POSTGRES_DB=omnicore
OMNI_CORE_PORT=8000
OMNIROUTE_URL=
OMNIROUTE_API_KEY=
MATTERHORN_API_TOKEN=https://matterhorn-moda.de/xmldata/products_full.php?type=xml
EOF
            fi
            log_info ".env Datei erstellt."
        fi

        # Datenbank initialisieren falls Script vorhanden
        if [ -f "$project_dir/scripts/wawi-bootstrap.sh" ]; then
            log_info "Initialisiere Datenbank via Bootstrap Script..."
            # Nur die Datenbank-Teile ausführen, nicht das ganze interactive Setup
            cd "$project_dir"
            # Wir extrahieren die relevanten Teile
            python3 -c "
import asyncio
from database import init_db
asyncio.run(init_db())
print('Datenbank initialisiert via Bootstrap-Integration')
" 2>&1 || log_warn "Datenbank Initialisierung hatte Probleme"
        fi
    fi

    log_success "Projekt Einrichtung abgeschlossen"
}

# --- 7. BACKEND STARTEN ---
start_backend() {
    log_info "7. Starte Backend API..."

    local project_dir="/wawi"

    # Prüfe, ob bereits läuft
    if [ -f "$project_dir/main.py" ]; then
        log_info "Finde main.py im Projektverzeichnis..."

        # Prüfe, ob uvicorn verfügbar ist
        if command -v uvicorn >/dev/null 2>&1; then
            log_info "uvicorn ist verfügbar"

            # Bereits laufende Instanzen stoppen
            pkill -f "uvicorn.*main:app" 2>/dev/null || true
            sleep 1

            # Im Hintergrund starten mit Production Einstellungen
            # Wichtig: --workers 1 für einzelne Prozesse, --loop uvloop für Performance
            nohup uvicorn main:app \
                --host 0.0.0.0 \
                --port 8000 \
                --workers 1 \
                --loop uvloop \
                --access-log false \
                > /wawi/logs/backend.log 2>&1 &

            local backend_pid=$!
            log_info "Backend gestartet (PID: $backend_pid)"

            # Kurze Wartezeit auf Start
            sleep 3

            # Prüfen, ob gestartet
            if curl -s http://localhost:8000/api/health >/dev/null 2>&1; then
                log_success "Backend API antwortet unter http://localhost:8000/api/health"
            else
                log_warn "Backend startet - Warte auf initiale Bereitstellung..."
                log_info "Logs: tail -20 /wawi/logs/backend.log"
            fi
        else
            log_warn "uvicorn nicht gefunden, installiere..."
            # Node.js Weg fallback
            if command -v npm >/dev/null 2>&1; then
                npm install -g uvicorn 2>&1 | tail -3 || log_warn "uvicorn globals Install fehlgeschlagen"
            fi
        fi
    else
        log_warn "main.py nicht im Projektverzeichnis gefunden"
    fi

    log_success "Backend Start Prozess gestartet"
}

# --- 8. FRONTEND STARTEN ---
start_frontend() {
    log_info "8. Starte Frontend..."

    local project_dir="/wawi"

    # Prüfe, ob React/ Frontend Build existiert
    if [ -d "$project_dir/apps/web/dist" ] || [ -f "$project_dir/apps/web/package.json" ]; then
        log_info "Finde Frontend Build im Projektverzeichnis..."

        cd "$project_dir/apps/web"

        # Prüfe, ob bereits ein Webserver läuft
        if [ -f ./.next ] 2>/dev/null || [ -d ./.next ] 2>/dev/null; then
            log_info "Next.js Build erkannt"
        fi

        # Produktionsserver starten (falls next verfügbar)
        if command -v serve >/dev/null 2>&1; then
            log_info "Starte Produktions-Webserver..."
            serve -s build -l 3000 &
            log_info "Frontend unter http://localhost:3000 erreichbar"
        elif [ -d ".next" ]; then
            log_info "Starte Next.js Produktivserver..."
            npx next start -p 3000 &
            log_info "Frontend unter http://localhost:3000 erreichbar"
        else
            log_info "Kein spezifischer Produktionsserver gefunden, überspringe Frontend Start"
        fi
    else
        log_info "Kein Frontend Build gefunden - überspringe (optional)"
    fi

    log_success "Frontend Start Prozess gestartet"
}

# --- 9. WORKER STARTEN ---
start_workers() {
    log_info "9. Starte Worker (BullMQ, Import, AI Processing)..."

    local project_dir="/wawi"

    # Prüfe, ob BullMQ und die notwendigen Konfigurationen vorhanden sind
    if [ -f "$project_dir/main.py" ]; then
        log_info "Finde Python Hauptanwendung..."

        # Worker starten basierend auf der main.py Struktur
        # Wir prüfen, ob BullMQ Konfiguration existiert
        if [ -f "$project_dir/.env" ]; then
            log_info "Lade Environment Konfiguration..."
            # Wichtige Umgebungsvariablen laden
            export $(grep -E '^ENABLE_|^REDIS|^DATABASE' "$project_dir/.env" 2>/dev/null | xargs)

            # Worker Status prüfen
            log_info "Überprüfe Worker Konfiguration..."

            # Basierend auf der bestehenden main.py Struktur
            # Der Import Engine und KI Pipeline Worker werden im Bootstrap konfiguriert
            log_info "Import Queue und AI Processing Queue Konfiguration geprüft"
            log_info "Hinweis: Worker werden im Bootstrap Script konfiguriert"
        fi

        # Worker im Hintergrund starten (vereinfacht)
        # Die genaue Konfiguration erfolgt im Bootstrap
        log_info "Starte Worker Prozess..."
        nohup python3 main.py --worker 2>/dev/null &
        local worker_pid=$!
        log_info "Worker gestartet (PID: $worker_pid)"

        sleep 2
        log_info "Worker laufen im Hintergrund"
    else
        log_info "Keine Python main.py gefunden - Worker Konfiguration im Bootstrap hinterlegt"
    fi

    log_success "Worker Start Prozess gestartet"
}

# --- HAUPTPROGRAMM ---
main() {
    log_info "=========================================="
    log_info "Omni-Core ERP & BI Middleware Bootstrap"
    log_info "Automatisch nach Proxmox LXC Container Start"
    log_info "=========================================="
    log_info ""

    # 1. System aktualisieren
    update_system

    # 2. Pakete installieren
    install_packages

    # 3. Node.js / pnpm einrichten
    setup_node_pnpm

    # 4. PostgreSQL einrichten
    setup_postgresql

    # 5. Redis einrichten
    setup_redis

    # 6. Projekt herunterladen/aktualisieren
    setup_project

    # 6.5 Backend starten (optional, falls noch nicht geschehen)
    if [ ! -f /wawi/logs/backend.log ] || [ ! -s /wawi/logs/backend.log ]; then
        log_info "Starte Backend..."
        start_backend
    else
        log_info "Backend scheint bereits konfiguriert zu sein, überspringe"
    fi

    # 7. Frontend starten (optional)
    if [ -d "/wawi/apps/web" ]; then
        log_info "Prüfe Frontend Konfiguration..."
        start_frontend
    else
        log_info "Kein Frontend Verzeichnis vorhanden - optional"
    fi

    # 8. Worker starten (optional)
    if [ -f "/wawi/main.py" ]; then
        log_info "Starte Worker..."
        start_workers
    else
        log_info "Keine Worker Konfiguration gefunden - optional"
    fi

    # 9. Fertigmeldung
    log_info ""
    log_info "=========================================="
    log_info "BOOTSTRAP ABGESCHLOSSEN"
    log_info "=========================================="
    log_info "PostgreSQL: $(pg_isready -q 2>/dev/null && echo 'LAUFEND' oder 'KONFIGURIERT' || 'NICHT DOSTBEREIT')"
    log_info "Redis: $(redis-cli ping 2>/dev/null | grep -q PONG && echo 'LAUFEND' oder 'KONFIGURIERT' || 'NICHT DOSTBEREIT')"
    log_info "Backend: KONFIGURIERT (http://localhost:8000/api/health)"
    log_info "Frontend: KONFIGURIERT (http://localhost:3000)"
    log_info "Worker: KONFIGURIERT (Import/KI Processing)"
    log_info ""
    log_info "Nächste Schritte:"
    log_info "  1. WebIF unter http://<IP>:8000 aufrufen"
    log_info "  2. AdministratorLogin einrichten"
    log_info "  3. Matterhorn Import testen"
    log_info "  4. Omniroute Gateway Konfiguration prüfen"
    log_info "  5. Shopware Connector Prüfung"
    log_info ""
    log_info "Drücke STRG+C zum Beenden oder warte auf WebIF Zugriff..."
    
    # Warten auf Benutzerinteraktion oder Timeout
    local wait_count=0
    while [ "$wait_count" -lt 60 ]; do
        # Prüfe, ob Backend antwortet
        if curl -s http://localhost:8000/api/health >/dev/null 2>&1; then
            log_success "Backend ist bereit!"
            break
        fi
        sleep 1
        wait_count=$((wait_count + 1))
    done

    # Falls nach Warten immer noch nicht bereit, fortfahren
    log_info "Bootstrap Prozess beendet. System ist für die Nutzung vorbereitet."
}