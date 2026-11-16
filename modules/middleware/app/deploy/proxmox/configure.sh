#!/bin/bash
# ==============================================================================
# Omni-Core ERP & BI Middleware - Proxmox Container Configuration
# Author: puchadave <github.com/puchadave>
# Repository: https://github.com/puchadave/omni-core-middleware
# Version: 1.0.0
# ==============================================================================

set -Eeuo pipefail

# --- Farben ---
GREEN='\033[0;32m'
YELLOW='033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()   { echo -e "${RED}[ERROR]${NC} $1"; }

# --- Prüfe Voraussetzungen ---
check_prerequisites() {
    log_info "Prüfe Proxmox Host Voraussetzungen..."

    local missing="0"

    # Wichtige Proxmox Befehle
    for cmd in pct pvesm pvenode; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            log_warn "Proxmox Werkzeug nicht gefunden: $cmd"
            missing=$((missing + 1))
        fi
    done

    # Netzwerktools
    for cmd in ip curl; do
        if ! command -v "$cmd" >/dev/null 2>&1; then
            log_warn "Netzwerkwerkzeug nicht gefunden: $cmd"
            missing=$((missing + 1))
        fi
    done

    if [ "$missing" -gt 0 ]; then
        log_warn "Es fehlen $missing Werkzeuge. Einige Funktionen stehen möglicherweise nicht zur Verfügung."
    else
        log_info "Alle erforderlichen Proxmox-Werkzeuge vorhanden."
    fi

    # Speicher prüfen
    local total_mem=$(free -m 2>/dev/null | awk '/Mem:/ {print $2}' || echo "0")
    if [ "$total_mem" -lt 2048 ]; then
        log_warn "Wenig Arbeitsspeicher verfügbar ($total_mem MB). Empfohlen: mind. 4 GB."
    else
        log_info "Ausreichend Arbeitsspeicher: ${total_mem} MB."
    fi
}

# --- LXC Container Konfiguration ---
configure_lxc() {
    log_info "Konfiguriere LXC Container..."

    local ct_id="$1"
    local hostname="$2"

    if [ -z "$ct_id" ] || [ -z "$hostname" ]; then
        log_err "CTID und Hostname sind erforderlich."
        exit 1
    fi

    # Prüfe, ob Container existiert
    if ! pct list 2>/dev/null | grep -q "^$ct_id:"; then
        log_err "Container CTID $ct_id existiert nicht."
        exit 1
    fi

    # Container starten falls gestoppt
    log_info "Starte Container CTID $ct_id..."
    pct start "$ct_id" 2>/dev/null || log_warn "Konnte Container nicht starten"

    # Netzwerk konfigurieren
    log_info "Konfiguriere Netzwerk für CTID $ct_id..."
    # Bridge prüfen/konfigurieren
    local bridge="vmbr0"
    if pct list "$ct_id" 2>/dev/null | grep -q "net"; then
        log_info "Netzwerkschnittstelle bereits konfiguriert"
    else
        pct set "$ct_id" --net0 "name=net,bridge=$bridge,ip=dhcp" 2>/dev/null || log_warn "Netzwerkkonfiguration Teilweise fehlgeschlagen"
    fi

    # DNS einstellen
    pct set "$ct_id" --nameserver "8.8.8.8 8.8.4.4" 2>/dev/null || log_warn "DNS Konfiguration teilweise fehlgeschlagen"

    # Ressourcen prüfen und anpassen
    log_info "Prüfe und passe Ressourcen an..."
    # CPU Units setzen (wenn unterstützt)
    pct set "$ct_id" --cpuunits 1024 2>/dev/null || true

    # Memory prüfen
    local current_mem=$(pct config "$ct_id" 2>/dev/null | grep "^memory:" | awk '{print $2}' || echo "0")
    log_info "Aktueller Memory-Wert: ${current_mem} MB"

    # Disk prüfen
    local rootfs=$(pct config "$ct_id" 2>/dev/null | grep "^rootfs:" | awk '{print $2}' || echo "0")
    log_info "Aktueller Rootfs: ${rootfs}"

    # Hostname setzen
    pct set "$ct_id" --hostname "$hostname" 2>/dev/null || log_warn "Hostname konnte nicht gesetzt werden"

    log_info "LXC Container CTID $ct_id konfiguriert für Hostname $hostname"
}

