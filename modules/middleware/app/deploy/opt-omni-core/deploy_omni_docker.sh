#!/bin/bash
#
# deploy_omni_docker.sh
# Deployment script for Omni-Core Middleware Docker Container.
# Starts the Docker Compose application in detached mode.
#

set -euo pipefail

# --- Farben für Ausgabe ---
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# --- Prere Checks ---
log_info "Überprüfe Docker Installation..."
if ! command -v docker &>/dev/null; then
    log_err "Docker ist nicht installiert. Abbruch."
    exit 1
fi

log_info "Überprüfe Docker Compose..."
if ! command -v docker compose &>/dev/null && ! docker compose version &>/dev/null; then
    log_err "Docker Compose ist nicht verfügbar. Abbruch."
    exit 1
fi

# --- In Projektverzeichnis wechseln ---
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${PROJECT_DIR}" || {
    log_err "Konnte Projektverzeichnis nicht wechseln nach ${PROJECT_DIR}. Abbruch."
    exit 1
}

log_info "Wechsle in Projektverzeichnis: ${PROJECT_DIR}"

# --- Docker Image bauen ---
log_info "Baue Docker Image..."
docker build -t omni-core-middleware . || {
    log_err "Docker Build fehlgeschlagen! Abbruch."
    exit 1
}

log_ok "Docker Image erfolgreich gebaut: omni-core-middleware"

# --- Container starten ---
log_info "Starte Docker Compose..."
docker compose up -d || {
    log_err "Docker Compose Start fehlgeschlagen! Abbruch."
    exit 1
}

log_ok "Docker Compose wurde gestartet."

# --- Health Check ---
log_info "Warte auf Service-Bereitschaft (max. 10 Sekunden)..."
for i in $(seq 1 20); do
    STATUS=$(docker compose ps -q omni-core-middleware | xargs -r docker inspect --format='{{.State.Health.Status}}' 2>/dev/null || echo "unknown")
    if [ "${STATUS}" = "healthy" ]; then
        log_ok "Service ist healthy und bereit."
        break
    fi
    if [ "$i" -eq 20 ]; then
        log_warn "Service erreichte nach 10 Sekunden nicht den Status 'healthy'. Status: ${STATUS}"
    fi
    sleep 0.5
done

log_info ""
log_info "=========================================="
log_ok "  Omni-Core Middleware Deployment abgeschlossen"
log_info "=========================================="
log_info ""
log_info "Dienst erreichbar unter: http://localhost:8000"
log_info "Dokumentation:         http://localhost:8000/docs"
log_info "Health Check:          curl http://localhost:8000/"
log_info ""
log_info "Verwaltung:"
log_info "  • Logs anzeigen:       docker compose logs -f"
log_info "  • Service neustarten:  docker compose restart"
log_info "  • Status prüfen:     docker compose ps"
log_info ""

exit 0