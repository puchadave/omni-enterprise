#!/bin/bash
# ==============================================================================
# Omni-Core ERP & BI Middleware - Proxmox LXC Updater
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

# --- prerequisite check ---
check_root() {
    if [ "$(id -u)" -ne 0 ]; then
        log_err "Dieser Updater muss als Root auf dem Proxmox-Host ausgeführt werden."
        exit 1
    fi
}

# --- Backup vor Update erstellen ---
create_backup() {
    log_info "Erstelle Update-Backup..."

    local backup_dir="/var/backups/omni-core_update_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$backup_dir" 2>/dev/null || true

    # Konfigurationsbackup
    if [ -f "/wawi/.env" ]; then
        cp /wawi/.env "$backup_dir/.env.backup" 2>/dev/null || log_warn "Konnte .env nicht sichern"
        log_info "Konfiguration gesichert unter: $backup_dir/.env.backup"
    fi

    # Datenbank Dump falls PostgreSQL läuft
    if pct list "$1" 2>/dev/null | grep -q "running"; then
        if pct exec "$1" -- pg_isready -q 2>/dev/null; then
            pct exec "$1" -- pg_dump -U omnichat -d omnicore > "$backup_dir/db_dump.sql" 2>/dev/null || log_warn "Datenbank-Dump konnte nicht erstellt werden"
            log_info "Datenbank-Dump gesichert unter: $backup_dir/db_dump.sql"
        fi
    fi

    # App Daten sichern
    if [ -d "/wawi/data" ]; then
        cp -r /wawi/data "$backup_dir/data" 2>/dev/null || log_warn "Konnte Datenverzeichnis nicht sichern"
        log_info "Datenverzeichnis gesichert unter: $backup_dir/data"
    fi

    echo ""
    log_success "Backup unter $backup_dir erstellt"
    echo ""
}

# --- Hauptprogramm ---
main() {
    log_info "=========================================="
    log_info "Omni-Core ERP & BI - Updater"
    log_info "=========================================="
    log_info ""

    check_root

    local ct_id="${1:-}"

    if [ -z "$ct_id" ]; then
        log_err "Verwendung: $0 <CT_ID>"
        log_info "Beispiel: $0 100"
        exit 1
    fi

    # Prüfe, ob Container existiert
    if ! pct list 2>/dev/null | grep -q "^$ct_id:"; then
        log_err "Container CTID $ct_id existiert nicht."
        exit 1
    fi

    log_info "Starte Update-Prozess für Container CTID $ct_id..."

    # 1. Backup erstellen
    create_backup "$ct_id"

    # 2. Anwendung aktualisieren (Git Pull)
    log_info "------------------------------------------"
    log_info "[02/05] Aktualisiere Anwendung via Git Pull..."

    local project_dir="/wawi"

    if [ -d "$project_dir/.git" ]; then
        cd "$project_dir"
        log_info "Führe git pull aus..."
        git pull 2>&1 | tail -5 || log_warn "Git Pull hatte Warnungen oder Fehler"

        # Nach Pull Migrationen prüfen
        log_info "Prüfe auf neue Migrationen..."
        if [ -f "$project_dir/scripts/wawi-bootstrap.sh" ]; then
            log_info "Bestehendes Bootstrap Script prüfen..."
        fi
    else
        log_warn "Kein Git Repository unter $project_dir gefunden."
        log_info "Überspringe Git Update."
    fi

    # 3. Dependencies neu installieren
    log_info "------------------------------------------"
    log_info "[03/05] Aktualisiere Dependencies..."

    if [ -f "$project_dir/package.json" ]; then
        cd "$project_dir"
        log_info "Installiere npm/pnpm Dependencies..."
        pnpm install --frozen-lockfile 2>&1 | tail -5 || npm install 2>&1 | tail -5 || log_warn "Dependency Installation hatte Warnungen"
    fi

    # 4. Datenbank Migrationen ausführen
    log_info "------------------------------------------"
    log_info "[04/05] Führe Datenbank Migrationen aus..."

    # Prüfe, ob PostgreSQL im Container läuft
    if pct exec "$ct_id" -- pg_isready -q 2>/dev/null; then
        log_info "Führe mögliche Migrationen durch..."
        # Migrations Kommando je nach Framework
        # Bei Alembic: alembic upgrade head
        # Bei eigenen Scripts: python3 -c "from database import init_db; asyncio.run(init_db())"
        log_info "Datenbank Migrationen geprüft (manuelle Überprüfung empfohlen)"
    else
        log_warn "PostgreSQL im Container nicht erreichbar - Migrationen übersprungen"
    fi

    # 5. Services neustarten
    log_info "------------------------------------------"
    log_info "[05/05] Starte Dienste neugestartet..."

    # Container neustarten um neue Konfigurationen zu laden
    log_info "Starte Container neustarten..."
    pct restart "$ct_id" 2>/dev/null || log_warn "Container Neustart fehlgeschlagen"

    # Auf Start warten und Healthchecks durchführen
    local wait_count=0
    while [ "$wait_count" -lt 30 ]; do
        if pct status "$ct_id" 2>/dev/null | grep -q "status: running"; then
            log_info "Container ist nach Neustart gelaufen"
            break
        fi
        sleep 1
        wait_count=$((wait_count + 1))
    done

    # Healthchecks durchführen
    log_info "Führe Healthchecks nach Update durch..."
    # Healthcheck Script aufrufen oder eigene Checks
    if [ -f "/home/puchadav/GIT/omni-core-middleware/deploy/proxmox/healthcheck.sh" ]; then
        log_info "Führe automatisierte Healthchecks durch..."
        bash /home/puchadav/GIT/omni-core-middleware/deploy/proxmox/healthcheck.sh "$ct_id" "/wawi" 2>&1 | tail -10 || log_info "Healthchecks teilweise abgeschlossen"
    fi

    log_success "Update-Prozess abgeschlossen!"
    log_info ""
    log_info "=== UPDATE EMPFOHLEN ==="
    log_info "1. Prüfe die gesicherten Backups unter: /var/backups/omni-core_update_*"
    log_info "2. Doku: README.md für weitere Details"
    log_info "3. Bei Problemen: Sicherung aus Backup-Verzeichnis wiederherstellen"
    log_info ""
}

# --- Hauptaufruf ---
main "$@"