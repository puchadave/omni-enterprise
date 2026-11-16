# Modul: Coding Swarm (on-demand)

On-demand Coding/Agent-Worker (Platzhalter-Image, anpassbar). Nutzt OmniRoute/Middleware-Endpoints aus `.env`.

## Start
```bash
cp .env.example .env
./start.sh up
./start.sh logs
```
- Workspace: `./workspace` (gemountet), Config: `./config`.
- Für produktiven Einsatz Image/Command auf euren Agent-Runner setzen (siehe `stacks/swarm/configs/coding-swarm/`).


## ZeroTier & Multi-Device (sicher, resilient)
- **Host-empfohlen:** ZeroTier auf dem Host installieren/joinen (`../../scripts/zerotier-host-setup.sh --network <ZT_NETWORK_ID>`), Gerät autorisieren, ZT-IP notieren. Peer-URLs in `.env` auf ZT-IPs setzen (z.B. `OMNIROUTE_URL=http://<ZT_IP>:20128/v1`).
- **Optional (Sidecar):** `docker compose --profile zt up -d` aktiviert den ZT-Container (erfordert `/dev/net/tun` & NET_ADMIN/SYS_ADMIN, `ZT_NETWORK_ID`).
- **Härtung:** `SERVICE_BIND_HOST` restriktiv setzen (Loopback/ZT-IP), nur benötigte Ports/Flows in ZT-Flow-Rules erlauben, starke Secrets/Keys, TLS/Auth für exponierte Dienste.
- **Resilienz:** `restart: unless-stopped`, Healthchecks, `depends_on: service_healthy` wo zutreffend; bei Netzpartition/ZT-Ausfall definierte Degradation (z.B. AI-Pfad 503) und Retry/Timeout-Policy beachten.
