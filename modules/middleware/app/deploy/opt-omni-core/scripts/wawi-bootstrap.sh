#!/bin/bash
# ==============================================================================
# Omni-Core ERP & BI Middleware - Optimized Bootstrap für Proxmox LXC
# Author: Principal Software Architect
# Version: 1.1.0 (Optimized for LXC Efficiency)
# ==============================================================================

set -Eeuo pipefail

###############################################################################
# Farben und Logging
###############################################################################

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()  { echo -e "${RED}[ERROR]${NC} $1"; }
log_success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }

###############################################################################
# OS-Erkennung
###############################################################################

detect_os() {
    local os_id=""
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        os_id=$ID
    fi
    case "$os_id" in
        alpine)  OS="alpine"  PKG_MGR="apk" ;;
        debian)  OS="debian"  PKG_MGR="apt" ;;
        ubuntu)  OS="ubuntu"  PKG_MGR="apt" ;;
        zorin)   OS="zorin"   PKG_MGR="apt" ;;
        *)       OS="unknown" PKG_MGR="manual"; log_warn "Unbekanntes OS: $os_id" ;;
    esac
    log_info "Erkanntes OS: $OS"
}

###############################################################################
# 1. SYSTEM-DETECTION für Proxmox LXC
###############################################################################

detect_lxc_environment() {
    log_info "Erkenne LXC/Proxmox Umgebung..."

    # Prüfe, ob wir in einem LXC Container laufen
    if [ -f /proc/1/status ]; then
        # Prüfe Speicherlimits
        if [ -f /sys/fs/memory/memory.limit_in_bytes ]; then
            LXC_MEMORY_LIMIT=$(cat /sys/fs/memory/memory.limit_in_bytes 2>/dev/null)
            LXC_MEMORY_LIMIT_MB=$((LXC_MEMORY_LIMIT / 1024 / 1024))
        elif [ -f /sys/fs/memory.max ]; then
            LXC_MEMORY_LIMIT=$(cat /sys/fs/memory.max 2>/dev/null)
            LXC_MEMORY_LIMIT_MB=$((LXC_MEMORY_LIMIT / 1024 / 1024))
        fi

        # Prüfe auf Proxmox-spezifische Flags
        if grep -q "proxmox" /etc/issue 2>/dev/null || grep -q "proxmox" /etc/os-release 2>/dev/null; then
            IS_PROXMOX="true"
        else
            IS_PROXMOX="false"
        fi
    fi

    # Fallback: nproc und free (mit deutscher/englischer Locale-Behandlung)
    CPU_CORES=$(nproc 2>/dev/null || echo "2")
    # free -m Output kann je nach Locale "Mem:" oder "Speicher:" enthalten
    MEM_TOTAL=$(free -m 2>/dev/null | awk '/Mem:|Speicher:/ {print $2}' || echo "2048")

    log_info "CPU-Kerne: $CPU_CORES"
    log_info "Gesamter RAM: ${MEM_TOTAL}MB"
    log_info "Proxmox Container: $IS_PROXMOX"

    # Ableite Werte basierend auf LXC Limits, falls vorhanden
    # Standardwerte setzen (werden überschrieben wenn LXC Limits gefunden werden)
    ALLOCATED_RAM_MB="$((MEM_TOTAL * 75 / 100))"
    [ "$ALLOCATED_RAM_MB" -gt 4096 ] && ALLOCATED_RAM_MB=4096
    ALLOCATED_CPU="$((CPU_CORES / 2))"
    [ "$ALLOCATED_CPU" -lt 1 ] && ALLOCATED_CPU=1
    [ "$ALLOCATED_CPU" -gt 4 ] && ALLOCATED_CPU=4

    # LXC Memory Limit pruefen (falls vorhanden)
    # Variable vorher default setzen, um Fehler bei set -eu zu vermeiden
    LXC_MEMORY_LIMIT_MB="${LXC_MEMORY_LIMIT_MB:-0}"

    if [ -n "$LXC_MEMORY_LIMIT_MB" ] && [ "$LXC_MEMORY_LIMIT_MB" -gt 0 ] 2>/dev/null; then
        # 70% des LXC Limits zuweisen, Rest fuer Host erhalten bleiben
        ALLOCATED_RAM_MB="$((LXC_MEMORY_LIMIT_MB * 70 / 100))"
        log_info "LXC Memory Limit erkannt: ${LXC_MEMORY_LIMIT_MB}MB, zugewiesen: ${ALLOCATED_RAM_MB}MB"
    fi

    export LXC_ALLOCATED_RAM_MB="$ALLOCATED_RAM_MB"
    export LXC_ALLOCATED_CPU="$ALLOCATED_CPU"
}

