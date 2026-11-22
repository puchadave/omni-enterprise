# Überarbeiteter & erweiterter Plan + Implementierung (Omni Enterprise)

Stand: 2026-11-16 | Status: **implementiert (MVP/Skelett + produktive Config-Bindings)**

## 1) Architektur (überarbeitet)
- **OpenCode** = einziger primärer Orchestrator (`opencode.json`, `AGENTS.md`). Keine parallelen Hauptagenten.
- **OmniRoute** = zentrales LLM-Gateway `http://192.168.176.62:20128/v1` (Key via Env/Secret). Tiers/Combos statt Provider-Lock-in.
- **OmniSwarm** = persönliches Multi-Agent-Tool (Plan→T1 Umsetzung→Tests→T3 Review→Gate) auf OmniRoute, MCP-tool-fähig (`modules/omniswarm/`).
- **MCP pro Domäne** = Tool-Schicht, least-privilege, read-only default (`shared/mcp/catalog.json`).
- **omniroute-gateway-cline** = Spezial-Plugins am Gateway (Routing, Safety, Output-Guard, MCP-Bridge, Context, Cache, Telemetrie, A/B, Resilienz) – Manifeste/Registry `modules/omniroute-gateway-cline/`.

## 2) Erweiterungen (neu)
- **Tiers/Combos:** `t0-fast-trivial`, `coding-primary`, `coding-review` (unabhängig), `architecture-reasoning`, `long-context-analysis` + `omnicore-sales-product-intelligence` (explainable scorecard, `no_auto_export_without_gate`), Task-Mappings erweitert. In allen Combo-Configs gespiegelt.
- **Verkaufs-KI / Product Intelligence (MVP, implementiert):**
  - `services/product_intelligence_engine.py`: Analyse (Finanzen/Marge, Qualität/Bestand, Trend/Social/A-B-Signale, optionale LLM-Anreicherung via OmniRoute-Combo), Scorecard, Entscheidung, Modifikationsvorschläge (guardrailed).
  - `services/export_gate.py`: Gate konservativ (`review_required` default), `approved|rejected|hold|review_required`, Expiry, Quellen.
  - **DB-Attribute/Tabellen (neu):** `product_analysis_runs`, `product_analysis_results`, `product_export_approvals` (unique sku+channel), `product_modifications`, `product_market_signals`, `product_ab_tests` + CRUD/Helper in `database.py`.
  - **APIs:** `/api/product-intelligence/analyze|results|approvals|approve|gate/{sku}|signals`.
  - **Export-Gate erzwungen:** `shopware_export_manager.py` (Block → `release_required`) und `google_export_engine.py` (Queue/Auto-Export nur bei approved, sonst Validation/blocked) – **kein Auto-Export ohne Approval** (zusätzlich Payment-Guard `paid && !fulfillment_locked` bleibt maßgeblich).
- **Vinted/Vintage:** bestehender `pucha_vinted` capability-gated Connector beibehalten/integriert; Sync/Listing/Order/Payment laufen durch Intelligence+Gate (capability/Vertrag weiterhin Voraussetzung).
- **Shopware-MCP lokal:** Modul `modules/shopware-mcp/` (Compose, Env, Start, README) + Client-Referenz `shared/mcp/clients/shopware-mcp.json`; in `modules/integrated` als Service/Config verdrahtet (read-only default, Creds aus Bootstrap-Integration). Projekt-`opencode.json` enthält MCP-Eintrag (disabled-by-default, aktivierbar via Env).
- **MCP-Katalog (alle Aufgaben):** `shared/mcp/catalog.json` (Shopware, Marketplace/Vinted, Product-Intelligence, Middleware/Core, OmniRoute-Admin, Social, Marketing, Content, DevOps, Infra, Knowledge, Governance) mit Scopes/Gates.
- **OmniSwarm:** `modules/omniswarm/` (Rollen/Combo-Mapping, Workflows, Policies, README).
- **Gateway-Plugins:** `modules/omniroute-gateway-cline/plugins/*` (Manifeste + Registry) für Policy-Routing, Safety, Structured-Output, MCP-Bridge, Context, Cache, Telemetrie, A/B-Eval, Resilienz.
- **Agent-Governance:** `AGENTS.md`, aktualisiertes Projekt-`opencode.json` (Provider/Tiers/Agenten/MCP via `{env:...}`).

