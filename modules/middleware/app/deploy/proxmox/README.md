# Proxmox One-Click Installer – Shopware Server

Dieses Installationsskript installiert ausschließlich einen **Shopware-6-Server** auf Proxmox VE (LXC). Es installiert **keine Omni-Core-Middleware**.

## Installationslink (Proxmox-Host, root)

```bash
bash -c "$(curl -fsSL -H 'Cache-Control: no-cache' \"https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/install.sh?nocache=$(date +%s)\")"
```

Mit eigener Konfiguration:

```bash
CONFIG_URL="https://example.com/shopware-config.yaml" \
bash -c "$(curl -fsSL -H 'Cache-Control: no-cache' https://raw.githubusercontent.com/puchadave/omni-core-middleware/main/deploy/proxmox/install.sh)"
```

## Parameter

- `REPO_URL` / `REPO_REF` – Quelle der Deployment-Skripte (Standard: dieses Repo, `main`)
- `CONFIG_URL` oder `CONFIG_FILE` – Shopware/Proxmox-Konfiguration
- `DEPLOY_ARGS` – z. B. `--force` oder `--skip-tests`
- `TEMPLATE_ARCH` – z. B. `amd64` (automatisch erkannt)

## Hinweise

- Ausführung auf Proxmox-VE-Host als `root` erforderlich (`pct`).
- Container-Storage muss `rootdir` unterstützen (prüfen: `pvesm status -content rootdir`).
- Standardkonfiguration installiert nur Shopware (`middleware.enabled=false`). Details/erweiterte Optionen: `deploy/shopware-proxmox/README.md` und `deploy/shopware-proxmox/config.yaml`.
