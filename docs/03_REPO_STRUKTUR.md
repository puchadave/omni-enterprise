# 03 – Repository-Struktur

```text
omni-enterprise/
  README.md                         # Einstieg, 3 Betriebsmodi
  PORTAINER.md                      # Swarm/Portainer Setup
  .gitignore                        # keine .env/Secrets/Runtime/Backups
  .env.example                      # Root Single-Host Variablen (Platzhalter)
  docker-compose.yml                # Single-Host Gesamtstack (Profile)
  start-all.sh                      # Wrapper für Root-Compose
  modules/                          # je Modul standalone
    <modul>/
      docker-compose.yml            # eigenständiger Start
      .env.example                  # dokumentierte Variablen/Platzhalter
      start.sh                      # up/down/logs/status/config/health (idempotent)
      README.md                     # Contract, Ports, ZT/Peer-Hinweise
      config/ data/ db/init/ ...    # modulspezifisch
  stacks/swarm/
    omni-enterprise.stack.yml       # Swarm (file-configs, Git/Repo)
    omni-enterprise.portainer-web.yml # Swarm (externe Configs)
    portainer.stack.yml
    .env.example
    configs/ (omniroute, placement, coding-swarm, proxy, ...)
  scripts/
    validate.sh                     # compose config / yaml checks (read-only)
    smoke-test.sh                   # Health/Erreichbarkeit
    check-secrets.sh                # Platzhalter/fehlende Secrets erkennen
    backup-volumes.sh               # Volume/DB-Backup-Helfer (sicher)
    inventory.sh                    # lokale Inventar-Erhebung
    zerotier-host-setup.sh          # Host-ZT Join-Hinweise/Checks (idempotent)
  shared/
    networks-and-addressing.md      # Adressierung, ZT, Peer-URLs
  secrets/
    templates/                      # Vorlagen (ohne echte Werte)
    generated/ (.gitkeep, gitignored)
  backups/ (.gitkeep, gitignored)
  docs/
    01_INVENTORY_IST.md
    02_ZIELARCHITEKTUR.md
    03_REPO_STRUKTUR.md
    04_MODUL_SERVICE_MATRIX.md
    05_IMPLEMENTIERUNG_UND_AENDERUNGEN.md
    06_VALIDIERUNGSPROTOKOLL.md
    07_BACKUP_ROLLBACK.md
    08_BETRIEB_MONITORING.md
    09_BLOCKER_OFFENE_PUNKTE.md
    10_ROADMAP.md
    ZEROTIER_ARCHITECTURE.md
    AUFTRAG_VINTED_API_ANBINDUNG.md
```
