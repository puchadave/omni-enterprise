# Modul: Middleware (OMNI-CORE/ERP)

Eigenständiger Stack: FastAPI-Middleware + PostgreSQL (self-contained). Enthält Vinted-Connector-Skeleton (capability-gesteuert, `config_required` bis Zugang bestätigt).

## Start
```bash
cp .env.example .env   # Secrets werden bei Bedarf auto-generiert
./start.sh up
./start.sh health      # http://localhost:8002/healthz
```

## Konfiguration
- DB: `POSTGRES_*`, persistentes Volume `pg_data`, Init `db/init/*.sql`.
- OmniRoute-Anbindung: `OMNIROUTE_URL`/`OMNIROUTE_API_KEY` in `.env` setzen (z.B. Modul `omniroute` auf Port 20128 oder externer Gateway).
- Medien ausgelagert: `MEDIA_PATH`, `UPLOADS_PATH` (Host-Pfade/Volumes).
- Port: `OMNI_CORE_PORT` (Default 8002) – bei Konflikt mit laufender Instanz ändern.

## Hinweise
- Healthcheck `/healthz`, `depends_on: service_healthy` (DB), `restart: unless-stopped`.
- Shopware/Vinted produktiv: Connector-Credentials/Zugang erforderlich (siehe `docs/ANBINDUNG_SHOPWARE_VINTED.md`).


## ZeroTier & Multi-Device (sicher, resilient)
- **Host-empfohlen:** ZeroTier auf dem Host installieren/joinen (`../../scripts/zerotier-host-setup.sh --network <ZT_NETWORK_ID>`), Gerät autorisieren, ZT-IP notieren. Peer-URLs in `.env` auf ZT-IPs setzen (z.B. `OMNIROUTE_URL=http://<ZT_IP>:20128/v1`).
- **Optional (Sidecar):** `docker compose --profile zt up -d` aktiviert den ZT-Container (erfordert `/dev/net/tun` & NET_ADMIN/SYS_ADMIN, `ZT_NETWORK_ID`).
- **Härtung:** `SERVICE_BIND_HOST` restriktiv setzen (Loopback/ZT-IP), nur benötigte Ports/Flows in ZT-Flow-Rules erlauben, starke Secrets/Keys, TLS/Auth für exponierte Dienste.
- **Resilienz:** `restart: unless-stopped`, Healthchecks, `depends_on: service_healthy` wo zutreffend; bei Netzpartition/ZT-Ausfall definierte Degradation (z.B. AI-Pfad 503) und Retry/Timeout-Policy beachten.
