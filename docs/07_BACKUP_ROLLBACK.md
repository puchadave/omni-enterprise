# 07 – Backup / Restore / Rollback

## Grundsätze
- **Kein Datenverlust:** niemals produktive Volumes/DBs löschen oder überschreiben. Backups vor Updates/Änderungen.
- **Stateful:** Postgres (`pg_data`), Middleware (`app_data`, media/uploads), Shopware-Daten, Portainer-Daten.

## Backup (bereitgestellt/geplant)
- **Skript:** `scripts/backup-volumes.sh`
  - `list` – Volumes anzeigen (read-only).
  - `pg-dump --container <name> --db <db> [--user <u>] [--out backups]` – konsistenter SQL-Dump (gzip).
  - `volume --name <volume> [--out backups]` – Volume-Tar (read-only gemountet).
- **Empfehlung:** regelmäßige Dumps + Off-Host-Kopie (`backups/` gitignored), Aufbewahrungsregeln, Restore-Tests in isolierter Umgebung.
- **Konfig/Code:** `docker-compose.yml`, `.env.example`, Docs/Skripte versioniert; reale `.env`/Secrets separat und sicher verwahren.

## Restore (Leitfaden, risikobewusst)
1. Ziel-Dienst stoppen (nur betroffene Instanz), **keine** fremden produktiven Daten anfassen.
2. DB: neue/leere Ziel-DB oder verifizierte Restore-Instanz → `gunzip -c dump.sql.gz | docker exec -i <container> psql -U <user> <db>`.
3. Volume: in neues Volume entpacken und Dienst gegen neues Volume starten; erst nach Verifikation umschalten.
4. Integrität prüfen (Health, Stichproben, Logs) bevor Traffic.

## Rollback
- **Compose:** vorheriges Image-Tag/Config in Git wiederherstellen → `docker compose up -d` (betroffener Dienst). Kein `down -v`.
- **Swarm:** `docker service update --rollback <service>` bzw. vorherige Stack-Definition deployen; `start-first`/`failure_action: rollback` wo konfiguriert.
- **DB-Schema:** nur mit getesteten Down-Migrationen/Point-in-Time-Strategie; bei Unsicherheit Restore aus Dump bevorzugen.
- **Dokumentation:** jede Änderung/Version (Image-Tag, Config-Hash, Zeitpunkt) im Change-Log festhalten.