## 3) Konfiguration & Binding (implementiert)
- **OmniRoute:** `http://192.168.176.62:20128/v1`, Key `change_me_omniroute_api_key` in `modules/integrated/.env(.example)` gesetzt; `docker-compose` der integrierten Services auf externen Endpoint/Env parametriert (lokaler omniroute-dep entfernt), neue Attribute (`OMNIROUTE_DEFAULT_COMBO`, `OMNIROUTE_SALES_COMBO`, `PRODUCT_INTELLIGENCE_*`, `EXPORT_GATE_MODE`, `SHOPWARE_MCP_*`).
- **Middleware/Root:** Env-Beispiele erweitert (Combos, Gate, PI, MCP, Binding). `modules/middleware` Compose/Env um PI/MCP/Gate-Variablen ergänzt.
- **Secrets-Hinweis:** bereitgestellte Keys in Env-Dateien für lokalen Betrieb hinterlegt – **nicht committen/rotieren** für Produktion, in Secret-Store überführen.

## 4) Software-Attribute (neu, konsolidiert)
- `OMNIROUTE_URL/BASE_URL/API_KEY`, `OMNIROUTE_DEFAULT_COMBO=coding-primary`, `OMNIROUTE_SALES_COMBO=omnicore-sales-product-intelligence`
- `PRODUCT_INTELLIGENCE_ENABLED`, `PRODUCT_INTELLIGENCE_AUTO_APPROVE` (default false), `PRODUCT_INTELLIGENCE_USE_LLM`
- `PI_MIN_MARGIN_PCT`, `PI_MIN_APPROVE_SCORE`, `PI_MIN_STOCK`
- `EXPORT_GATE_MODE` (`review_required`|`open`|`strict`)
- `SHOPWARE_MCP_ENABLED/URL/READ_ONLY` + `SHOPWARE_URL/ACCESS_KEY_ID/SECRET_ACCESS_KEY`
- Gate-Entscheidungen/Audit in `product_export_approvals`, Analyse-Artefakte in `product_analysis_*`, Signale in `product_market_signals`.

## 5) Rollout / Nutzung
1. Env prüfen (`modules/integrated/.env`), Ziel-OmniRoute erreichbar (`/v1/models`), Combos auf Gateway importieren falls nötig.
2. `modules/integrated`: `./start.sh up` → Middleware `/healthz`, Analyse via `POST /api/product-intelligence/analyze`, Freigabe `POST /approve`, Gate `GET /gate/{sku}`.
3. Shopware: Bootstrap-Creds in `modules/shopware-mcp/.env` übernehmen, `./start.sh up` (MCP), OpenCode-MCP aktivieren.
4. Export: nur bei `approved` (Gate) und Payment `paid` (Guard) – konservativer Default.

## 6) Offene Punkte / nächste Ausbaustufe
- Live-Import der erweiterten Combos in die Ziel-OmniRoute-Instanz verifizieren (Mgmt-API je Version).
- Shopware-MCP-Paketname/Transport final an verfügbaren Server anpassen; Schreib-Scopes nach Freigabe.
- Vinted produktiv: bestätigter Zugang/Vertrag/Webhook/Testkonto → Client/Sync implementieren (derzeit capability-gated).
- UI für Scorecards/Freigaben, A/B-Test-Import, automatische Safe-Apply-Policies nach Datenlage, vollständige Gateway-Plugin-Implementierung gemäß Ziel-Plugin-API.
- Secrets rotieren & in Secret-Store/ZeroTier-gesicherte Verteilung überführen; keine Live-Prod-Veränderung ohne Freigabe.