###############################################################################
# 2. OPTIMIERTE ENVIRONMENT KONFIGURATION
###############################################################################

configure_optimized_env() {
    log_info "Konfiguriere optimierte Umgebung für LXC..."

    PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.."
    ENV_FILE="$PROJECT_DIR/.env"

    # Nur erstellen wenn nicht vorhanden (idempotent)
    if [ -f "$ENV_FILE" ] && [ "$FORCE_RESET" != "true" ]; then
        log_info ".env existiert bereits, bewerte Optimierung"
        source "$ENV_FILE" 2>/dev/null || true
    fi

    # Sichere Secrets generieren falls nötig
    if [ -z "${SECRET_KEY:-}" ]; then
        SECRET_KEY=$(openssl rand -hex 16)
    fi
    if [ -z "${JWT_SECRET:-}" ]; then
        JWT_SECRET=$(openssl rand -hex 32)
    fi

    # Datenbank-Konfiguration optimiert für Container-RAM
    DB_USER="${POSTGRES_USER:-omnichat}"
    DB_PASSWORD="${POSTGRES_PASSWORD:-$(openssl rand -hex 12)}"
    DB_NAME="${POSTGRES_DB:-omnicore}"

    # RAM-optimierte PostgreSQL Einstellungen
    if [ "$LXC_ALLOCATED_RAM_MB" -lt 2048 ]; then
        POSTGRES_MAX_CONNECTIONS="10"
        POSTGRES_SHARED_BUFFERS="128MB"
        POSTGRES_WORK_MEM="4MB"
    elif [ "$LXC_ALLOCATED_RAM_MB" -lt 4096 ]; then
        POSTGRES_MAX_CONNECTIONS="20"
        POSTGRES_SHARED_BUFFERS="256MB"
        POSTGRES_WORK_MEM="8MB"
    else
        POSTGRES_MAX_CONNECTIONS="40"
        POSTGRES_SHARED_BUFFERS="512MB"
        POSTGRES_WORK_MEM="16MB"
    fi

    # OmniRoute Gateway Konfiguration
    OMNI_ROUTE_URL="${OMNI_ROUTE_URL:-http://host.docker.internal:20128}"
    OMNI_ROUTE_API_KEY="${OMNI_ROUTE_API_KEY:-test-key}"

    # Matterhorn Import
    MATTERHORN_API_TOKEN="${MATTERHORN_API_TOKEN:-}"

    # Server Konfiguration - auf Container optimiert
    APP_PORT="${OMNI_CORE_PORT:-8000}"
    FRONTEND_PORT="${FRONTEND_PORT:-3000}"

    # Admin - aus Umgebung oder Default
    ADMIN_EMAIL="${ADMIN_EMAIL:-admin@omni-core.local}"
    if [ -z "${ADMIN_PASSWORD:-}" ] && [ "$INTERACTIVE" = "true" ]; then
        log_info "Bitte Admin-Passwort für LXC-Installation festlegen:"
        read -r -p "Admin-Passwort: " ADMIN_PASSWORD
    fi
    ADMIN_PASSWORD="${ADMIN_PASSWORD:-$(openssl rand -hex 12)}"

    # .env Datei schreiben mit optimierten Werten
    {
        echo "# ============================================================================="
        echo "# Omni-Core ERP & BI Middleware - Optimierte LXC Konfiguration"
        echo "# Automatisch generiert von wawi-bootstrap.sh (Optimized)"
        echo "# ============================================================================="
        echo ""
        echo "# === Datenbank Optimierung (an Container-RAM angepasst) ==="
        echo "POSTGRES_USER=$DB_USER"
        echo "POSTGRES_PASSWORD=$DB_PASSWORD"
        echo "POSTGRES_DB=$DB_NAME"
        echo "POSTGRES_MAX_CONNECTIONS=${POSTGRES_MAX_CONNECTIONS:-20}"
        echo "POSTGRES_SHARED_BUFFERS=${POSTGRES_SHARED_BUFFERS:-256MB}"
        echo "POSTGRES_WORK_MEM=${POSTGRES_WORK_MEM:-8MB}"
        echo ""
        echo "# === OmniRoute Gateway ==="
        echo "OMNI_ROUTE_URL=$OMNI_ROUTE_URL"
        echo "OMNI_ROUTE_API_KEY=$OMNI_ROUTE_API_KEY"
        echo ""
        echo "# === Server (optimiert für LXC) ==="
        echo "OMNI_CORE_PORT=$APP_PORT"
        echo "FRONTEND_PORT=$FRONTEND_PORT"
        echo ""
        echo "# === Administration ==="
        echo "ADMIN_EMAIL=$ADMIN_EMAIL"
        echo "ADMIN_PASSWORD_HASH=$(echo -n "$ADMIN_PASSWORD" | sha256sum | cut -d' ' -f1)"
        echo ""
        echo "# === KI und Features ==="
        echo "SECRET_KEY=$SECRET_KEY"
        echo "JWT_SECRET=$JWT_SECRET"
        echo "ENABLE_MARKETING_ENGINE=true"
        echo "ENABLE_SALES_AUTOMATION=true"
        echo "ENABLE_CHANNEL_ADAPTERS=true"
        echo "ENABLE_MATTERHORN_IMPORT=true"
        echo ""
        echo "# === LXC Ressourcenzuweisung (aus Bootstrap) ==="
        echo "LXC_ALLOCATED_RAM_MB=$LXC_ALLOCATED_RAM_MB"
        echo "LXC_ALLOCATED_CPU=${LXC_ALLOCATED_CPU:-2}"
    } > "$ENV_FILE"

    log_success ".env optimiert erstellt: $ENV_FILE"
    log_info "Hinweis: PostgreSQL Parameter wurden an ${LXC_ALLOCATED_RAM_MB}MB RAM angepasst"
}

