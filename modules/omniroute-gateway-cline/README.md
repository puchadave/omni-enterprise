# omniroute-gateway-cline – Spezial-Plugins & Integration (OmniRoute Gateway)

Spezialisierte Plugins/Erweiterungen für das zentrale OmniRoute-Gateway (192.168.176.62:20128/v1) und dessen Cline/OpenCode-Integration. Ziel: Routing/Policy/Safety/Tooling/Observability fachlich erweitern.

## Plugin-Übersicht
| Plugin | Zweck | Hook/Vertrag |
|---|---|---|
| `combo-policy-router` | Task→Combo/Tier (T0–T3, sales/vision/longctx), Budget/Kosten/Latenz, Quota-aware Fallback | pre-route / request classifier |
| `structured-output-guard` | JSON-Schema-Validierung & Repair-Retry (Scorecards/Entscheidungen) | post-process / validator |
| `safety-gate` | PII/Secret-Redaktion, Block riskanter Aktionen, Export nur mit Approval-Hinweis | pre/post filter, policy hook |
| `mcp-tool-bridge` | Registrierung/Proxy ausgewählter MCP-Tools (read-only default) | tool registry / proxy |
| `omni-context-injector` | Projekt/Tenant/Policy/Combo-Kontext je Request injizieren | pre-prompt |
| `cache-semantic` | Dedupe/Cache wiederholter Prompts/Analysen | cache layer |
| `observability-telemetry` | Traces, Token/Kosten, Latenz, Audit-Events, Fehlerklassen | telemetry/audit sink |
| `ab-routing-eval` | A/B von Modellen/Combos, Qualitäts-Evals | eval/routing feedback |
| `resilience-failover` | Health/Cooldown/Circuit-Breaker, Provider-Failover | resilience middleware |

> Hinweis: Konkrete Einhängepunkte hängen von der Plugin-API der Zielinstanz (OmniRoute/Cline) ab. Manifeste unten sind implementierungsleitend und konfigurationsgetrieben.

## Konfiguration
- Gateway: `OMNIROUTE_URL=http://192.168.176.62:20128/v1`, `OMNIROUTE_API_KEY` aus Secret.
- Combos/Mapping: `stacks/swarm/configs/omniroute/combos.optimized.json` (+ Spiegel).
- Registrierung: `plugins/registry.json` referenziert die Plugin-Manifeste.

## Sicherheit
- Kein Klartext-Secret im Repo; least-privilege; write/Export-Tools nur mit Gate/Confirm.
- Compliance: PII-Redaktion, Audit, `no_auto_export_without_gate`.
