# 04 – Modul-/Service-Matrix

| Modul | Pfad (Compose) | Zweck | Default Ports | State/Volumes | Abhängigkeiten | ZT/Multi-Device Hinweis | Profile/Modi |
|---|---|---|---|---|---|---|---|
| middleware | `modules/middleware/docker-compose.yml` | OMNI-CORE/ERP API | 8002→8000 | pg_data, app_data, media/uploads (kritisch) | db (healthy), omniroute (AI, konfigurierbar) | `OMNIROUTE_URL` auf ZT-Peer setzen; DB bevorzugt lokal | standalone, root `core/full`, swarm `core` |
| omniroute | `modules/omniroute/docker-compose.yml` | AI-Gateway `/v1` | 20128 | omniroute_config/data | - | von middleware/workern via ZT-IP erreichbar machen | standalone, root `core/ai/full`, swarm `ai` |
| shopware | `modules/shopware/docker-compose.yml` | Shop (Dev/Demo dockware) | 8088→80 | shopware_data/db (kritisch) | - (prod: eigene DB/Redis) | für Prod extern/gehärtet; API-Anbindung credential-pflichtig | standalone, root `commerce/full`, swarm `core` |
| edge-proxy | `modules/edge-proxy/docker-compose.yml` | Traefik Ingress | 80,443,8080 | letsencrypt | Docker provider | TLS/Dashboard-Auth in Prod; nur nötige Ports | standalone, root `edge/full`, swarm `edge` |
| portainer | `modules/portainer/docker-compose.yml` | Docker/Swarm UI | 9443,9000,(8000) | portainer_data | socket/agent | Admin nur über ZT/VPN + HTTPS | standalone, root `ops/full`, swarm/agent |
| marketing | `modules/marketing/docker-compose.yml` | Worker (Platzhalter) | - | worker_data | middleware/omniroute | Peer-URLs/Keys via ZT | standalone, root `growth/full`, swarm `growth` |
| social | `modules/social/docker-compose.yml` | Worker (Platzhalter) | - | worker_data | middleware/omniroute | Peer-URLs/Keys via ZT | standalone, root `growth/full`, swarm `growth` |
| content-studio | `modules/content-studio/docker-compose.yml` | Worker (Platzhalter) | - | worker_data | middleware/omniroute | Peer-URLs/Keys via ZT | standalone, root `ai/full`, swarm `ai` |
| coding-swarm | `modules/coding-swarm/docker-compose.yml` | On-demand Agent | - | worker_data, workspace | omniroute | Policies lokal, Keys via ZT | standalone, root `ai/full`, swarm `ai` |

## Health/Resilienz (übergreifend)
- **Restart:** `unless-stopped` (Compose) / Swarm `restart_policy` + ggf. start-first Updates.
- **Healthchecks:** DB `pg_isready`, Middleware `/healthz`, OmniRoute `/v1/models` (200/401/403 erreichbar), HTTP-Probes für Webdienste.
- **Reihenfolge:** `depends_on.condition: service_healthy` wo anwendbar.
- **Netz:** restriktive Bindings/Exposure empfohlen, ZT-Flow-Rules least-privilege.

## Env/Secrets (Kern, nie committen)
- **Global/OmniRoute:** `OMNIROUTE_API_KEY`, `OMNIROUTE_STORAGE_ENCRYPTION_KEY`.
- **Middleware/DB:** `POSTGRES_PASSWORD`, `DATABASE_URL`, `SESSION_SECRET`, `INTERNAL_API_KEY`, `SECRET_KEY`, `JWT_SECRET`.
- **ZT:** `ZT_NETWORK_ID` (nur nötig wenn Join/Profil), Host-ZT bevorzugt.
- **Commerce:** Shopware/Vinted API-Credentials (extern, Blocker bis bereitgestellt).