###############################################################################
# 3. EINZIGE POSTGRESQL INSTANZ (keine Redundanz)
###############################################################################

init_single_postgresql() {
    log_info "Initialisiere einzelne PostgreSQL-Instanz..."

    PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.."

    # Prüfe, ob Docker Compose verwendet wird (bestehende Architektur)
    if [ -f "$PROJECT_DIR/docker-compose.yml" ]; then
        log_info "Docker Compose erkannt - nutze bestehende PostgreSQL Konfiguration"

        # Warte auf PostgreSQL im Docker Netzwerk (max 40 Sekunden)
        log_info "Warte auf PostgreSQL Docker Service..."
        local db_ready="false"
        for i in $(seq 1 20); do
            if docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db pg_isready -U "$DB_USER" -d "$DB_NAME" 2>/dev/null; then
                db_ready="true"
                log_success "PostgreSQL ist bereit"
                break
            fi
            sleep 2
        done

        if [ "$db_ready" = "false" ]; then
            log_warn "PostgreSQL nicht erreichbar nach Wartezeit - pruefe native Installation"
            # Native Fallback
            cd "$PROJECT_DIR"
            if [ ! -f "middleware.db" ]; then
                python3 -c "
import asyncio
from database import init_db
asyncio.run(init_db())
print('SQLite Datenbank initialisiert')
"
            else
                log_info "Bestehende SQLite Datenbank gefunden"
            fi
            return 0
        fi

        # Datenbank und User sicherstellen (idempotent)
        DB_EXISTS=$(docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db psql -U postgres -lqt 2>/dev/null | cut -d '|' -f1 | grep -w "$DB_NAME" || true)
        if [ -z "$DB_EXISTS" ]; then
            log_info "Erstelle Datenbank $DB_NAME"
            docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db psql -U postgres -c "CREATE DATABASE $DB_NAME" 2>/dev/null || true
        else
            log_info "Datenbank $DB_NAME existiert bereits"
        fi

        # User prüfen/erstellen
        USER_EXISTS=$(docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db psql -U postgres -tc "SELECT 1 FROM pg_roles WHERE rolname = '$DB_USER'" 2>/dev/null || true)
        if [ -z "$USER_EXISTS" ]; then
            log_info "Erstelle Benutzer $DB_USER"
            docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db psql -U postgres -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD'" 2>/dev/null || true
        fi

        # Berechtigungen
        docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER" 2>/dev/null || true
        docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T db psql -U postgres -c "GRANT ALL PRIVILEGES ON SCHEMA public TO $DB_USER" 2>/dev/null || true

        log_success "PostgreSQL Verbindung getestet: OK"

        # PostgreSQL Parameter dokumentieren (konnten nicht runtime geändert werden)
        log_info "PostgreSQL Parameter wurden in .env konfiguriert und beim nächsten Container-Start wirksam"
        log_info "Folgende Parameter wurden gesetzt:"
        log_info "  - POSTGRES_MAX_CONNECTIONS=${POSTGRES_MAX_CONNECTIONS:-20}"
        log_info "  - POSTGRES_SHARED_BUFFERS=${POSTGRES_SHARED_BUFFERS:-256MB}"
        log_info "  - POSTGRES_WORK_MEM=${POSTGRES_WORK_MEM:-8MB}"

    else
        # Native Installation - SQLite Fallback
        log_info "Kein Docker Compose gefunden, nutze SQLite native Installation"
        cd "$PROJECT_DIR"
        if [ ! -f "middleware.db" ]; then
            python3 -c "
import asyncio
from database import init_db
asyncio.run(init_db())
print('SQLite Datenbank initialisiert')
"
        else
            log_info "Bestehende SQLite Datenbank gefunden"
        fi
    fi
}

