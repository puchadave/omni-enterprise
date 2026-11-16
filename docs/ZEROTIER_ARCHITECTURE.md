# ZeroTier – Architektur & Integrationsplan (sicher, fehlertolerant)

## Ziel
Sichere, verschlüsselte, reibungslose Kommunikation zwischen Modulen, auch wenn sie **standalone auf unterschiedlichen Geräten** laufen, sowie Reduktion von Totalausfällen durch klare Failure-Domains, Resilienz und kontrolliertes Routing.

## Netzmodell (empfohlen, parametrisierbar)
- **Overlay:** ein autorisiertes ZeroTier-Netz `zt-omni` (Netz-ID via `ZT_NETWORK_ID`), Gerätezugang nur nach Freigabe (ZeroTier Central/self-hosted Controller).
- **Adressierung:** pro Host eine stabile ZT-Managed-IP (Dokumentation in `shared/networks-and-addressing.md` / Inventar). Module auf unterschiedlichen Hosts kommunizieren über `http(s)://<ZT_IP>:<PORT>` bzw. FQDN (falls DNS gepflegt).
- **Segmentierung (Blast-Radius):** Flow-Rules deny-by-default; nur benötigte Ports/Rollen zulassen (z.B. middleware→omniroute 20128, edge→middleware 8002, Admin→portainer 9443 nur aus Admin-Gruppe). DB idealerweise **lokal/kolokiert** mit Middleware (kein Remote-DB über unsichere Pfade); falls remote nötig: nur ZT + TLS + starke Auth.
- **Modi:**
  - *Standalone multi-device:* Host-ZeroTier auf jedem Gerät, Services binden an Host/konfigurierbares Interface, Peers via ZT-IPs in `.env` (`OMNIROUTE_URL`, `MIDDLEWARE_URL` etc.).
  - *Single-host:* Docker-Bridge/DNS (`omniroute`, `db`) primär; ZT optional für externen/Admin-Zugriff.
  - *Swarm:* Overlay-Netze für Intra-Cluster; ZT für Management-/Cross-Site-Zugriff (getrennt betrachten, kein ungeprüftes Overlay-Stretching).

## Integration im Repository (implementiert als optional, sicher-by-default)
- **Host-seitig (empfohlen):** Installation von `zerotier-one` pro Gerät, Join & Autorisierung, danach Start der Module mit Peer-URLs auf ZT-IPs. Skript: `scripts/zerotier-host-setup.sh` (idempotent, keine Auto-Autorisierung).
- **Optionaler Container (Profil `zt`):** In Modulen/Stacks kann ein `zerotier`-Service (Image konfigurierbar) als **opt-in** Profil ergänzt werden, um Join zu automatisieren (`ZT_NETWORK_ID` als Secret/Env). Hinweis: erfordert erhöhte Capabilities/Devices je Plattform – nur wenn Host-Install nicht möglich; Dokumentation der Risiken.
- **Konfiguration:** `.env.example` enthält `ZT_ENABLED`, `ZT_NETWORK_ID` (leer/Platzhalter), `ZT_NODE_NAME`, `SERVICE_BIND_HOST` (Default restriktiv wo sinnvoll), und Peer-URLs.

## Sicherheit
- ZeroTier-Verschlüsselung + strikte Flow-Rules + Gerätefreigabe. **Kein** Verlass nur auf Netzwerk: App-Auth (API-Keys), TLS am Edge/Reverse-Proxy, keine offenen Dashboards ohne Auth.
- Secrets nie im Repo; Keys rotierbar; Ports minimal exponieren (bevorzugt Bind an `127.0.0.1` oder ZT-Interface, nicht pauschal `0.0.0.0` auf untrusted Hosts).

## Fehlertoleranz & Resilienz
- **Timeouts/Retries/Backoff** in Clients (z.B. OmniRoute-Calls), `restart: unless-stopped`/Swarm-Restart, Healthchecks + `depends_on: service_healthy`, start-first Updates wo möglich.
- **Degradation:** Middleware bleibt für Nicht-AI-Pfade nutzbar, wenn OmniRoute/ZT-Pfad ausfällt (klare 503/Fehlercodes); Worker mit Retry-Queues/Idempotenz (fachlich).
- **Failure-Domains:** stateful Dienste lokal/pinned, Backups & Restore-Runbooks; ZT-Ausfall behandelt als Netzpartition (Monitoring des ZT-Dienstes, Fallback nur wenn definiert).
- **Betrieb:** Smoke-Tests (`scripts/smoke-test.sh`), Secret-Checks (`scripts/check-secrets.sh`), regelmäßige Backups (`scripts/backup-volumes.sh`/DB-Dumps).

## Offene Entscheidungen (Blocker für produktive ZT-Freischaltung)
- Finale `ZT_NETWORK_ID`, genehmigte Mitglieder/Controller, finale Flow-Rule-Matrix, und ob DB remote erlaubt ist (Default: nein/kolokiert).
