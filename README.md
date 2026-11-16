# OMNI Enterprise – Modulares Docker/Compose/Swarm Projekt

Ein Projektordner mit **eigenständigen Modul-Servern** (je eigener `docker-compose.yml`), einem **Single-Host Gesamtstack** (eine Root-`docker-compose.yml`) und **verteiltem Docker-Swarm/Portainer**-Deployment (pve01–pve04).

## 1) Module einzeln starten (schnell & eigenständig)
Jedes Modul liegt in `modules/<name>/` und ist separat nutzbar:
```bash
cd modules/<name>
cp .env.example .env   # Werte/Secrets anpassen
./start.sh up          # up | down | restart | logs | status | pull | config
```
| Modul | Zweck | Standard-Port(s) | Hinweis |
|---|---|---|---|
| `middleware` | OMNI-CORE/ERP (FastAPI) + PostgreSQL | 8002 | self-contained (inkl. DB), Health `/healthz` |
| `omniroute` | AI-Gateway (OpenAI-kompatibel `/v1`) | 20128 | API-Key erforderlich |
| `shopware` | Shopware 6 (App + MariaDB, dev/dockware) | 8088 | Demo/Dev-Image; prod siehe Modul-README |
| `edge-proxy` | Traefik Reverse-Proxy | 80,443,8080 | Dashboard optional |
| `portainer` | Portainer CE (Docker UI) | 9443,9000 | verwaltet lokalen Docker |
| `marketing` | Marketing-Worker (Platzhalter/konfigurierbar) | - | skaliert/aktivierbar |
| `social` | Social-Media-Worker (Platzhalter) | - | skaliert/aktivierbar |
| `content-studio` | Content/AI-Worker (Platzhalter) | - | skaliert/aktivierbar |
| `coding-swarm` | Coding-Swarm Agent (on-demand) | - | Profil/On-demand |
| **`integrated`** | **Gebündelt:** Middleware+DB + AI/OmniRoute + Shopware + Social + Marketing + Content-Studio + Coding-Swarm | 8002,20128,8088 | alle Module vorkonfiguriert & in Middleware integriert (`./start.sh up`) |

> Ports via `.env` anpassbar, um Konflikte (z.B. live `:8002`) zu vermeiden.

## 2) Komplett auf einem Rechner (eine Datei)
Root-`docker-compose.yml` startet den Gesamtstack mit Profilen:
```bash
cp .env.example .env   # Secrets setzen (POSTGRES_PASSWORD, OMNIROUTE_API_KEY, ...)
# core = middleware+db+omniroute, full = alles
docker compose --profile core up -d
# oder
./start-all.sh up core
```
Profile: `core`, `ai`, `edge`, `commerce`, `ops`, `growth`, `full`.

## 3) Verteilt als Docker Swarm (Portainer)
Swarm/Portainer-Stacks unter `stacks/swarm/` (pve01–pve04, Labels edge/ai/growth/core):
```bash
# auf Manager (z.B. pve04)
./scripts/prepare-swarm-nodes.sh print
./scripts/prepare-swarm-nodes.sh label
docker stack deploy -c stacks/swarm/portainer.stack.yml portainer
# Enterprise-Stack via Portainer UI oder CLI
docker stack deploy -c stacks/swarm/omni-enterprise.stack.yml omni
```
Details & Web-Editor-Variante: **[PORTAINER.md](PORTAINER.md)**, `stacks/swarm/.env.example`.

## Voraussetzungen & Best Practices
- Docker Engine + Compose v2 (`docker compose`). Swarm: initialisiert & Nodes vernetzt.
- **Secrets rotieren** (nie Defaults produktiv), `.env`/`secrets/generated` gitignored.
- Middleware-Image für Swarm vorab bauen/verteilen: `scripts/build-and-distribute.sh`.
- Healthchecks & `depends_on: service_healthy` aktiv; Stateful-Dienste (DB/Middleware/Shopware) mit Volumes & Backups.

## Struktur
`modules/` (einzeln), `docker-compose.yml` (single-host), `stacks/swarm/` (verteilt), `scripts/`, `secrets/`, `shared/`, `docs/`.


## ZeroTier (Multi-Device, integriert)
- Sichere Kommunikation zwischen standalone Modulen auf unterschiedlichen Geräten über ZeroTier (Host-Install bevorzugt): `scripts/zerotier-host-setup.sh`.
- Pro Modul optionales Profil `zt` (`docker compose --profile zt up -d`) für ZT-Sidecar; Konfiguration via `.env` (`ZT_ENABLED/NETWORK_ID/NODE_NAME`, `SERVICE_BIND_HOST`).
- Architektur/Adressierung: `docs/ZEROTIER_ARCHITECTURE.md`, `shared/networks-and-addressing.md`. Smoke/Secrets/Backup: `scripts/smoke-test.sh`, `scripts/check-secrets.sh`, `scripts/backup-volumes.sh`.