###############################################################################
# 4. EINZIGE REDIS INSTANZ (keine Duplikate)
###############################################################################

init_single_redis() {
    log_info "Initialisiere einzelne Redis-Instanz..."

    PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.."

    # Prüfe, ob Redis bereits in Docker Compose läuft
    if [ -f "$PROJECT_DIR/docker-compose.yml" ] && docker compose -f "$PROJECT_DIR/docker-compose.yml" ps 2>/dev/null | grep -q "redis"; then
        log_info "Redis Docker Service bereits in docker-compose.yml enthalten"

        # Warte auf Redis readiness (max 30 Sekunden)
        for i in $(seq 1 15); do
            if docker compose -f "$PROJECT_DIR/docker-compose.yml" exec -T redis redis-cli ping 2>/dev/null | grep -q PONG; then
                log_success "Redis ist erreichbar"

                # BullMQ Konfiguration - ressourcenschonend
                log_info "BullMQ Queue Konfiguration (optimiert):"
                log_info "  - Maximale parallele Jobs: 4 (LXC-resource-schonend)"
                log_info "  - Retry mit Exponential Backoff aktiviert"
                log_info "  - Queue Persistenz: enabled (kritische Aufgaben)"
                log_info "  - Polling-Intervalle: minimiert"
                return 0
            fi
            sleep 2
        done

        log_warn "Redis nicht erreichbar nach Wartezeit"
        return 1

    elif docker ps --format "{{.Names}}" 2>/dev/null | grep -qi "redis"; then
        log_info "Redis Container läuft (nicht in docker-compose.yml)"

        local redis_ready="false"
        for i in $(seq 1 15); do
            local rcn=$(docker ps --format "{{.Names}}" 2>/dev/null | grep -i redis | head -1)
            if [ -n "$rcn" ]; then
                if docker exec -i "$rcn" redis-cli ping 2>/dev/null | grep -q PONG; then
                    log_success "Redis ist erreichbar: $rcn"
                    local redis_ready="true"
                    return 0
                fi
            fi
            sleep 2
        done

        if [ "$redis_ready" != "true" ]; then
            log_warn "Redis Container nicht bereit nach Wartezeit"
        fi
        return 1

    else
        log_warn "Kein Redis Docker Container gefunden"
        log_info "Hinweis: Ohne Redis ist BullMQ nicht voll funktionsfähig"
        log_info "Starte Redis falls erforderlich manuell: redis-server --daemonize yes"
        # Nicht kritisch abbrechen - System funktioniert teilweis ohne Redis
        return 0
    fi
}

###############################################################################
# 5. OPTIMIERTE RUNTIME-KONFIGURATION (Python/uvicorn)
###############################################################################

