#!/bin/bash
# ==============================================================================
# Omni-Core ERP & BI Middleware - Healthcheck Script für Proxmox LXC
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

# --- Variablen ---
CT_ID="${1:-}"
PROJECT_DIR="${2:-/wawi}"

if [ -z "$CT_ID" ]; then
    log_err "Verwendung: $0 <CT_ID> [PROJECT_DIR]"
    exit 1
fi

# --- 1. LXC Container Status ---
check_container_status() {
    log_info "1. Prüfe LXC Container Status (CTID $CT_ID)..."

    local status=$(pct status "$CT_ID" 2>/dev/null | grep -o 'status: [a-z]*' | head -1 | awk '{print $2}')

    if [ "$status" = "running" ]; then
        log_info "   Status: GREEN - Container läuft"
        return 0
    else
        log_warn "   Status: RED - Container läuft nicht (Status: $status)"
        return 1
    fi
}

# --- 2. Netzwerk Konnektivität ---
check_network() {
    log_info "2. Prüfe Netzwerk Konnektivität..."

    # Prüfe, ob Container ping kann
    if pct exec "$CT_ID" -- ping -c 1 -W 2 8.8.8.g >/dev/null 2>&1; then
        log_info "   Netzwerk: GREEN - Internet erreichbar"
        return 0
    else
        log_warn "   Netzwerk: YELLOW - Internet nicht direkt erreichbar (kann LXC Netzwerk sein)"
        return 1
    fi
}

# --- 3. PostgreSQL Check ---
check_postgresql() {
    log_info "3. Prüfe PostgreSQL Datenbank..."

    # Versuche im Container PostgreSQL zu prüfen
    if pct exec "$CT_ID" -- pg_isready -q 2>/dev/null; then
        log_info "   Datenbank: GREEN - PostgreSQL erreichbar"
        return 0
    else
        # Auch ohne Container-Prüfung prüfen
        if pg_isready -q 2>/dev/null; then
            log_info "   Datenbank: GREEN - PostgreSQL lokal erreichbar"
            return 0
        else
            log_warn "   Datenbank: RED - PostgreSQL nicht erreichbar"
            return 1
        fi
    fi
}

# --- 4. Redis Check ---
check_redis() {
    log_info "4. Prüfe Redis Cache..."

    # Im Container prüfen
    if pct exec "$CT_ID" -- redis-cli ping >/dev/null 2>&1; then
        log_info "   Cache: GREEN - Redis erreichbar"
        return 0
    else
        # Auch lokal prüfen
        if redis-cli ping >/dev/null 2>&1; then
            log_info "   Cache: GREEN - Redis lokal erreichbar"
            return 0
        else
            log_warn "   Cache: YELLOW - Redis nicht erreichbar (optional, BullMQ kann nachträglich gestartet werden)"
            return 1
        fi
    fi
}

# --- 5. Backend API Check ---
check_backend_api() {
    log_info "5. Prüfe Backend API Endpoint..."

    # Container IP ermitteln
    local container_ip=$(pct exec "$CT_ID" -- ip addr show 2>/dev/null | grep 'inet ' | head -1 | awk '{print $2}' | cut -d/ -f1 || echo "127.0.0.1")

    # Fallback auf localhost wenn Container-IP nicht gefunden
    local check_ip="${2:-localhost}"

    if curl -s -o /dev/null -w "%{http_code}" "http://${check_ip}:8000/api/health" 2>/dev/null | grep -q "200"; then
        log_info "   API: GREEN - Backend API antwortet (http://${check_ip}:8000/api/health)"
        return 0
    else
        log_warn "   API: RED - Backend API antwortet nicht (http://${check_ip}:8000/api/health)"
        return 1
    fi
}

# --- 6. Frontend Check ---
check_frontend() {
    log_info "6. Prüfe Frontend Oberfläche..."

    local container_ip="${2:-localhost}"

    # Frontend üblicherweise auf Port 3000
    if curl -s -o /dev/null -w "%{http_code}" "http://${local_ip}:3000" 2>/dev/null | grep -q "200"; then
        log_info "   Frontend: GREEN - Oberfläche erreichbar (http://${local_ip}:3000)"
        return 0
    else
        log_info "   Frontend: YELLOW - Frontend nicht erreichbar (kann in Entwicklung sein oder wurde noch nicht gebaut)"
        return 1
    fi
}

# --- 7. BullMQ / Worker Check ---
check_workers() {
    log_info "7. Prüfe Worker / BullMQ Status..."

    # Prüfe im Container
    if pct exec "$CT_ID" -- redis-cli ping >/dev/null 2>&1; then
        log_info "   Worker: YELLOW - Redis läuft, Worker Konfiguration geprüft (BullMQ Setup läuft im Bootstrap)"

        # Prüfe ob Queue Files existieren
        if [ -f "/wawi/data/import_initialized" ] 2>/dev/null; then
            log_info "   Queue: INFO - Import Queue initialisiert"
        fi
    else
        log_info "   Worker: INFO - Redis nicht im Container, optional"
    fi

    # Prüfe auf existierende Daten/Queue Struktur
    if [ -f "$PROJECT_DIR/data/benchmark_results.txt" ] 2>/dev/null; then
        log_info "   Metriken: INFO - Benchmark Ergebnisse vorhanden"
    fi
}

