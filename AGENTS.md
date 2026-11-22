# AGENTS – Omni Enterprise (OpenCode + OmniRoute + OmniSwarm)

## Zielarchitektur
- **OpenCode** = primärer Orchestrator (Plan/Edit/Test/Git). Keine parallelen konkurrierenden Hauptagenten.
- **OmniRoute** = zentrales LLM-Gateway (`OMNIROUTE_URL`, `OMNIROUTE_API_KEY` aus Env/Secret). Modellwahl über Combos/Tiers, nicht hardcoded pro Provider.
- **MCP** = domänenspezifische Tool-Server (pro Aufgabe/Domäne), least-privilege, Secrets via `{env:...}`.
- **OmniSwarm** = persönliches Multi-Agent-Tool (Plan→Umsetzung→unabhängiges Review→Gate) auf OmniRoute-Tiers.

## Modell-Tiers / Combos (OmniRoute)
- **T0 `t0-fast-trivial`**: Suche, kleine Edits, Format/Docs.
- **T1 `coding-primary`**: Implementierung, Bugfixes, Tests, API-Integration.
- **T3 `coding-review`**: unabhängiges Review (Diff/Risiko/Security/Regression), anderes Profil.
- **T2 `architecture-reasoning`**: Architektur, Security, komplexe Refactors, schwieriges Debugging.
- **Long-Context `long-context-analysis`**: große Repos/Kataloge/Dokumente.
- **Vision `omnicore-vision` / Photoshoot**: Bild/QC/Creative.
- **Sales `omnicore-sales-product-intelligence`**: Produktanalyse (Trends/Mode, Social, Finanzen/Marge, A/B), Freigabeempfehlung, Modifikationen (guardrailed), `no_auto_export_without_gate`.

## Arbeitsdoktrin
1. Kontext/Repo analysieren → 2. Plan & passendes Tier/Combo wählen → 3. Implementieren (T1) → 4. Tests/Checks → 5. **unabhängiges Review (T3)** → 6. Security/Secrets/Gate-Compliance → 7. Diff-Inspection → 8. Finale Validierung.

## Sicherheits- & Geschäftsregeln
- **Keine Secrets/Keys** ins Repo oder in Logs; nur Env/Secret-Store.
- **Export-Gate verbindlich:** kein Auto-Export ohne `approved` (Product-Intelligence/Gate). Default konservativ `review_required`.
- **Payment-Guard:** Versand/Weitergabe nur bei `paid && !fulfillment_locked`.
- **Destruktive Aktionen** (Deploy/Delete/Force/Prod-Daten) nur mit expliziter Bestätigung; keine Änderungen an Live-Prod-Containern/Volumes ohne Freigabe.
- **Schreibende MCP-Tools** nur mit Confirm/Least-Privilege; read-only als Default.
- **Preis/Modifikation:** guardrailed (Min-Marge/Preisband), versioniert/nachvollziehbar, keine stillen Überschreibungen.

## MCP (Domänen, je eigener Server)
Siehe `shared/mcp/catalog.json`. Kern: `shopware-mcp` (lokal), `omni-middleware-mcp`, `product-intelligence-mcp`, `marketplace-vinted-mcp`, `omniroute-admin-mcp`, `social/marketing/content-mcp`, `devops-swarm-mcp`, `infra-mcp`, `knowledge-mcp`, `governance-mcp`.

## Konfiguration
- `OMNIROUTE_URL=http://192.168.176.62:20128/v1` (Beispiel/gesetzt), `OMNIROUTE_API_KEY` aus Env.
- `SHOPWARE_URL`/Keys aus `modules/shopware/integration/credentials.env` (Bootstrap).
- Projekt-`opencode.json` nutzt `{env:...}` Platzhalter; globale Overrides optional in `~/.config/opencode/`.