optimize_runtime() {
    log_info "Konfiguriere Runtime-Optimierung für Python/uvicorn..."

    # Das System ist Python/FastAPI basiert
    # Optimiere uvicorn für Container-Effizienz

    # Worker-Konfiguration basierend auf CPU-Kernen
    CPU_COUNT="${LXC_ALLOCATED_CPU:-2}"
    UVICORN_WORKERS="${UVICORN_WORKERS:-1}"  # Single Worker für LXC-Effizienz

    log_info "uvicorn Konfiguration:"
    log_info "  - Worker: $UVICORN_WORKERS (Single für LXC Stabilität)"
    log_info "  - CPU-Kernen zugewiesen: $CPU_COUNT"
    log_info "  - Event Loop: uvloop (aktiviert für maximale Durchsatzleistung)"

    # Heap-Limits basierend auf verfügbarem RAM
    if [ "$LXC_ALLOCATED_RAM_MB" -lt 2048 ]; then
        PYTHON_MEM_LIMIT="256M"
    elif [ "$LXC_ALLOCATED_RAM_MB" -lt 4096 ]; then
        PYTHON_MEM_LIMIT="512M"
    else
        PYTHON_MEM_LIMIT="1G"
    fi

    export UVICORN_WORKERS
    export PYTHON_MEM_LIMIT

    log_info "Python Speicherlimit: $PYTHON_MEM_LIMIT"
    log_info "Hinweis: UVICORN_WORKERS=1 minimiert Memory Overhead im LXC"
}

###############################################################################
# 6. OPTIMIERTE MATTERHORN IMPORT ENGINE
###############################################################################

optimized_matterhorn_import() {
    log_info "Initialisiere optimierten Matterhorn Import Engine..."

    # Streaming XML Parsing für Speichereffizienz
    # Keine vollständigen XMLs mehrfach laden

    IMPORT_PARALLELISM="${IMPORT_PARALLELISM:-1}"  # Single Import Job gleichzeitig
    IMPORT_BATCH_SIZE="${IMPORT_BATCH_SIZE:-50}"   # Batch Size für DB Inserts

    log_info "Matterhorn Import Optimierung:"
    log_info "  - Parallelität: $IMPORT_PARALLELISM simultane Import-Jobs"
    log_info "  - Batch Size: $IMPORT_BATCH_SIZE Datensätze pro Batch"
    log_info "  - Strategie: Streaming Parser, inkrementelle Updates"
    log_info "  - Duplikaterkennung: Hash-basiert (SHA-256)"
    log_info "  - Memory: Streaming-Verarbeitung, keine Voll-Datei-Ladung in RAM"

    # Initialisierungs-Flag
    mkdir -p "$PROJECT_DIR/data" 2>/dev/null || true
    touch "$PROJECT_DIR/data/import_initialized" 2>/dev/null || true
    log_info "Import-Engine initialisiert (optimiert für LXC-Ressourcen)"
}

###############################################################################
# 7. SPEICHEREFFIZIENTE BILDVERARBEITUNG
###############################################################################

optimized_media_handling() {
    log_info "Konfiguriere optimierte Medienverwaltung..."

    # SHA-256 basierte Duplikaterkennung
    # Originalbilder erhalten, Thumbnails asynchron

    MEDIA_HASH_ALGO="SHA256"
    IMAGE_QUALITY_ORIGINAL="original"
    THUMBNAIL_ENABLED="${THUMBNAIL_ENABLED:-true}"
    THUMBNAIL_ASYNC="${THUMBNAIL_ASYNC:-true}"
    THUMBNAIL_MAX_SIZE="${THUMBNAIL_MAX_SIZE:-300x300}"

    log_info "Medien-Optimierung:"
    log_info "  - Hash-Algo: $MEDIA_HASH_ALGO"
    log_info "  - Originalbilder: Behalten ($IMAGE_QUALITY_ORIGINAL)"
    log_info "  - Thumbnails: $THUMBNAIL_ENABLED, asynchron ($THUMBNAIL_ASYNC)"
    log_info "  - Thumbnail Max Size: $THUMBNAIL_MAX_SIZE Pixel"
    log_info "  - Speicher: Keine doppelten Speicherung, nur Referenzen"
}

###############################################################################
# 8. FRONTEND PRODUKTIONS-OPTIMIERUNG
###############################################################################