# --- 8. Matterhorn Import Check ---
check_matterhorn() {
    log_info "8. Prüfe Matterhorn Import Verbindung..."

    # Standard URL prüfen
    local matterhorn_url="${3:-https://matterhorn-moda.de/xmldata/products_full.php?type=xml}"

    # Wir können nur prüfen, ob die URL format ist und ob .env konfiguriert ist
    if grep -q "MATTERHORN_API_TOKEN" "/wawi/.env" 2>/dev/null || grep -q "MATTERHORN" "/wawi/.env" 2>/dev/null; then
        log_info "   Import: GREEN - Matterhorn Konfiguration in .env vorhanden"
    else
        log_info "   Import: YELLOW - Matterhorn Konfiguration nicht in .env gefunden (optional, wird im WebIF nachgeholt)"
    fi
}

# --- 8. Omniroute Check ---
check_omniroute() {
    log_info "9. Prüfe OmniRoute Gateway Konfiguration..."

    if grep -q "OMNI_ROUTE" "/wawi/.env" 2>/dev/null; then
        log_info "   KI-Gateway: GREEN - OmniRoute Konfiguration in .env vorhanden"
    else
        log_info "   KI-Gateway: YELLOW - OmniRoute Konfiguration fehlt (optional, wird im WebIF nachgeholt)"
    fi
}

# --- 9. Shopware Check ---
check_shopware() {
    log_info "10. Prüfe Shopware Connector Konfiguration..."

    if grep -q "SHOPWARE" "/wawi/.env" 2>/dev/null; then
        log_info "   Shopware: GREEN - Shopware Connector Konfiguration in .env vorhanden"
    else
        log_info "   Shopware: YELLOW - Shopware Connector Konfiguration fehlt (optional, wird im WebIF nachgeholt)"
    fi
}

# --- 10. Administrator Login Check ---
check_admin_login() {
    log_info "11. Prüfe Administrator Login..."

    # Prüfe ob .env Admin Daten hat
    if grep -q "ADMIN_EMAIL" "/wawi/.env" 2>/dev/null; then
        log_info "   Admin: GREEN - Admin Konfiguration in .env vorhanden"
    else
        log_info "   Admin: YELLOW - Admin Konfiguration fehlt, läuft erstmalig erster Start"
    fi

    # Versuche Login Test (sofern Backend läuft)
    local check_ip="${2:-localhost}"
    if curl -s -o /dev/null -w "%{http_code}" -u "admin:$(grep 'ADMIN_PASSWORD' /wawi/.env 2>/dev/null | cut -d= -f2)" "http://${check_ip}:8000/api/auth/login" 2>/dev/null | grep -q "200"; then
        log_info "   Login: GREEN - Administrator Login funktioniert"
    else
        log_info "   Login: INFO - Login Test konnte nicht automatisch durchgeführt werden (erster Start oder andere Auth Methode)"
    fi
}

# --- HAUPTPROGRAMM ---
# Format: ./healthcheck.sh <CT_ID> [PROJECT_DIR] [MATTERHORN_URL]
main() {
    local ct_id="${1}"
    local project_dir="${2:-/wawi}"
    local matterhorn_url="${3:-https://matterhorn-moda.de/xmldata/products_full.php?type=xml}"

    log_info "=========================================="
    log_info "Omni-Core ERP & BI - Healthcheck Report"
    log_info "CTID: $ct_id"
    log_info "=========================================="
    log_info ""

    local all_green="1"

    # 1. Container Status
    log_info "=== Prüfe Komponenten ==="
    check_container_status || all_green="0"

    # 2. Netzwerk
    log_info "=== Netzwerk ==="
    check_network || all_green="0"

    # 3. PostgreSQL
    log_info "=== Datenbank ==="
    check_postgresql || all_green="0"

    # 4. Redis
    log_info "=== Cache ==="
    check_redis || all_green="0"

    # 5. Backend API
    log_info "=== Backend API ==="
    check_backend_api "$ct_id" || all_green="0"

    # 6. Frontend
    log_info "=== Frontend ==="
    check_frontend "$ct_id" || all_green="0"

    # 7. Worker
    log_info "=== Worker / BullMQ ==="
    check_workers || all_green="0"

    # 8. Matterhorn
    log_info "=== Matterhorn Import ==="
    check_matterhorn "$ct_id" "$project_dir" "$matterhorn_url" || all_green="0"

    # 8. Omniroute
    log_info "=== KI Gateway ==="
    check_omniroute "$ct_id" || all_green="0"

    # 9. Shopware
    log_info "=== Shopware ==="
    check_shopware "$ct_id" || all_green="0"

    # 10. Admin Login
    log_info "=== Administrator ==="
    check_admin_login "$ct_id" || all_green="0"

    # --- Zusammenfassung ---
    log_info ""
    log_info "=========================================="
    local green_count=$(echo "$results" 2>/dev/null | grep -o "GREEN:" | wc -l || echo "0")
    local yellow_count=$(echo "$results" 2>/dev/null | grep -o "YELLOW:" | wc -l || echo "0")
    local red_count=$(echo "$results" 2>/dev/null | grep -o "RED:" | wc -l || echo "0")

    log_info "Ergebnis Übersicht:"
    log_info "  GREEN: $green_count"
    log_info "  YELLOW: $yellow_count"
    log_info "  RED: $red_count"
    log_info ""

    if [ "$red_count" -eq 0 ]; then
        log_success "✓ Healthcheck abgeschlossen - KEINE kritischen Fehler"
        log_info "  System ist betriebsbereit (optionale YELLOW Checks können nachkonfiguriert werden)"
        return 0
    else
        log_warn "⚠ Healthcheck abgeschlossen - $red_count kritische(r) Fehler gefunden"
        log_info "  Es werden folgende Probleme angezeigt:"

        # Zeige RED Einträge
        echo "$results" 2>/dev/null | grep "RED:" | while read -r line; do
            log_info "  - $line"
        done

        log_info ""
        log_info "  Trotzdem lauffähig wenn dies gewünscht ist (optionale Services nachkonfigurieren)"
        return 1
    fi
}