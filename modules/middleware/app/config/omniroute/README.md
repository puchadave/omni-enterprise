# OMNI-CORE OmniRoute Auto-Kombos (Middleware-Server)

Enthält empfohlene Auto-Combo-/Routing-Konfigurationen für OMNI-CORE (`omnicore-auto-combos.json`).

## Auf dem Middleware-Server geladen/angewendet
- **Konfigurationsdateien:** `config/omniroute/omnicore-auto-combos.json`, `config/omniroute/task_model_mapping.json`
- **Middleware-Einstellungen (DB `settings`):** `omniroute_agent_model`, `default_llm_model`, `default_image_model`, `ai_model_photoshoot`, `ai_model_*` (pro Aufgabe) und `omniroute_combo_config` → zeigen auf auto/omnicore-Combos.

Die Middleware nutzt diese Werte für Copilot/Content/Trend/Fotoshooting (sofern verdrahtet). `auto/*` wird vom OmniRoute-Gateway ausgewertet.

## In OmniRoute (Gateway) laden
Die OmniRoute-**Verwaltungs-API** (`/api/combos`, `/api/config`, …) benötigt ein **Management-Token** (nicht den v1 API-Key). Ohne Token kann der Middleware-Server die Combos nicht automatisch im Gateway anlegen.

Mit gültigem Management-Token (Beispiel, je nach OmniRoute-Version/Schema anpassen):
```bash
export OMNIROUTE_MGMT="https://puchalla.online"
export OMNIROUTE_MGMT_TOKEN="<MANAGEMENT_TOKEN>"
# Beispiel je Combo (Payload ggf. an euer OmniRoute-Schema anpassen)
curl -sS -X POST "$OMNIROUTE_MGMT/api/combos" \
  -H "Authorization: Bearer $OMNIROUTE_MGMT_TOKEN" \
  -H "Content-Type: application/json" \
  -d @<(jq -c '.combos[0]' config/omniroute/omnicore-auto-combos.json)
```
Alternativ Combos/`auto/*`-Defaults im OmniRoute-Dashboard pflegen und die Namen hier in den Settings referenzieren.

## Wichtige Auto-Kombos (Kurz)
- `auto/smart` (selbstlernend/qualitativ), `auto` (balanced), `auto/fast`, `auto/cheap`, `auto/coding`, `auto/vision`, `auto/offline`, `auto/subscription`, `auto/thrifty`, `auto/lkgp`, `auto/chaos`
- Eigene: `omnicore-copilot`, `omnicore-content-bulk`, `omnicore-content-quality`, `omnicore-fast-classify`, `omnicore-coding`, `omnicore-photoshoot`, `omnicore-photoshoot-identity` (Luna/Raven/Nyx), `omnicore-critical-fusion`, `omnicore-subscription-thrifty`, `omnicore-cache-optimized`, `omnicore-trend-analysis`, u. a.

Keine Zugangsdaten/Secrets in Dateien; Auth erfolgt über Umgebungsvariablen/Settings.
