# OmniRoute Gateway – All-in-One (alle Configs & API-Keys)

Eigenständiger Docker-Container/Stack mit **allen OmniRoute-Konfigurationen und allen bekannten API-Keys** vorkonfiguriert (self-contained).

## Inhalt (gebündelt im Image, seed beim Start)
- `config/config.json` – vollständige Gateway-Konfiguration
- `config/omnicore-auto-combos.json` – Combos/Routing (`combos.optimized.json`)
- `config/endpoints.json` – Endpunkte
- Laufzeit-Seed nach `/root/.omniroute` und `/data` (persistente Volumes), falls dort noch nicht vorhanden.

## Alle OmniRoute API-Keys (vorbelegt)
| Zweck | Key |
|---|---|
| Primär (`OMNIROUTE_API_KEY`, `Authorization: Bearer …`) | `sk-814c9467fd9c9314-a41c41-72e2d3b0` |
| Zusätzlich (bekannt) | `test-key` |
| Gesamtliste (`OMNIROUTE_API_KEYS`, kommasepariert) | `sk-814c9467fd9c9314-a41c41-72e2d3b0,test-key` |

> Hinweis: Keys sind in `.env`/`.env.example` und im Entrypoint als Default hinterlegt. Für produktive Umgebungen Schlüssel rotieren/geheim halten.

## Start
```bash
cp .env.example .env   # anpassen falls nötig (Keys bereits gesetzt)
./start.sh up          # baut & startet
./start.sh health      # prüft /v1/models
```
- Endpoint: `http://<host>:${OMNIROUTE_PORT:-20128}/v1`
- Auth: `Authorization: Bearer <OMNIROUTE_API_KEY>`
- Optional ZeroTier-Sidecar: `./start.sh up --profile zt` (Host-ZT bevorzugt).

## Build / Betrieb
```bash
docker compose build
docker compose up -d
docker compose logs -f omniroute
```
- Image: `${OMNIROUTE_IMAGE:-ghcr.io/local/omniroute-gateway-allinone}:${OMNIROUTE_TAG:-1.0.0}`
- Persistenz: Volumes `*_config` (`/root/.omniroute`) und `*_data` (`/data`). Gebündelte Configs werden nur geseedet, wenn im Volume noch nicht vorhanden.
- Healthcheck: `GET /v1/models` (200/401/403 als erreichbar gewertet).
