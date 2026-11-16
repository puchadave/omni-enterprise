#!/bin/sh
#
# create_omni_lxc.sh
# Automated setup script for Omni-Core Middleware on Alpine Linux LXC.
# Configuriert einen frischen Alpine LXC-Container auf Proxmox VE.
# Non-Interactive Mode (vollautomatisch).
#

set -euo pipefail

# --- Farben und Emojis ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
EMOJI_INFO="ℹ️"
EMOJI_OK="✅"
EMOJI_WARN="⚠️"
EMOJI_ERR="❌"

log_info() {
    echo -e "${GREEN}${EMOJI_INFO} ${1}${NC}"
}

log_ok() {
    echo -e "${GREEN}${EMOJI_OK} ${1}${NC}"
}

log_warn() {
    echo -e "${YELLOW}${EMOJI_WARN} ${1}${NC}"
}

log_err() {
    echo -e "${RED}${EMOJI_ERR} ${1}${NC}"
}

# ===========================================================================
# 1. SYSTEM-VORBEREITUNG & PAKETE
# ===========================================================================
log_info "Starte Alpine System-Updates und installiere Pakete..."

apk update && apk upgrade -a --no-cache || {
    log_err "Apk-Update fehlgeschlagen! Abbruch."
    exit 1
}

log_info "Installiere notwendige Systempakete..."
apk add --no-cache \
    python3 \
    py3-pip \
    py3-virtualenv \
    git \
    sqlite \
    curl \
    build-base \
    python3-dev \
    libffi-dev \
    sqlite-libs || {
    log_err "Paket-Installation fehlgeschlagen! Abbruch."
    exit 1
}

log_ok "Alle Systempakete erfolgreich installiert."

# ===========================================================================
# 2. REPOSITORY KLONEN
# ===========================================================================
APP_DIR="/opt/omni-middleware"
REPO_URL="https://github.com/puchadave/omni-core-middleware.git"

log_info "Sichere Zielverzeichnis ${APP_DIR}..."
mkdir -p "${APP_DIR}"

log_info "Prüfe auf bestehendes Repository..."
if [ -d "${APP_DIR}/.git" ]; then
    log_info "Repository existiert bereits. Update via git pull..."
    cd "${APP_DIR}" && git pull || {
        log_err "Git Pull fehlgeschlagen! Abbruch."
        exit 1
    }
else
    log_info "Klone Repository von GitHub nach ${APP_DIR}..."
    git clone "${REPO_URL}" "${APP_DIR}" || {
        log_err "Git Clone fehlgeschlagen! Abbruch."
        exit 1
    }
fi

log_ok "Repository erfolgreich gesichert/geklont in ${APP_DIR}."

# ===========================================================================
# 3. PYTHON VIRTUAL ENVIRONMENT & DEPENDENCIES
# ===========================================================================
VENV_DIR="${APP_DIR}/venv"

log_info "Erstelle Python Virtual Environment bei ${VENV_DIR}..."
python3 -m venv "${VENV_DIR}" || {
    log_err "Virtual Environment Erstellung fehlgeschlagen! Abbruch."
    exit 1
}

log_info "Aktiviere Virtual Environment und aktualisiere pip..."
PATH="${VENV_DIR}/bin:${PATH}"

pip install --upgrade pip --no-cache-dir || {
    log_err "pip Update fehlgeschlagen! Abbruch."
    exit 1
}

log_info "Installiere Python-Abhängigkeiten (optimiert für Alpine/musl)..."
pip install --no-cache-dir \
    fastapi==0.110.0 \
    uvicorn[standard]==0.28.0 \
    uvloop==0.19.0 \
    httpx==0.27.0 \
    pydantic==2.6.4 \
    aiosqlite==0.20.0 \
    jinja2==3.1.3 \
    python-multipart==0.0.9 || {
    log_err "Python-Pakete Installation fehlgeschlagen! Abbruch."
    exit 1
}

log_ok "Alle Python-Pakete erfolgreich installiert."

# ===========================================================================
# 4. SYSTEMD AUTOMAISIERUNG-DIENST
# ===========================================================================
SERVICE_FILE="/etc/systemd/system/omni-middleware.service"

log_info "Erstelle systemd Service Konfiguration unter ${SERVICE_FILE}..."

cat > "${SERVICE_FILE}" << EOF
[Unit]
Description=Omni-Core Middleware FastAPI Application
After=network.target

[Service]
User=root
Group=root
WorkingDirectory=/opt/omni-middleware
ExecStart=/opt/omni-middleware/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --loop uvloop
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

log_info "Setze Berechtigungen für Service-Datei..."
chmod 644 "${SERVICE_FILE}" || {
    log_err "Service-Datei konnte nicht beschreibbar gemacht werden! Abbruch."
    exit 1
}

log_info "Lade systemd Daemon neu..."
systemctl daemon-reload || {
    log_warn "systemctl daemon-reload konnte nicht ausgeführt werden (kann in LXC-Container ohne vollständiges systemd auftreten)."
}

log_info "Aktiviere Service für automatischen Start..."
systemctl enable omni-middleware.service || {
    log_warn "systemctl enable konnte nicht ausgeführt werden (kann in LXC-Container ohne volle systemd Unterstützung auftreten)."
}

log_info "Starte Service sofort..."
systemctl start omni-middleware.service || {
    log_warn "systemctl start konnte nicht ausgeführt werden (kann in LXC-Container ohne vollständiges systemd auftreten)."
}

log_ok "Systemd Service Konfiguration abgeschlossen."

# ===========================================================================
# 5. GESUNDHEITSCHECK (Health Check)
# ===========================================================================
log_info "Warte 3 Sekunden auf Initialisierung des Services..."
sleep 3

log_info "Führe Health Check per curl durch..."
HEALTH_URL="http://localhost:8000/"
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${HEALTH_URL}" || echo "000")

if [ "${HTTP_CODE}" = "200" ]; then
    log_ok "FastAPI Server ist erreichbar unter ${HEALTH_URL}"
    log_ok "Status-Code: ${HTTP_CODE}"
else
    log_warn "Health Check lief mit Status-Code: ${HTTP_CODE}"
    log_warn "Der Dienst wird möglicherweise noch gestartet. Überprüfen Sie später den Status mit: systemctl status omni-middleware.service"
fi

log_info ""
log_ok "============================================"
log_ok "  Omni-Core Middleware Installation abgeschlossen"
log_ok "============================================"
log_info ""
log_info "Zugriffspunkte:"
log_info "  • API:               http://<Container-IP>:8000"
log_info "  • interaktive Doku: http://<Container-IP>:8000/docs"
log_info "  • Gesundheitscheck: curl http://<Container-IP>:8000/"
log_info "  • Systemd Status:    systemctl status omni-middleware.service"
log_info "  • Logs ansehen:      journalctl -u omni-middleware.service -f"
log_info ""

exit 0