# Portainer / Docker Swarm (pve01–pve04)

## Node-Verteilung
| Node | IP | Label `omni.role` | Dienste |
|---|---|---|---|
| pve01 | 192.168.178.250 | `edge` | Traefik/Edge (80/443, host) |
| pve02 | 192.168.178.251 | `ai` | OmniRoute, Content/Coding (on-demand) |
| pve03 | 192.168.178.252 | `growth` | Marketing, Social |
| pve04 | 192.168.178.254 | `core` + manager | DB, Middleware, Shopware, Portainer |

## Setup
1. **Swarm init & join**
```bash
# pve04
docker swarm init --advertise-addr 192.168.178.254
# pve01-pve03: docker swarm join --token <worker> 192.168.178.254:2377
```
2. **Labels setzen** (Node-Namen ggf. an `docker node ls` anpassen):
```bash
./scripts/prepare-swarm-nodes.sh print
./scripts/prepare-swarm-nodes.sh label
```
3. **Portainer deployen**
```bash
docker stack deploy -c stacks/swarm/portainer.stack.yml portainer
# UI: https://<pve04>:9443 (Admin anlegen)
```
4. **Middleware-Image bereitstellen** (Swarm baut nicht)
```bash
./scripts/build-and-distribute.sh build --tag omnicore-middleware:local
./scripts/build-and-distribute.sh save --out /tmp/omnicore.tar
./scripts/build-and-distribute.sh load-remote --hosts root@192.168.178.250,root@192.168.178.251,root@192.168.178.252,root@192.168.178.254 --image /tmp/omnicore.tar
```
5. **Enterprise-Stack via Portainer**
- Stacks → Add stack `omni` → **Git-Repository** (empfohlen, wegen `configs:` file-refs): Compose-Pfad `stacks/swarm/omni-enterprise.stack.yml`.
- **Web-Editor:** `stacks/swarm/omni-enterprise.portainer-web.yml` nutzen und Swarm-Configs vorher anlegen:
  ```bash
  docker config create omniroute_config stacks/swarm/configs/omniroute/config.json
  docker config create omniroute_combos stacks/swarm/configs/omniroute/combos.optimized.json
  ```
- Environment: Werte aus `stacks/swarm/.env.example` (Pflicht: `POSTGRES_PASSWORD`, `OMNIROUTE_API_KEY`, `OMNICORE_IMAGE`).

## Betrieb & Hinweise
- Prüfen: `docker stack services omni` / `docker service ls`.
- Stateful (DB/Middleware/Shopware) pinned auf `core`/pve04 – Volumes node-lokal, Backups einplanen.
- Platzhalter-Services mit `*_REPLICAS=0` skalierbar; Overlay-Netze `omni/edge/agent`.
- Bestehende Live-Instanzen (z.B. `:8002`, DB `omnicore`) nicht überschreiben – Ports/Namen/Volumes trennen.
