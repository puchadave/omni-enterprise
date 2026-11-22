# Modul: Shopware MCP (lokal)

Stellt einen **MCP-Server** für lokalen Agent-Zugriff auf Shopware 6 bereit (Tools/Resources für Katalog, Bestand, Bestellungen). Gedacht für OpenCode/OmniSwarm und andere MCP-Clients.

## Voraussetzungen
- Laufendes lokales Shopware (`modules/shopware`, Standard `http://shopware:80` im gemeinsamen Docker-Netz bzw. `http://localhost:8088`).
- API-Credentials (Access Key/Secret) aus dem Shopware-Bootstrap: `modules/shopware/integration/credentials.env` (`SHOPWARE_CLIENT_ID` / `SHOPWARE_CLIENT_SECRET`).

## Setup
```bash
cp .env.example .env
# .env setzen: SHOPWARE_URL, SHOPWARE_ACCESS_KEY_ID, SHOPWARE_SECRET_ACCESS_KEY
# optional: SHOPWARE_MCP_PACKAGE (an verfügbaren Server anpassen), SHOPWARE_MCP_READ_ONLY=true
./start.sh up
```

## OpenCode/Client-Einbindung
- Referenz: `shared/mcp/clients/shopware-mcp.json` (Platzhalter `{env:...}`).
- In Projekt-`opencode.json`/globaler Config MCP `shopware` ergänzen; Env aus Secret/`.env` laden, **keine Keys committen**.
- Default `read_only` empfohlen; Schreib-Tools nur mit Freigabe/Least-Privilege.

## Hinweise
- Paketname/Startkommando `SHOPWARE_MCP_PACKAGE`/`SHOPWARE_MCP_ARGS` muss zum tatsächlich eingesetzten Shopware-6-MCP-Server passen (ggf. Image/Build statt `npx` verwenden).
- Secrets nie committen; Credentials rotierbar über Shopware-Integration.
- Im `modules/integrated`-Stack als Service `shopware-mcp` einbindbar (gleiches Netz wie `shopware`).
