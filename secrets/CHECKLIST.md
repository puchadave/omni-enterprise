# Secrets & Härtung – Checkliste
- [ ] Alle `change_me_*`/Platzhalter/`__GENERATE__`-Reste in produktiven `.env` ersetzt (oder bewusst generiert).
- [ ] `.env` und `secrets/generated/*` nicht in Git; Rechte restriktiv.
- [ ] Starke, zufällige Keys (>=32 Bytes wo sinnvoll), getrennt pro Umgebung.
- [ ] Dashboards/APIs nur authentifiziert; Traefik-Dashboard nicht insecure in Prod.
- [ ] DB/Redis/Admin-Ports nicht unnötig öffentlich; Bind an Loopback/ZT/Firewall.
- [ ] ZT: nur freigegebene Geräte, Flow-Rules deny-by-default, Rollen segmentiert.
- [ ] Rotation: Plan für API-Keys/DB-Passwörter dokumentiert, Downtime-Fenster bedacht.
- [ ] Swarm: bevorzugt `docker secret`/`config` statt Klartext-Env für sensible Werte.
