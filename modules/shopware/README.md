# Modul: Shopware

Schnellstartbares Shopware-Modul (Standard: `dockware/dev` mit integrierter DB für Dev/Demo).

## Start
```bash
cp .env.example .env
./start.sh up
# http://localhost:8088 (dockware Defaults je nach Tag, z.B. Admin oft /admin)
```

## Produktion (Empfehlung)
Für produktiven Betrieb separates Setup mit eigener MariaDB/Redis/(OpenSearch) und offiziellen Images/Build nutzen (siehe Shopware Hosting-Doku). Anbindung an Middleware via Shopware 6 API (URL, Client-ID/Secret, Sales-Channel) – Credentials erforderlich.


## ZeroTier & Multi-Device (sicher, resilient)
- **Host-empfohlen:** ZeroTier auf dem Host installieren/joinen (`../../scripts/zerotier-host-setup.sh --network <ZT_NETWORK_ID>`), Gerät autorisieren, ZT-IP notieren. Peer-URLs in `.env` auf ZT-IPs setzen (z.B. `OMNIROUTE_URL=http://<ZT_IP>:20128/v1`).
- **Optional (Sidecar):** `docker compose --profile zt up -d` aktiviert den ZT-Container (erfordert `/dev/net/tun` & NET_ADMIN/SYS_ADMIN, `ZT_NETWORK_ID`).
- **Härtung:** `SERVICE_BIND_HOST` restriktiv setzen (Loopback/ZT-IP), nur benötigte Ports/Flows in ZT-Flow-Rules erlauben, starke Secrets/Keys, TLS/Auth für exponierte Dienste.
- **Resilienz:** `restart: unless-stopped`, Healthchecks, `depends_on: service_healthy` wo zutreffend; bei Netzpartition/ZT-Ausfall definierte Degradation (z.B. AI-Pfad 503) und Retry/Timeout-Policy beachten.
