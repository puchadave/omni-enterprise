# 02 – Zielarchitektur

## Übersicht
Einheitliches Repository mit drei Betriebsmodi:
1. **Modulweise (Standalone):** `modules/<dienst>/` mit eigener `docker-compose.yml`, `.env.example`, `start.sh`, README – einzeln startbar, auch auf getrennten Geräten.
2. **Single-Host Gesamtstack:** Root `docker-compose.yml` + Profile (`core|ai|edge|commerce|ops|growth|full`) startet alles auf einem Rechner.
3. **Swarm verteilt:** `stacks/swarm/*.stack.yml` (Portainer/Git/Web) mit Overlay-Netzen, Labels/Placement pve01–pve04, externen/file-Configs.

## Netzwerk- & Sicherheitsarchitektur
- **Interne Netze:** pro Modul eigenes Bridge-Netz; Root/Swarm gemeinsame Netze (`omni`, `edge`, `agent`).
- **ZeroTier-Overlay (integriert, optional/konfigurierbar):** sichere Host-übergreifende Kommunikation für standalone Multi-Device; Host-Installation bevorzugt, optionaler Container via Profil `zt`. Flow-Rules deny-by-default, ZT-IPs als Peer-Adressen.
- **Härtung:** Secrets nie im Git, starke generierte Keys, Auth auf APIs/Dashboards, TLS am Edge (Prod), minimale Port-Exposition (bevorzugt Loopback/ZT), Healthchecks, Restart-Policies, Least-Privilege-Hinweise.

## Datenfluss (vereinfacht)
- Edge (Traefik, pve01) → Middleware/Shopware/Portainer (core, pve04) über internes Netz/Ingress.
- Middleware (core) ↔ DB (core, lokal) und ↔ OmniRoute (ai, pve02 / ZT-Peer) für AI-Funktionen.
- Worker (growth/ai) ↔ Middleware/OmniRoute.
- Externe Anbindungen: Shopware-API, Vinted (wenn Zugang), OmniRoute-Provider – jeweils credential-/netzabhängig.

## Canonical Service Contracts (Soll, je Modul)
| Service | Zweck | Ports (intern/typ.) | Health | Volumes (State) | Abhängigkeiten | Kritische Env/Secrets | Update/Rollback |
|---|---|---|---|---|---|---|---|
| `middleware` (omni-core) | ERP/API/Core | 8000 (→8002) | `/healthz`/`/health` | `app_data`, media/uploads, DB extern | `db` healthy, `omniroute` (AI) | `DATABASE_URL`, `OMNIROUTE_*`, `SESSION/INTERNAL/SECRET/JWT` | start-first (Swarm), DB-Backup vor Update |
| `db` (postgres) | Persistenz | 5432 | `pg_isready` | `pg_data` (kritisch) | - | `POSTGRES_*` | pinned, Dump/Volume-Backup, restore-tested |
| `omniroute` | AI-Gateway `/v1` | 20128 | `/v1/models` (200/401/403) | config/data Volumes | - | `OMNIROUTE_API_KEY`, enc-key | restart-safe, config versioniert |
| `shopware` | Shop/Commerce | 80 (→8088) | HTTP probe | app/db/data (kritisch) | (DB/Redis prod-separat) | Shopware/App-Secrets, API-Creds | pinned, Backup, prod-Setup trennen |
| `edge-proxy` (traefik) | Ingress/TLS | 80,443,(8080) | Traefik intern | letsencrypt/data | Docker socket/provider | Dashboard-Auth, ACME | stateless, config rollout |
| `portainer` (+agent) | Mgmt UI/API | 9443,9000,9001 | UI/TCP | `portainer_data` | agent/socket | Admin-Creds | Backup `portainer_data` |
| `marketing` | Worker | - | Prozess/Logs | `worker_data` | middleware/omniroute | `OMNIROUTE_API_KEY`, URLs | stateless/queue-idempotent |
| `social` | Worker | - | Prozess/Logs | `worker_data` | middleware/omniroute | Keys/URLs | stateless |
| `content-studio` | Worker | - | Prozess/Logs | `worker_data`, config | omniroute/middleware | Keys/URLs | stateless |
| `coding-swarm` | On-demand Agent | - | Prozess/Logs | `worker_data`, workspace | omniroute | Keys, Policies | on-demand, audit |

> **Hinweis:** Exakte Contract-Details pro Modul in `modules/<name>/README.md` und `.env.example` pflegen.
