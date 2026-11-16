# Netzwerke & Adressierung (Single-Host / Multi-Device ZeroTier / Swarm)

## Prinzipien
- **Single-Host (Compose):** Interne Kommunikation über Docker-Bridge-Netz und Service-DNS (`omniroute`, `db`, `middleware`, `shopware`). Keine Abhängigkeit von ZeroTier.
- **Multi-Device (Standalone-Module auf verschiedenen Geräten):** Kommunikation über **ZeroTier**-Overlay (empfohlen) oder vertrauenswürdiges LAN. Peers werden per **stabilen ZT-IPs** in `.env` adressiert (`OMNIROUTE_URL`, `MIDDLEWARE_URL`, `SHOPWARE_URL` etc.). Ports nur minimal/gezielt exponieren.
- **Swarm:** Intra-Cluster über Overlay-Netze (`omni/edge/agent`) und Service-DNS; ZeroTier primär für Management/Admin-Zugriff und (optional) Cross-Site – nicht als Ersatz für Swarm-Overlay ohne explizites Design.

## Konventions-Variablen (pro Modul/.env)
| Variable | Bedeutung | Beispiel (Single-Host) | Beispiel (Multi-Device via ZT) |
|---|---|---|---|
| `SERVICE_BIND_HOST` | Bind/Publish-Host für Ports | `0.0.0.0` (lokal) / `127.0.0.1` (härter) | `0.0.0.0` + ZT-Flow-Rules **oder** konkrete ZT-IP (falls unterstützt) |
| `ZT_ENABLED` | Optionalen ZT-Container/Profil aktivieren | `false` | `true` (oder Host-ZT bevorzugt) |
| `ZT_NETWORK_ID` | ZeroTier Netzwerk-ID (16 hex) | leer | `<netz-id>` |
| `ZT_NODE_NAME` | Anzeigename/Identifikation | `omni-<modul>-<host>` | pro Gerät eindeutig |
| `OMNIROUTE_URL` | Endpoint für AI-Gateway | `http://omniroute:20128/v1` | `http://<ZT_IP_OMNIROUTE>:20128/v1` |
| `OMNIROUTE_API_KEY` | Auth zu OmniRoute | Secret | gleiches Secret (verteilt über sicheren Kanal) |
| `MIDDLEWARE_URL` | Middleware-Basis | `http://middleware:8000` | `http://<ZT_IP_MIDDLEWARE>:8002` |
| `DATABASE_URL` | DB (nur wo nötig) | intern `db:5432` | **Default:** DB kolokiert; Remote-DB nur ZT+TLS+Auth (Risiko) |

## Abhängigkeits-/Port-Matrix (Soll)
| Quelle | Ziel | Port/Proto | Zweck | Hinweis |
|---|---|---|---|---|
| middleware/worker | omniroute | 20128/tcp HTTP(S) | LLM/API | API-Key, Timeouts/Retries |
| middleware | db (postgres) | 5432/tcp | Persistenz | bevorzugt lokal/same-host |
| edge-proxy | middleware/shopware/portainer | 8000/80/9000 | Routing | TLS am Edge empfohlen |
| admin/user | edge/portainer/middleware | 80/443/9443/8002 | UI/API | nur benötigte exponieren, Auth |
| alle (ZT) | ZeroTier UDP | 9993/udp (typ.) | Overlay | Firewall/Host erlauben |

## Resilienz-Hinweise
- **Timeouts/Retries/Backoff:** Clients (middleware↔omniroute, Worker↔APIs) mit konfigurierbaren Timeouts, begrenzten Retries + Jitter; 429/5xx retryable.
- **Health & Startreihenfolge:** `depends_on: service_healthy`, Healthchecks, `restart: unless-stopped`/Swarm-Restart, start-first Updates.
- **Degradation:** OmniRoute/ZT-Ausfall → AI-Pfade liefern kontrollierten Fehler/503, Nicht-AI-Pfade bleiben nach Möglichkeit verfügbar.
- **Failure-Domains:** Stateful Dienste gepinnt/kolokiert, Backups (DB-Dump/Volumes), idempotente Jobs, klare Runbooks.