# --- Service Konfiguration ---
configure_services() {
    log_info "Konfiguriere Dienste im Container..."

    local ct_id="$1"

    if [ -z "$ct_id" ]; then
        log_err "CTID ist erforderlich."
        exit 1
    fi

    # Prüfe, ob wir im Container executieren können
    if ! pct exec "$ct_id" -- echo "test" >/dev/null 2>&1; then
        log_warn "Keine Ausführung im Container möglich (SSH oder pct exec Problematik)"
        return 1
    fi

    log_info "Konfiguriere Dienste innerhalb von CTID $ct_id..."

    # PostgreSQL und Redis im Container sicherstellen
    pct exec "$ct_id" -- bash -c '
    # Inside Container
    set -euo pipefail

    log_info "Innere Container-Konfiguration..."

    # PostgreSQL sicherstellen
    if pg_isready -q; then
        log_info "PostgreSQL läuft im Container"
    else
        log_warn "PostgreSQL im Container nicht erreichbar"
        # Starte PostgreSQL
        service postgresql start 2>/dev/null || pg_ctlcluster 15 main start 2>/dev/null || true
    fi

    # Redis sicherstellen
    if redis-cli ping >/dev/null 2>&1; then
        log_info "Redis läuft im Container"
    else
        log_warn "Redis im Container nicht erreichbar"
        # Starte Redis
        redis-server --daemonize yes 2>/dev/null || true
    fi

    # Arbeitsverzeichnis sicherstellen
    mkdir -p /wawi/data /wawi/logs 2>/dev/null || true

    # .env Datei prüfen falls vorhanden
    if [ -f /wawi/.env ]; then
        log_info "Bestehende .env Datei im Container gefunden"
    else
        log_info "Erstelle Standard .env Datei im Container"
        cat > /wawi/.env <<ENVEOF
POSTGRES_USER=omnichat
POSTGRES_PASSWORD=$(openssl rand -hex 16)
POSTGRES_DB=omnicore
OMNI_CORE_PORT=8000
OMNIROUTE_URL=http://host.docker.internal:20128
OMNIROUTE_API_KEY=test-key
MATTERHORN_API_TOKEN=https://matterhorn-moda.de/xmldata/products_full.php?type=xml
ENVEOF
        log_info "Standard .env Datei erstellt"
    fi

    log_info "Container-Dienste konfiguriert"
    ' || log_warn "Teilweise Container-Konfiguration fehlgeschlagen"
}

# --- Healthchecks ---
run_healthchecks() {
    log_info "Führe Healthchecks durch..."

    local ct_id="$1"
    local results=""

    if [ -z "$ct_id" ]; then
        log_err "CTID ist erforderlich."
        return 1
    fi

    # 1. Container Status
    local cstatus=$(pct status "$ct_id" 2>/dev/null | grep -o 'status: [a-z]*' | head -1 | awk '{print $2}')
    if [ "$cstatus" = "running" ]; then
        results="${results}GREEN: Container läuft; "
    else
        results="${results}RED: Container nicht laufen; "
    fi

    # 2. Netzwerk Konnektivität
    if pct exec "$ct_id" -- ping -c 1 -W 2 8.8.8.g >/dev/null 2>&1; then
        results="${results}GREEN: Netzwerk funktioniert; "
    else
        results="${results}RED: Netzwerk problematisch; "
    fi

    # 3. PostgreSQL im Container
    if pct exec "$ct_id" -- pg_isready -q 2>/dev/null; then
        results="${results}GREEN: PostgreSQL erreichbar; "
    else
        results="${results}RED: PostgreSQL nicht erreichbar; "
    fi

    # 4. Redis im Container
    if pct exec "$ct_id" -- redis-cli ping >/dev/null 2>&1; then
        results="${results}GREEN: Redis erreichbar; "
    else
        results="${results}RED: Redis nicht erreichbar; "
    fi

    # 5. Backend API Test
    local backend_ip=$(pct exec "$ct_id" -- ip addr show 2>/dev/null | grep 'inet ' | head -1 | awk '{print $2}' | cut -d/ -f1 || echo "127.0.0.1")
    if curl -s "http://${backend_ip}:8000/api/health" >/dev/null 2>&1; then
        results="${results}GREEN: Backend API erreichbar; "
    else
        results="${results}RED: Backend API nicht erreichbar; "
    fi

    # 6. Frontend Test
    if curl -s "http://${backend_ip}:3000" >/dev/null 2>&1; then
        results="${results}GREEN: Frontend erreichbar; "
    else
        results="${results}YELLOW: Frontend nicht erreichbar (kann optional sein); "
    fi

    echo ""
    echo "Healthcheck Ergebnisse für CTID $ct_id:"
    echo "$results"
    echo ""

    # Zusammenfassung
    local error_count=$(echo "$results" | grep -o "RED:" | wc -l)
    if [ "$error_count" -gt 0 ]; then
        log_warn "Es gab $error_count kritische Healthcheck-Fehler."
        return 1
    elif [ "$error_count" -eq 0 ]; then
        log_success "Alle Healthchecks bestanden."
        return 0
    fi
}

# --- Hauptprogramm ---
main() {
    log_info "=========================================="
    log_info "Omni-Core ERP & BI - Container Konfiguration"
    log_info "=========================================="
    log_info ""

    check_prerequisites

    local ct_id="$1"
    local hostname="$2"

    if [ -z "$ct_id" ] || [ -z "$hostname" ]; then
        log_err "Verwendung: $0 <CTID> <Hostname>"
        log_info "Beispiel: $0 100 wawi"
        exit 1
    fi

    configure_lxc "$ct_id" "$hostname"
    configure_services "$ct_id"
    run_healthchecks "$ct_id"

    log_info ""
    log_info "Container Konfiguration abgeschlossen."
}