# Shopware Deployment – Architektur & WebUI-Vorbereitung

## Komponenten

- `deploy/shopware-proxmox/`: Proxmox One-Click Installer (LXC, Shopware, Performance, Security, Middleware, Backup, Healthcheck)
- `shopware-connector/`: API-only Connector-Struktur für Middleware↔Shopware
- `deploy/shopware-proxmox/config.yaml`: zentrale, maschinenlesbare Konfiguration

## WebUI-Vorbereitung (Middleware-WebUI)

Die Konfiguration ist YAML/JSON-basiert und versionierbar. Für spätere UI-Verwaltung vorgesehen:

- **Proxmox**: `proxmox.*` (Modus, Node/API, Storage, Bridge)
- **Container**: `container.*` (CTID, Ressourcen, Netz, Storage, Autostart)
- **Shopware**: `shopware.*` (Version, URL, Admin/Shop-Daten)
- **API/Integration**: `middleware.*`, `shopware-connector/config/connector.json`
- **Import/Sync**: `mapping/*`, `sync.*`, Intervalle, Batches
- **Preis/Medien**: `mapping/pricing|media`, Regeln in Middleware (führend)
- **Webhooks/Queue**: `webhook_base_url`, Worker/Timer-Status via Healthcheck/Monitoring
- **Backup/Monitoring**: `backup.*`, `monitoring.*`, JSON-Reports

Empfehlung: UI liest/schreibt validierte Config-Objekte und erzeugt daraus Deployment-Artefakte; Secrets nie im Klartext anzeigen/speichern (nur Referenzen/rotierbar).

## Status & Reports

- Deployment: `deployment.report_file` (Schritte, Exit-Codes, Zeitstempel)
- Health: `monitoring.report_json` (Pflichtprüfungen, PASS/FAIL/WARN)
- Abschlussstatus: `SUCCESS` nur bei bestandenen Pflichtprüfungen.
