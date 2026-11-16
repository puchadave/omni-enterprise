# Modul: Integrated (Middleware + alle integrierten Module)

Bündelt **alle einzelnen Module, die in die Middleware integriert sein müssen**, in einem gemeinsamen Stack mit durchgängiger Verdrahtung (Netz, URLs, API-Keys, Auto-Integration).

## Enthaltene Module & Integration
| Bereich | Service(s) | Integration in Middleware |
|---|---|---|
| Core | `db`, `middleware` | Zentraler Dienst (API `:8002`, `/healthz`, `/ui/shopware`) |
| AI | `omniroute` (all-in-one) | `OMNIROUTE_URL=http://omniroute:20128/v1` + vorkonfigurierte Keys/Configs/Combos |
| Commerce | `shopware`, `shopware-bootstrap` | `SHOPWARE_URL=http://shopware:80`, Auto-Erstellung der Shopware-API-Integration & Registrierung in Middleware |
| Social Media | `social` | `MIDDLEWARE_URL`, `INTERNAL_API_KEY`, `OMNIROUTE_*` gesetzt |
| Marketing | `marketing` | `MIDDLEWARE_URL`, `INTERNAL_API_KEY`, `OMNIROUTE_*` gesetzt |
| Content/AI | `content-studio` | `MIDDLEWARE_URL`, `INTERNAL_API_KEY`, `OMNIROUTE_*` gesetzt |
| AI/Coding | `coding-swarm` | `MIDDLEWARE_URL`, `INTERNAL_API_KEY`, `OMNIROUTE_*` gesetzt |

Alle Dienste laufen im gemeinsamen Netz `omni-integrated_net` und erreichen Middleware/OmniRoute/Shopware per Service-Namen.

## Start
```bash
cp .env.example .env   # Secrets prüfen/setzen (POSTGRES_PASSWORD etc.), Ports bei Bedarf anpassen
./start.sh up          # baut & startet alles
./start.sh health      # Kurz-Check Middleware/OmniRoute/Shopware
./start.sh status|logs|down
```
- Middleware: `http://localhost:8002` (Health `/healthz`, Shopware-UI `/ui/shopware`)
- OmniRoute (AI): `http://localhost:20128/v1` (Bearer-Key in `.env`, all-in-one mit allen Configs/Keys)
- Shopware: `http://localhost:8088` – `shopware-bootstrap` erstellt/registriert die API-Integration automatisch in der Middleware (idempotent; Credentials/Artefakte in `../shopware/integration/`).

## Konfiguration
- Zentrale `.env`: DB/Secrets, OmniRoute-Keys, Shopware-Parameter, Images/Ports, `SERVICE_BIND_HOST`/ZeroTier (`--profile zt`).
- OmniRoute liefert alle gebündelten Configs/Combos/Keys (Modul `omniroute-allinone`).
- Shopware: für extern erreichbare Setups `SHOPWARE_URL`/Netzwerk entsprechend anpassen; Sales-Channel/produktive Credentials nach Bedarf ergänzen.

## Hinweise
- `social/marketing/content-studio/coding-swarm` sind als integrierte Worker-Container (Platzhalter-Image, konfigurierbar via `*_IMAGE`) mit voller Middleware/AI-Verdrahtung bereit; produktive Worker-Images/Commands je nach Implementierung setzen.
- Ressourcen: Stack umfasst DB, Middleware-Build, Shopware – ausreichend RAM/CPU/Disk einplanen.
- Keine Live-Prod-Instanzen verändern; Port-Konflikte zu bestehenden Diensten (`:8002`) via `.env` vermeiden.