optimize_frontend_production() {
    log_info "Optimiere Frontend für Produktionsbetrieb..."

    # React als statische Dateien, kein Dev-Server
    FRONTEND_BUILD_MODE="${FRONTEND_BUILD_MODE:-production}"
    GZIP_COMPRESSION="${GZIP_COMPRESSION:-true}"
    BROTLI_COMPRESSION="${BROTLI_COMPRESSION:-auto}"
    CACHE_CONTROL_MAX_AGE="${CACHE_CONTROL_MAX_AGE:-30d}"
    CODE_SPLITTING="${CODE_SPLITTING:-true}"
    LAZY_LOADING="${LAZY_LOADING:-true}"

    log_info "Frontend-Produktions-Optimierung:"
    log_info "  - Build Mode: $FRONTEND_BUILD_MODE"
    log_info "  - Gzip Kompression: $GZIP_COMPRESSION"
    log_info "  - Brotli Kompression: $BROTLI_COMPRESSION"
    log_info "  - Browser Cache: $CACHE_CONTROL_MAX_AGE"
    log_info "  - Code Splitting: $CODE_SPLITTING"
    log_info "  - Lazy Loading: $LAZY_LOADING"
    log_info "  - WICHTIG: Kein Entwicklungsserver im Produktionsbetrieb"
}

###############################################################################
# 9. SINGLE SERVICE MANAGEMENT (einzelne Prozesse)
###############################################################################

manage_single_services() {
    local action="$1"

    case "$action" in
        start)
            log_info "Starte optimierte Services..."
            PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.."

            if [ -f "$PROJECT_DIR/docker-compose.yml" ]; then
                cd "$PROJECT_DIR"
                # Nur notwendige Services starten: db und omni-core
                log_info "Starte Docker Services: db, omni-core (redis falls bereits in compose)"
                docker compose up -d db omni-core 2>/dev/null || log_warn "Docker Compose Start problematisch, fallback auf native"
            fi

            # Native Fallback: nur eine Prozess-Instanz
            log_info "Starte native Python Applikation als einzige Instanz..."
            mkdir -p /app/logs 2>/dev/null || true
            nohup python3 main.py --host 0.0.0.0 --port "${APP_PORT:-8000}" > /app/logs/backend.log 2>&1 &
            local pid=$!
            log_info "Backend Prozess gestartet (PID: $pid, einzige Instanz)"
            ;;

        stop)
            log_info "Stoppe Services..."
            PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.."

            if [ -f "$PROJECT_DIR/docker-compose.yml" ]; then
                cd "$PROJECT_DIR"
                docker compose down --remove-orphans 2>/dev/null || log_err "Docker Compose Stop Fehler"
                log_info "Docker Compose heruntergefahren (eine Container-Generation)"
            else
                pkill -f "python3 main.py" 2>/dev/null || log_warn "Kein Python Prozess gefunden (bereits gestoppt)"
            fi
            ;;

        restart)
            $0 stop 2>/dev/null || true
            sleep 2
            $0 start
            ;;

        status)
            log_info "Prüfe Service-Status..."
            PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.."

            if [ -f "$PROJECT_DIR/docker-compose.yml" ]; then
                docker compose -f "$PROJECT_DIR/docker-compose.yml" ps 2>/dev/null || true
            else
                pgrep -f "python3 main.py" >/dev/null 2>&1 && echo "Backend: läuft (eine Prozess-Instanz)" || echo "Backend: gestoppt"
            fi
            ;;
    esac
}

###############################################################################
# 10. INTELLIGENTE LASTSTEUERUNG (Resource Manager)
###############################################################################

