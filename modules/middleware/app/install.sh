#!/bin/sh
set -e

echo "==> [1/4] Starte Omni-Core Direkt-Installation von GitHub..."

# Docker prüfen
if ! command -v docker >/dev/null 2>&1; then
    echo "Installiere Docker..."
    apt-get update && apt-get install -y docker.io docker-compose-v2
fi

# Verzeichnis vorbereiten
INSTALL_DIR="/opt/omni-core"
mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

echo "==> [2/4] Lade Repository-Inhalte..."
if [ -d ".git" ]; then
    git pull origin main
else
    git clone https://github.com/puchadave/omni-core-middleware.git .
fi

echo "==> [3/4] Baue Docker-Container (Python 3.11 Alpine)..."
docker compose down || true
docker compose up --build -d

HOST_IP=$(hostname -I | awk '{print $1}')

echo "=================================================================="
echo "✔ OMNI-CORE ERFOLGREICH INSTALLIERT UND GESTARTET!"
echo "✔ Dashboard erreichbar unter: http://${HOST_IP:-localhost}:8000"
echo "=================================================================="