# Proxmox One-Click Shopware 6 Deployment (Middleware Edition)

Automatisierte, idempotente Bereitstellung einer produktionsnahen Shopware-6-Instanz in einem unprivilegierten Debian-13-LXC auf Proxmox VE – optimiert für die Anbindung der Omni-Core-Middleware.

## Installation per Link (One-Click Bootstrap)

Auf dem **Proxmox-VE-Host** als `root` ausführen:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/feature/full-admin_webui_control_center/deploy/shopware-proxmox/install.sh)"
```

Mit eigener Konfiguration (empfohlen für Produktion):

```bash
CONFIG_URL="https://example.com/shopware-config.yaml" \
bash -c "$(curl -fsSL https://raw.githubusercontent.com/puchadave/omni-core-middleware/feature/full-admin_webui_control_center/deploy/shopware-proxmox/install.sh)"
```

Alternativ lokale Konfigurationsdatei:

```bash
git clone --depth 1 --branch feature/full-admin_webui_control_center https://github.com/puchadave/omni-core-middleware.git /opt/omni-shopware-deploy-src
cd /opt/omni-shopware-deploy-src/deploy/shopware-proxmox
cp config.yaml config.production.yaml
# config.production.yaml anpassen (Domain, CTID, Ressourcen, SSL, Backup-Ziel, etc.)
bash ./deploy-shopware.sh --config ./config.production.yaml
```

## Was das System macht

1. **Proxmox/LXC**: Erstellt/konfiguriert unprivilegierten Debian-13-LXC (Standard CTID 210, 4 vCPU, 8 GB RAM, 64 GB, `vmbr0`, Autostart). Storage/Netzwerk werden validiert/auto-detektiert.
2. **OS & Stack**: Installiert NGINX, PHP-FPM (kompatibel zur gewählten Shopware-Version), MariaDB/MySQL (kompatibel), Redis, Composer, Node.js-Abhängigkeiten nach Bedarf, OPcache/APCu und PHP-Extensions.
3. **Shopware**: Ermittelt aktuelle stabile Version (oder nutzt Pin), prüft Kompatibilität, installiert `shopware/production`, führt `system:install`, Migrationen, Cache/Theme/Assets/Index und Scheduled-Task-Registrierung aus.
4. **Performance**: Berechnet PHP-FPM/OPcache/DB/Redis-Parameter ressourcenbasiert (keine pauschale Überlastung).
5. **Sicherheit**: Firewall (UFW), SSH-Härtung, TLS (Let’s Encrypt / Reverse-Proxy / self-signed / deaktiviert), Secrets-Dateien mit restriktiven Rechten, Least-Privilege-Middleware-User.
6. **Middleware-Vorbereitung**: Erstellt Connector-Struktur unter `/opt/omni-shopware-connector`, schreibt maschinenlesbare `config/connector.json`, vorbereitet Health/Webhook-Integration. **Keine direkten DB-Schreibzugriffe der Middleware** – nur offizielle Shopware-APIs.
7. **Backup/Recovery**: Tägliche Backups (DB, Dateien, Konfiguration, Medien, Plugins, Connector), Rotation, Kompression, Integritätsprüfung, Restore-Skript, optionaler Restore-Test.
8. **Monitoring & Abnahme**: Healthchecks, JSON-Reports (`deployment.report_file`, `monitoring.report_json`), Pflichtprüfungen mit Status `SUCCESS`/`PARTIAL_SUCCESS`/`FAILED`.

## Skripte

| Datei | Zweck |
|---|---|
| `deploy-shopware.sh` | Haupt-Orchestrator, One-Click-Ausführung, Locking, Schrittprotokoll, Report |
| `install.sh` | Remote-Bootstrap per Installationslink (Clone + Start) |
| `config.yaml` | Zentrale, versionierbare Konfiguration |
| `install-container.sh` | Proxmox-Erkennung, Template/Storage/Netz, CT-Erstellung & OS-Bootstrap |
| `install-shopware.sh` | PHP/DB/Redis/NGINX/Composer/Shopware-Installation & Admin-Erstellung |
| `configure-performance.sh` | Ressourcenbasierte Performance-Tunings |
| `configure-security.sh` | Firewall, SSH, TLS, Permissions, Fail2ban/Updates |
| `configure-middleware.sh` | API-Integration, Connector-Vorbereitung, Webhook/Health-Marker |
| `setup-backups.sh` | Backup/Rotation/Integrität/Restore/optional Restore-Test |
| `healthcheck.sh` | Pflicht-Akzeptanztests + JSON-Monitoring |
| `uninstall-shopware.sh` | Kontrollierte Deinstallation **nur** des konfigurierten Containers |

## Konfiguration & Secrets

- Alle Parameter in `config.yaml` (keine hartcodierten Infrastrukturwerte).
- Passwörter/Secrets werden bei Leerwerten sicher generiert und unter `security.secrets_dir` (Default `/root/.omni-shopware-secrets`, `chmod 700/600`) abgelegt.
- Keine Secrets in Logs oder Git. `.env`/Secrets-Verzeichnisse nicht committen.
- Umgebungsvariablen: `PROXMOX_API_TOKEN_SECRET`, `SHOPWARE_ADMIN_PASSWORD`, `SHOPWARE_DB_PASSWORD`, `CONFIG_URL`, `REPO_URL`, `REPO_REF`.

## SSL/DNS-Verhalten

- `ssl.mode=letsencrypt`: DNS/Domain muss erreichbar sein (`deployment.require_dns_for_ssl=true` standardmäßig). Bei fehlendem DNS/Issuance-Fehler **kein falscher Erfolg** → Deployment schlägt kontrolliert fehl.
- `ssl.mode=existing_reverse_proxy`: Container liefert HTTP, Proxy-Vertrauensnetz konfigurierbar.
- `ssl.mode=selfsigned`/`disabled`: für Test/Bootstrap, nicht für öffentliche Produktion empfohlen.

## Idempotenz & Fehlerbehandlung

- Lock-Datei verhindert parallele Läufe.
- Bestehender CTID: nur mit `--force` im Reconfigure-Modus.
- Jeder Schritt wird mit Status/Exit-Code protokolliert (`deployment.state_dir/steps.json`) und im Abschlussreport ausgewiesen.
- `--dry-run` für Validierung/Plan ohne Änderungen.
- `--skip-tests` erzeugt höchstens `PARTIAL_SUCCESS`.

## Akzeptanztests (Pflichtprüfungen)

`healthcheck.sh` prüft u. a.: Netzwerk/DNS, PHP-FPM, NGINX, DB, Redis, Shopware-Installation/CLI, Storefront/Admin/Admin-API-Erreichbarkeit, Middleware-Connector/Secrets, Medienverzeichnis, Queue/Scheduled-Tasks, Backup/Restore-Artefakte, Storage. Ergebnis als JSON unter `monitoring.report_json`.

## Middleware-Connector (API-only)

Struktur: `/opt/omni-shopware-connector/{authentication,products,variants,categories,media,inventory,pricing,orders,customers,webhooks,health,mapping,validation,retry,logging,config}`.
Konfiguration: `config/connector.json` (maschinenlesbar, versionierbar, WebUI-vorbereitbar).

## Hinweise

- Ausführung auf Proxmox-Host im `pct`-Modus empfohlen. API-Modus ist vorbereitet, Remote-Execution per SSH muss für Vollbetrieb ergänzt werden.
- Backups sollten auf Host-Mount/NFS außerhalb des Container-Rootfs liegen (`backup.target_dir`).
- OpenSearch ist optional/separat anbinden (nicht Teil des Basis-Setups).
- Regelmäßige Restore-Tests in isolierter Umgebung aktivieren (`backup.test_restore=true`).