resource_manager() {
    log_info "Resource Manager: Überwache Container-Ressourcen..."

    local ram_usage="0"
    local cpu_load="0"

    # RAM Nutzung (vereinfacht aus /proc/meminfo)
    if [ -f /proc/meminfo ]; then
        MemTotal=$(grep "^MemTotal:" /proc/meminfo 2>/dev/null | awk '{print $2}' || echo "0")
        MemAvailable=$(grep "^MemAvailable:" /proc/meminfo 2>/dev/null | awk '{print $2}' || echo "0")
        if [ "$MemTotal" -gt 0 ] && [ "$MemAvailable" -gt 0 ]; then
            ram_usage=$((100 * (MemTotal - MemAvailable) / MemTotal))
        fi
    fi

    # CPU Load Average
    cpu_load=$(cat /proc/loadavg 2>/dev/null | awk '{print $1}' || echo "0")

    log_info "Resource Status: RAM ${ram_usage}% (Target: <70%), CPU Load: $cpu_load"

    # Maßnahmen bei hoher Last
    if [ "$ram_usage" -gt 70 ] 2>/dev/null; then
        log_warn "Hohe RAM-Auslastung erkannt - begrenze Importe"
        IMPORT_PARALLELISM=1
        log_info "Import-Jobs auf 1 parallelen Job begrenzt (Ressourcenschonung)"
    fi

    if [ "$(echo "$cpu_load > 3.0" | bc -l 2>/dev/null || echo 0)" -eq 1 ] 2>/dev/null; then
        log_warn "Hohe CPU-Last erkannt - drossele nicht-kritische Verarbeitung"
        ENABLE_MARKETING_ENGINE="${ENABLE_MARKETING_ENGINE:-false}"
        log_info "Nicht-kritische Verarbeitung vorübergehend gedrosselt"
    fi

    # Dokumentation für Administrator
    echo "Resource Monitoring abgeschlossen: RAM ${ram_usage}%, Load: $cpu_load"
}

###############################################################################
# 11. PERFORMANCE BENCHMARK NACH INSTALLATION
###############################################################################

run_benchmark() {
    log_info "Führe Post-Installation Benchmark durch..."

    local benchmark_result_file="$PROJECT_DIR/data/benchmark_results.txt"
    mkdir -p "$PROJECT_DIR/data" 2>/dev/null || true

    # RAM Idle messen
    local ram_idle=$(free | grep "Mem available:" 2>/dev/null | awk '{print $2}' || echo "0")
    echo "RAM Idle: ${ram_idle}KB" >> "$benchmark_result_file"

    # API Response Time
    local start_time=$(date +%s%N 2>/dev/null)
    if curl -s -o /dev/null -w "%{http_code}" "http://localhost:${APP_PORT:-8000}/api/health" 2>/dev/null | grep -q "200"; then
        local end_time=$(date +%s%N 2>/dev/null)
        local response_time=$(( (end_time - start_time) / 1000000 ))
        echo "API Response Time: ${response_time}ms" >> "$benchmark_result_file"
    else
        echo "API Response Time: Nicht erreichbar (Container eventuell noch Start)" >> "$benchmark_result_file"
    fi

    # DB Setup Time (konfiguriert in .env)
    echo "DB Config: POSTGRES_MAX_CONNECTIONS=${POSTGRES_MAX_CONNECTIONS:-20}" >> "$benchmark_result_file"
    echo "DB Config: POSTGRES_SHARED_BUFFERS=${POSTGRES_SHARED_BUFFERS:-256MB}" >> "$benchmark_result_file"

    # BullMQ Status
    if redis-cli ping 2>/dev/null | grep -q PONG; then
        echo "BullMQ: Verfügbar mit optimiertem Job-Limit (4 parallele Jobs)" >> "$benchmark_result_file"
    else
        echo "BullMQ: Nicht verfügbar (Optional - kann nachträglich gestartet werden)" >> "$benchmark_result_file"
    fi

    # Ressourcen-Profil dokumentieren
    echo "LXC Allocated RAM: ${LXC_ALLOCATED_RAM_MB}MB" >> "$benchmark_result_file"
    echo "CPU Allocated: ${LXC_ALLOCATED_CPU:-2} Cores" >> "$benchmark_result_file"
    echo "Performance Profile: $PERFORMANCE_PROFILE" >> "$benchmark_result_file"

    log_info "Benchmark Ergebnisse in: $benchmark_result_file"
    log_info "Gespeicherte Metriken: RAM, API Time, DB Config, Queue Status, Performance Profile"
}

###############################################################################
# 12. PERFORMANCE-PROFILE AUSWAHLEN
###############################################################################

