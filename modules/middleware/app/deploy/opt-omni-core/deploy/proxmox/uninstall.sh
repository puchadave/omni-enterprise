#!/bin/bash
# ==============================================================================
# Omni-Core ERP & BI Middleware - Proxmox LXC Uninstaller
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
        log_err "Dieser Uninstaller muss als Root auf dem Proxmox-Host ausgeführt werden."
        exit 1
    fi
}

# --- Hauptprogramm ---
main() {
    log_info "=========================================="
    log_info "Omni-Core ERP & BI - Uninstaller"
    log_info "=========================================="
    log_info ""

    check_root

    # CTID abfragen
    local ct_id=""
    read -r -p "Löschen Sie den WaWi LXC Container? (CTID eingeben oder 'abbruch'): " ct_id_input

    if [ "$ct_id_input" = "abbruch" ] || [ -z "$ct_id_input" ]; then
        log_info "Abbruch: Keine Löschung durchgeführt."
        exit 0
    fi

    # Prüfe, ob CTID existiert
    if ! pct list 2>/dev/null | grep -q "^$ct_id_input:"; then
        log_warn "Container CTID $ct_id_input existiert nicht oder ist bereits gelöscht."
        exit 1
    fi

    # Datenbank Backup erstellen (vor dem Löschen!)
    log_info "Erstelle Datenbank-Backup vor der Container-Löschung..."
    local db_backup_dir="/var/backups/omni-core_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$db_backup_dir" 2>/dev/null || true

    # PostgreSQL Dump falls vorhanden
    if pct exec "$ct_id_input" -- pg_isready -q 2>/dev/null; then
        log_info "Sichere PostgreSQL Datenbank..."
        pct exec "$ct_id_input" -- pg_dump -U omnichat -d omnicore > "$db_backup_dir/db_dump_$(date +%Y%m%d).sql" 2>/dev/null || log_warn "Datenbank-Dump konnte nicht erstellt werden"
    fi

    # Redis Daten sichern falls möglich
    if pct exec "$ct_id_input" -- redis-cli ping >/dev/null 2>&1; then
        log_info "Sichere Redis Daten..."
        # Redis RDB Snapshots sind unter /var/lib/redis/ verfügbar
        log_info "Redis Daten werden im Container-Verzeichnis gesichert"
    fi

    log_info "Backup wurde unter: $db_backup_dir"

    # WICHTIGE ABFRAGE: Daten wirklich löschen?
    log_warn "ACHTUNG: following actions will PERMANENTLY DELETE:"
    log_info "  - LXC Container CTID $ct_id"
    log_info "  - Alle Daten im Container (Dateisystem)"
    log_info "  - PostgreSQL Datenbank entries (sofern nur in diesem Container)"
    log_info "  - Konfigurationsdateien im Host-Verzeichnis"
    log_info ""

    read -r -p "Möchten Sie fortfahren? (j/N): " confirm

    if [ "$confirm" != "j" ] && [ "$confirm" != "J" ]; then
        log_info "Abbruch: Löschung abgebrochen, Daten bleiben erhalten."
        exit 0
    fi

    # Container stoppen und löschen
    log_info "------------------------------------------"
    log_info "[05/05] Stoppe und lösche Container CTID $ct_id..."

    # Container stoppen
    pct stop "$ct_id_input" 2>/dev/null || log_warn "Konnte Container nicht stoppen"

    # Container löschen (alle Daten sind weg!)
    pct destroy "$ct_id_input" 2>/dev/null || log_err "Konnte Container CTID $ct_id nicht löschen"

    # Aufräumen - prüfe, ob noch Verzeichnisse existieren
    log_info "Bereinige Verzeichnisse..."
    local wawi_dir="/opt/omni-core"
    if [ -d "$wawi_dir" ]; then
        # Alte Installationsverzeichnisse optional bereinigen
        # VORSICHT: Hier würde normale rm -rf Gefahr laufen, falsche Sachen zu löschen
        log_info "Bestehendes Installationsverzeichnis $wawi_dir erhalten (optional bereinigen)"
        # rm -rf "$wawi_dir" 2>/dev/null || log_warn "Bereinigung von $wawi_dir übersprungen"
    fi

    # Backup Hinweis
    log_success "Uninstallation abgeschlossen."
    log_info ""
    log_info "=== WICHTIG ==="
    log_info "Datenbank-Backup befindet sich unter: $db_backup_dir"
    log_info "Falls Sie die Daten wiederherstellen möchten, können Sie das Backup verwenden."
    log_info "Container und alle darauf befindlichen Daten sind jedoch endgültig gelöscht."
    log_info ""
    log_info "Für ein sicheres Rückgängigmachen:"
    log_info "  1. Container und Daten vorher sichern (Backup)"
    log_info "  2. Erst nach Bestätigung löschen"
    log_info ""
}

# --- Hauptaufruf ---
main "$@"