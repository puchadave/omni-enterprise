# OmniSwarm – persönliches Multi-Agent-Tool (OmniRoute-spezifisch)

OmniSwarm orchestriert Aufgaben über **OmniRoute-Tiers/Combos** und domänenspezifische **MCP-Tools** (OpenCode-integrierbar, optional Headless-Dienst in `coding-swarm`).

## Rollen → Combos
| Rolle | Tier/Combo | Aufgabe |
|---|---|---|
| planner/architect | `architecture-reasoning`, `long-context-analysis` | Analyse, Plan/Task-Graph, Risiken (kein Write) |
| coder/implementer | `coding-primary` | Umsetzung, Tests |
| reviewer/auditor | `coding-review` (unabhängig) | Diff/Risiko/Security/Regression/Gate-Check |
| researcher | `long-context-analysis` | Repo/Doku/Katalog-Analyse |
| vision | `omnicore-vision` | Bild/QC/Creative |
| sales-intel | `omnicore-sales-product-intelligence` | Produktanalyse, Freigabeempfehlung, Modifikation (guardrailed) |
| operator | `t0-fast-trivial` + MCP | Tool-Ausführung, Read/Reports |

## Ablauf
Intake → Plan → (parallele) Analyse/Recherche → Umsetzung → Tests → **unabhängiges Review (T3)** → Gate/Approval (Export/Preis/Write) → Zusammenfassung. Korrekturschleife max. N Iterationen.

## Policies & Gates
- Modell/Kosten/Latenz-Budgets pro Tier; PII/Secrets-Redaktion.
- **Kein Auto-Export ohne `approved`** + `paid && !fulfillment_locked` (Export-Gate).
- Destruktive/Schreib-Aktionen nur mit Confirm & Audit; least-privilege MCP-Scopes.
- Modifikationen guardrailed & versioniert (Min-Marge/Preisband).

## Konfiguration
- `config/agents.json` – Rollenprofile/Combo-Mapping.
- `config/workflows.json` – Standard-Workflows (feature, bugfix, review, sales-approval, deploy).
- Env: `OMNIROUTE_URL`, `OMNIROUTE_API_KEY` (192.168.176.62:20128/v1), MCP-Endpoints/Keys.

## Integration
- OpenCode: Agenten/Modelle in `opencode.json`, Doktrin in `AGENTS.md`.
- Optional Laufzeit: als Job/Service in `modules/coding-swarm` oder `modules/middleware` (Headless-Batch, Scheduler).
