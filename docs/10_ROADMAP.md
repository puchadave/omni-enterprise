# 10 – Nächste Schritte / Roadmap

## Priorität 1 (Sicherheit & Konnektivität)
1. ZeroTier-Netz anlegen, Geräte autorisieren, ZT-IP-Plan & Flow-Rules (deny-by-default) festlegen und in `shared/networks-and-addressing.md`/Inventar eintragen.
2. Produktive `.env`/Secrets pro Umgebung erzeugen & rotieren (`scripts/check-secrets.sh`), Admin-UIs absichern (TLS/Auth), Port-Bindings (`SERVICE_BIND_HOST`) härten.
3. Smoke-Tests end-to-end über ZT-Pfade (multi-device) in Testumgebung durchführen.

## Priorität 2 (Betrieb & Daten)
4. Backup-Jobs (DB-Dumps/Volumes) einrichten und Restore-Test durchführen.
5. Monitoring/Alerting (Health, Container, ZT, Ressourcen) aufsetzen; Log-Zentralisierung.

## Priorität 3 (Plattform-Rollout)
6. Single-Host/Modul-Start in freigegebener Testumgebung (`--profile core`), dann schrittweise Profile/Module.
7. Swarm: pve04 Manager, Nodes joinen/labeln, Images bauen/verteilen, Configs/Secrets anlegen, `stacks/swarm/*` deployen & verifizieren.
8. Shopware produktiv anbinden (Credentials/Channel) und Export/Order-Flows testen; Vinted nach Zugang (capability-gesteuert) implementieren & testen.

## Priorität 4 (Härtung & Qualität)
9. Prod-Shopware-Stack (gehärtet), optionale mTLS/TLS East-West, Image-Pinning/Digest, CI-Lint (compose/shell/hadolint).
10. Runbooks finalisieren (Incident, Rotation, DR) und regelmäßige Validierung automatisieren.