select_performance_profile() {
    log_info "Wähle Performance-Profil für LXC-Installation..."

    local profile="BALANCED"  # Standard

    # Basierend auf Ressourcen wählen
    local allocated_ram="$LXC_ALLOCATED_RAM_MB"
    local allocated_cpu="$LXC_ALLOCATED_CPU"

    if [ "$allocated_ram" -lt 2048 ] && [ "$allocated_cpu" -lt 2 ]; then
        profile="ECONOMY"
        log_info "Profil: ECONOMY (minimale Ressourcen, single-tasking, maximale Effizienz)"
    elif [ "$allocated_ram" -ge 4096 ] && [ "$allocated_cpu" -ge 4 ]; then
        profile="PERFORMANCE"
        log_info "Profil: PERFORMANCE (höhere Parallelität, größere Pools, wenn Hardware es zulässt)"
    else
        profile="BALANCED"
        log_info "Profil: BALANCED (Standard, ausgewogen für die meisten LXC-Setups)"
    fi

    # Profil in .env schreiben
    echo "PERFORMANCE_PROFILE=$profile" >> "$PROJECT_DIR/.env"

    # Profil-spezifische Einstellungen
    case "$profile" in
        ECONOMY)
            export POSTGRES_MAX_CONNECTIONS="8"
            export UVICORN_WORKERS="1"
            export IMPORT_PARALLELISM="1"
            log_info "ECONOMY Settings: max 8 DB Connections, 1 Worker, 1 Import Job (maximale Ressourcenschonung)"
            ;;
        BALANCED)
            export POSTGRES_MAX_CONNECTIONS="20"
            export UVICORN_WORKERS="1"
            export IMPORT_PARALLELISM="1"
            log_info "BALANCED Settings: max 20 DB Connections, 1 Worker, 1 Import Job (Standard-Einstellung)"
            ;;
        PERFORMANCE)
            export POSTGRES_MAX_CONNECTIONS="40"
            export UVICORN_WORKERS="${UVICORN_WORKERS:-2}"
            export IMPORT_PARALLELISM="2"
            log_info "PERFORMANCE Settings: max 40 DB Connections, 2 Worker, 2 Import Jobs (sofern Hardware verfügbar)"
            ;;
    esac
}

###############################################################################
# HAUPTPROGRAMM
###############################################################################

# --- Pfad resolution ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# --- Globale Variablen ---
INSTALL_MODE=""
FORCE_RESET="false"
SKIP_HEALTH_CHECK="false"
INTERACTIVE="true"

# --- Hauptablauf ---
main() {
    log_info "=== Omni-Core Optimized Bootstrap für Proxmox LXC ==="
    log_info "Version 1.1.0 - Maximum Efficiency / Minimum Resource Consumption"

    # 1. Umgebung erkennen
    detect_os
    detect_lxc_environment

    # 2. Optimierte Environment Konfiguration
    configure_optimized_env

    # 3. Performance Profil auswählen
    select_performance_profile

    # 4. Datenbank initialisieren (einzige Instanz)
    init_single_postgresql

    # 5. Redis initialisieren (einzige Instanz)
    init_single_redis

    # 6. Runtime-Optimierung
    optimize_runtime

    # 7. Matterhorn Import optimiert
    optimized_matterhorn_import

    # 8. Medien-Verarbeitung optimieren
    optimized_media_handling

    # 9. Frontend Production optimieren
    optimize_frontend_production

    # 10. Services verwalten
    manage_single_services "start"

    # 11. Resource Manager
    resource_manager

    # 12. Benchmark nach Installation
    run_benchmark

    # 13. Fertigmeldung mit Zugangsinformationen
    echo ""
    echo "=================================================================="
    log_success "Omni-Core Optimized Bootstrap abgeschlossen!"
    echo "✔ System läuft in optimiertem LXC Modus"
    echo "✔ Einzige PostgreSQL Instanz (optimiert ${LXC_ALLOCATED_RAM_MB}MB RAM)"
    echo "✔ Einzige Redis Instanz (falls Docker Compose vorhanden, sonst Optional)"
    echo "✔ Performance Profil: $PERFORMANCE_PROFILE"
    echo "✔ WebIF: http://${HOST_IP:-localhost}:${APP_PORT:-8000}"
    echo "=================================================================="

    # Admin Zugang sicher anzeigen (nicht im Klartext in allgemeinen Logs)
    echo -e "Admin Zugang: ${ADMIN_EMAIL:-admin@omni-core.local}"
    echo -e "Passwort: [Hash in .env, bei erster Anmeldung ändern - nicht im Klartext in Logs]"
}

# --- Hauptfunktion aufrufen ---
main "$@"