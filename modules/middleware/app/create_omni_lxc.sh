#!/bin/sh
# ==============================================================================
# Omni-Core Proxmox LXC Automated Provisioning Script
# Erstellt einen Alpine Linux LXC-Container und installiert die Middleware.
# ==============================================================================

set -e

# --- KONFIGURATION ---
CT_ID=200                  # Container ID (Anpassen falls bereits vergeben)
CT_NAME="omni-core-middleware"
CT_PASSWORD="securepassword" # Root-Passwort für den Container
BRIDGE="vmbr0"             # Netzwerk-Bridge
IP_CONFIG="ip=dhcp"        # IP-Konfiguration (alternativ z.B. ip=192.168.1.50/24,gw=192.168.1.1)
STORAGE="local-lvm"        # Proxmox Storage Pool
CORES=1                    # CPU Kerne (Minimal für Alpine völlig ausreichend)
MEMORY=256                 # RAM in MB (< 30MB Laufzeit-Footprint)
SWAP=0                     # Kein Swap nötig

echo "==> [1/6] Suche neuestes Alpine Linux Template..."
pveam update
TEMPLATE=$(pveam available -section system | grep alpine | tail -n 1 | awk '{print $2}')
if [ -z "$TEMPLATE" ]; then
    echo "Fehler: Kein Alpine-Template gefunden!"
    exit 1
fi

# Template herunterladen, falls nicht vorhanden
pveam download $STORAGE $TEMPLATE || true
TEMPLATE_PATH="/var/lib/vz/template/cache/$TEMPLATE"

echo "==> [2/6] Erstelle Alpine LXC Container ID $CT_ID ($CT_NAME)..."
pct create $CT_ID $TEMPLATE_PATH \
    --ostype alpine \
    --hostname $CT_NAME \
    --cores $CORES \
    --memory $MEMORY \
    --swap $SWAP \
    --storage $STORAGE \
    --rootfs ${STORAGE}:2 \
    --net0 name=eth0,bridge=$BRIDGE,$IP_CONFIG \
    --onboot 1 \
    --start 0

echo "==> [3/6] Konfiguriere Container-Sicherheit & Passwort..."
pct set $CT_ID -password "$CT_PASSWORD"

echo "==> [4/6] Starte Container $CT_ID..."
pct start $CT_ID

# Warten, bis das Netzwerk im Container steht
echo "==> Warte auf Netzwerk-Initialisierung im Container..."
sleep 5

echo "==> [5/6] Installiere Systempakete & Omni-Core im Container..."
pct exec $CT_ID -- sh -c "
    apk update && apk upgrade
    apk add python3 py3-pip py3-virtualenv git sqlite curl build-base python3-dev sqlite-libs
    
    mkdir -p /opt/omni-middleware
    cd /opt/omni-middleware
    
    # Repository klonen
    if [ ! -d '.git' ]; then
        git clone https://github.com/puchadave/omni-core-middleware.git .
    else
        git pull origin main
    fi
    
    # Python Virtual Environment einrichten
    python3 -m venv venv
    . venv/bin/activate
    pip install --no-cache-dir --upgrade pip
    pip install --no-cache-dir -r requirements.txt
"

echo "==> [6/6] Richte Systemd Autostart-Dienst im Container ein..."
pct exec $CT_ID -- sh -c "
cat << 'EOF' > /etc/systemd/system/omni-middleware.service
[Unit]
Description=Omni-Core E-Commerce Middleware (Alpine Lean)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/omni-middleware
ExecStart=/opt/omni-middleware/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --loop uvloop
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable omni-middleware
systemctl start omni-middleware
"

echo "=============================================================================="
echo "✔ ERFOLG: Omni-Core LXC Container ($CT_ID) wurde erfolgreich erstellt und gestartet!"
echo "✔ Dashboard erreichbar unter: http://<Container-IP>:8000"
echo "=============================================================================="
