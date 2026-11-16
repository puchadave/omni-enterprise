# 09 – Offene Punkte / Blocker

| Thema | Status | Bedarf/Entscheidung |
|---|---|---|
| ZeroTier produktiv | offen | `ZT_NETWORK_ID`, genehmigte Mitglieder, ZT-IP-Plan, Controller (Central/self-hosted), finale Flow-Rule-Matrix (deny-by-default) |
| Shopware Go-Live | blockiert | Shop-URL, Client-ID/Secret, Sales-Channel, Ziel-Konfiguration; `shopware_connections`/Anbindung in Ziel-DB herstellen |
| Vinted-Anbindung | blockiert | Bestätigter Zugang (Pro/Partner), Auth/Endpoints/Webhooks/Testkonto; Umsetzung capability-gesteuert, `config_required` bis Freigabe |
| Produktive Secrets | offen | Starke, rotierte Werte für DB/OmniRoute/App; sichere Verteilung (kein Git), optional Docker/Swarm Secrets |
| Swarm-Rollout pve01–pve04 | offen | Manager/Join/Labels/Overlay, Image-Registry/Distribution, Storage/Backup auf pve04, Freigabe-Fenster |
| TLS/Edge Prod | offen | Zertifikate/ACME, Dashboard-Auth, HSTS/Redirects, nur nötige Ports |
| DB-Remote-Policy | Entscheidung | Default kolokiert; Remote nur mit ZT+TLS+starker Auth (nicht empfohlen) |
| Prod Shopware-Stack | offen | Ersatz dockware/dev durch Prod-Setup (MariaDB/Redis/OpenSearch, gehärtet) |
| Live-Schutz | dauernd | `310b4d4eb7c1`/`1c51e239de30` und wawi/portainer-prod unverändert lassen |

> Annahmen sind in Docs markiert; ohne Freigabe/Credentials keine produktiven Verbindungen erzwingen.
