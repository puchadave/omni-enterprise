# 01 – Inventar / Ist-Zustand (Bestandsaufnahme)

Stand: 2026-11-16 (Erhebung im Projektkontext). Keine destruktiven Änderungen durchgeführt.

## 1. Lokale Docker-Umgebung (Host der Erhebung)
- **Engine/Compose:** Docker Engine (Swarm aktiv), Docker Compose v2 verfügbar.
- **Laufende Container (relevant, nicht anfassen):**
  - `310b4d4eb7c1` `omni-core-middleware-omni-core-1` (Image `omni-core-middleware-omni-core`, **healthy**, Port `0.0.0.0:8002->8000`) – **Produktiv, unverändert lassen**.
  - `1c51e239de30` `omni-core-middleware-db-1` (`postgres:15-alpine`, **healthy**, DB `omnicore`) – **Produktiv, unverändert lassen**.
  - Swarm-Dienste: `wawi_wawi-middleware.*` (healthy), `portainer_portainer.1.*`, `portainer_agent.*` (9001), diverse beendete Vorversionen.
- **Volumes (Auszug):** `omni-core-middleware_app_data`, `omni-core-middleware_pg_data`, `wawi_*`, `portainer_portainer_data` u.a. (keine Löschung/Verschiebung).
- **Netzwerke (Auszug):** `bridge`, `host`, `docker_gwbridge`, Swarm `ingress`, Overlay `agent_network`, `wawi-overlay`, `wawi_wawi-overlay`, `omni-core-middleware_omni-net`.
- **Swarm/Secrets/Configs:** Swarm aktiv; keine Docker-Secrets/Configs zum Zeitpunkt der Erhebung angelegt (zu verifizieren vor Swarm-Rollout).
- **ZeroTier:** auf dem Erhebungshost zum Zeitpunkt nicht als CLI installiert/konfiguriert vorgefunden (hostseitig separat zu prüfen/pro Gerät).

## 2. Bekannte Infrastruktur (aus Projektkontext, zu verifizieren)
| Node | IP | Rolle (geplant) | Hinweise |
|---|---|---|---|
| pve01 | 192.168.178.250 | edge | FUTRO S920, 2C/4G |
| pve02 | 192.168.178.251 | ai | CT101 vorhanden (Middleware/OmniRoute-Bundles deployed/testweise), Docker in CT |
| pve03 | 192.168.178.252 | growth | FUTRO S920 |
| pve04 | 192.168.178.254 | core/manager | HP Z420 4C8T/32G, Docker, Swarm-Zielmanager |

> **Hinweis:** Exakte Hostnamen, Swarm-Join-Status, Storage-Pfade und ZeroTier-Mitgliedschaften müssen vor produktivem Rollout per `scripts/inventory-remote.sh` (oder manuell) verifiziert und in `docs/INVENTORY_LIVE.md` gepflegt werden.

## 3. Repository-Ist (`/mnt/social.puchalla.pro/omni-enterprise`)
- **Module (`modules/`):** `middleware` (App+DB, Build-Kontext vorhanden), `omniroute`, `shopware` (dockware/dev), `edge-proxy` (Traefik), `portainer`, `marketing`, `social`, `content-studio`, `coding-swarm` – je `docker-compose.yml`, `.env.example`, `start.sh`, `README.md`.
- **Root:** `docker-compose.yml` (Single-Host, Profile), `.env.example`, `start-all.sh`.
- **Swarm:** `stacks/swarm/omni-enterprise.stack.yml`, `omni-enterprise.portainer-web.yml`, `portainer.stack.yml`, `.env.example`, `configs/`.
- **Vorhanden:** `docs/`, `scripts/` (teilweise leer), `secrets/templates|generated`, `shared/`, `backups/`.
- **Auffälligkeiten (zu härten):** Platzhalter-Secrets (`change_me_*`, `__GENERATE__`-Flows teils vorhanden), Traefik-Dashboard insecure in Beispielen, Ports teils auf `0.0.0.0`, keine ZeroTier-Integration, keine einheitlichen Smoke-/Secret-/Backup-Skripte, Swarm-Secrets/Configs nicht produktiv ausgerollt.

## 4. Abhängigkeiten (fachlich, grob)
- `middleware` → `db` (Postgres, gesund), optional/typisch → `omniroute` (AI), extern → Shopware/Connectoren (Credentials nötig).
- `omniroute` eigenständig (API-Key), konsumiert von middleware/workern.
- `shopware` eigenständig; Anbindung an middleware via API (Credentials/Channel).
- `edge-proxy` routet zu Diensten; `portainer` verwaltet Docker/Swarm.
- Worker (`marketing|social|content-studio|coding-swarm`) → `middleware`/`omniroute` (konfigurierbar).

## 5. Blocker / Unklare Punkte
- **Produktiv-Credentials fehlen/unbestätigt:** Shopware (URL/Client/Secret/Sales-Channel) und Vinted-Zugang (Partner/Pro, Endpunkte, Webhooks) – Anbindung bleibt `config_required`/dokumentiert.
- **ZeroTier:** Netzwerk-ID, Mitglieder, Rollen-IPs, Flow-Rules und Controller (Central/self-hosted) nicht festgelegt → Integration als optionales, konfigurierbares Modul/Profil vorgesehen.
- **Swarm-Rollout:** pve04 Manager/Join/Labels/Overlay-Erreichbarkeit und Image-Distribution zu verifizieren; DB-Init im Swarm (kein Host-Bind) gesondert zu behandeln.
- **Live-Schutz:** Keine Änderung an `310b4d4eb7c1`, `1c51e239de30`, wawi-/portainer-Produktiv-Containern/Volumes/Netzen ohne explizite Freigabe.
