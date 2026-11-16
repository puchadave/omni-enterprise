# 06 – Validierungsprotokoll

## Automatisierte Checks (ausgeführt)
- `docker compose -f docker-compose.yml --env-file .env.example config` → **OK** (Root).
- `docker compose -f modules/*/docker-compose.yml config` → **OK** für alle Module (coding-swarm, content-studio, edge-proxy, marketing, middleware, omniroute, portainer, shopware, social).
- `python3 -c "yaml.safe_load(...)"` für `stacks/swarm/*.yml` → **OK**.
- `bash -n` für `start-all.sh`, `scripts/*.sh`, `modules/*/start.sh` → **OK**.

> Hinweis: Compose kann Warnungen zu nicht gesetzten Variablen ausgeben (z.B. `ZT_NETWORK_ID` leer bei opt-in Profil); Konfiguration bleibt syntaktisch/strukturell gültig.

## Nicht ausgeführt (und warum)
- **`docker compose up` / Builds / Live-Smoke:** nicht gestartet, um keine Port-Konflikte/Ressourcenbindung mit laufenden produktiven Diensten (u.a. `:8002`, Swarm-Workloads) zu riskieren und ohne freigegebene Zielumgebung. Smoke-Tests als `scripts/smoke-test.sh` bereitgestellt (read-only).
- **Swarm `stack deploy`:** nicht ausgeführt (erfordert verifizierten Manager, Labels, Images, Secrets/Configs, Freigabe).
- **Secret-Scan gegen reale `.env`:** `scripts/check-secrets.sh` bereitgestellt; produktive `.env`-Werte bewusst nicht angelegt/committet.

## Empfohlene Verifikation vor Einsatz (pro Modus)
- **Modul/Single-Host (Testumgebung):** `cp .env.example .env` + Secrets setzen → `./scripts/check-secrets.sh` → `./start-all.sh up core` (oder Modul) → `docker compose ps` → `./scripts/smoke-test.sh`.
- **Multi-Device/ZeroTier:** ZT-IPs verifizieren (`zerotier-cli listnetworks`), Peer-URLs setzen, Connectivity `curl` über ZT, dann Smoke-Tests.
- **Swarm:** `docker node ls`/Labels prüfen, Images verteilen, Configs/Secrets anlegen, `docker stack deploy` (Test), Services/Health prüfen.
