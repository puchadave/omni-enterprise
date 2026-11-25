from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from database import fetch_product, list_products, marketplace_dataset_list, sales_state_upsert, upsert_product_aggregate
from .ai_task_routing import canonical_json

logger = logging.getLogger(__name__)

CHANNEL_SCOPE = "shopware"
BUNDLE_PREFIX = "BND-"
MIN_MARGIN_PCT = float(os.getenv("SHOPWARE_CHAT_MIN_MARGIN_PCT", "12"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(str(v).replace(",", "."))
    except Exception:
        return default


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(float(v))
    except Exception:
        return default


async def _price_history_30d_ok(sku: str, reference_price: float) -> bool:
    # Best-effort: use sales_state/release_state metadata if present; otherwise conservative false.
    # A real price-history table can be plugged in; we never fabricate compliance.
    try:
        from database import sales_state_get
        state = await sales_state_get(sku) or {}
        raw = state.get("price_history_json") or state.get("shopware_readback_json") or {}
        if isinstance(raw, str):
            try: raw = json.loads(raw)
            except Exception: raw = {}
        hist = raw.get("price_history_30d") if isinstance(raw, dict) else None
        if isinstance(hist, list) and hist:
            prices = [_to_float(x.get("price") if isinstance(x, dict) else x, 0.0) for x in hist]
            prices = [p for p in prices if p > 0]
            if prices and reference_price > 0:
                min_p = min(prices)
                return min_p <= reference_price * 1.001
    except Exception:
        pass
    return False


def _extract_skus(message: str) -> list[str]:
    return [s.strip().upper() for s in re.findall(r"\b[A-Z0-9][A-Z0-9._-]{2,63}\b", message or "") if not s.isdigit()]


def _intent(message: str) -> str:
    m = (message or "").lower()
    if "bundle" in m or "set" in m:
        return "bundle"
    if "best" in m or "angebot" in m or "aktion" in m or "sale" in m:
        return "best_sales"
    if "import" in m:
        return "import"
    return "advice"


async def _shopware_projection(sku: str) -> dict[str, Any] | None:
    # Canonical product + optional shopware dataset projection; never returns other channels.
    p = await fetch_product(sku)
    if not p:
        return None
    ds = await marketplace_dataset_list(sku=sku, channel=CHANNEL_SCOPE, limit=1)
    shop_ds = ds[0] if ds else None
    out = dict(p)
    out["channel_scope"] = CHANNEL_SCOPE
    out["shopware_dataset"] = shop_ds
    out["shopware_ready"] = bool(shop_ds and str(shop_ds.get("status")) == "ready")
    return out


async def plan_shopware_chat(message: str, *, actor: str = "", skus: list[str] | None = None, confirm: bool = False) -> dict[str, Any]:
    intent = _intent(message)
    target_skus = [s.upper() for s in (skus or _extract_skus(message)) if s]
    products = []
    for sku in target_skus[:50]:
        p = await _shopware_projection(sku)
        if p:
            products.append(p)
    if not products and intent in {"best_sales", "import"}:
        # deterministic fallback: top active products by stock/margin potential
        candidates = await list_products(limit=200)
        for p in candidates:
            sku = str(p.get("sku") or "").strip().upper()
            if not sku: continue
            proj = await _shopware_projection(sku)
            if proj: products.append(proj)
            if len(products) >= 10: break

    diff: dict[str, Any] = {"channel_scope": CHANNEL_SCOPE, "items": [], "other_marketplaces_touched": 0}
    guardrails: dict[str, Any] = {"channel_isolated": True, "paangv_validated": True, "min_margin_validated": True, "bundle_validated": True, "warnings": []}
    plan_id = str(uuid.uuid4())
    idempotency_key = _hash(CHANNEL_SCOPE, intent, actor, message, ",".join(target_skus))

    if intent == "bundle":
        bundle_sku = next((s for s in target_skus if s.startswith(BUNDLE_PREFIX)), f"{BUNDLE_PREFIX}{_hash(message)[:8].upper()}")
        components = [{"sku": str(p.get("sku")), "quantity": 1} for p in products[:5] if str(p.get("sku")) != bundle_sku]
        stocks = []
        tax_rates = set()
        missing = []
        for c in components:
            cp = await fetch_product(c["sku"])
            if not cp:
                missing.append(c["sku"]); continue
            q = max(1, _to_int(c["quantity"], 1))
            stocks.append(int(_to_float(cp.get("stock_quantity") or cp.get("stock") or 0, 0) // q))
            tax_rates.add(_to_float(cp.get("tax_rate") or cp.get("tax") or 19.0, 19.0))
        bundle_stock = min(stocks) if stocks else 0
        circular = bundle_sku in {c["sku"] for c in components}
        if not components:
            guardrails["warnings"].append("bundle_missing_components")
        if missing:
            guardrails["warnings"].append(f"missing_components:{','.join(missing)}")
        if circular:
            guardrails["warnings"].append("circular_bundle_blocked")
        tax_warning = None
        highest_tax = max(tax_rates) if tax_rates else 19.0
        if len(tax_rates) > 1:
            tax_warning = "mixed_tax_rates"
            guardrails["warnings"].append("mixed_tax_rates_highest_applied_or_blocked")
        if not components or missing or circular:
            guardrails["bundle_validated"] = False
        diff["bundle"] = {
            "sku": bundle_sku,
            "type": "bundle",
            "components": components,
            "calculated_stock": bundle_stock,
            "tax_rates": sorted(tax_rates),
            "highest_tax_rate": highest_tax,
            "tax_handling": "highest_rate_warning" if tax_warning else "ok",
            "shopware_custom_fields": {"omnicore_bundle_components": components, "omnicore_bundle": True, "omnicore_bundle_tax_rate": highest_tax},
            "cross_selling": [c["sku"] for c in components],
        }
        diff["items"] = [diff["bundle"]]
        summary = f"Bundle-Plan {bundle_sku} für Shopware (dry-run), {len(components)} Komponenten, Bestand {bundle_stock}."

    elif intent == "best_sales":
        items = []
        for p in products:
            sku = str(p.get("sku"))
            buy = _to_float(p.get("purchase_price") or p.get("supplier_price"), 0.0)
            sell = _to_float(p.get("sale_price") or p.get("retail_price") or p.get("price"), 0.0)
            stock = _to_int(p.get("stock_quantity") or p.get("stock"), 0)
            margin_pct = ((sell - buy) / sell * 100) if sell > 0 else 0.0
            ref = _to_float(p.get("reference_price_30d") or p.get("list_price") or sell, sell)
            paangv_ok = await _price_history_30d_ok(sku, ref)
            margin_ok = sell > 0 and (buy <= 0 or margin_pct >= MIN_MARGIN_PCT)
            action_price = round(sell * 0.95, 2) if sell > 0 else 0.0
            if buy > 0 and action_price < buy:
                margin_ok = False
            item = {
                "sku": sku,
                "type": "best_sales",
                "current_price": sell,
                "action_price": action_price,
                "reference_price_30d": ref if paangv_ok else None,
                "listPrice": {"price": ref} if paangv_ok and ref > action_price else None,
                "margin_pct": round(margin_pct, 2),
                "stock": stock,
                "paangv_ok": paangv_ok,
                "margin_ok": margin_ok,
                "blocked": (not margin_ok) or stock <= 0,
                "notes": [] if paangv_ok else ["PAngV: kein validierter 30-Tage-Referenzpreis – kein Streichpreis."],
            }
            if not margin_ok:
                item["notes"].append("Mindestmarge/Einstandspreis verletzt.")
                guardrails["min_margin_validated"] = False
            if stock <= 0:
                item["notes"].append("Kein Bestand.")
            if not paangv_ok:
                guardrails["paangv_validated"] = False
            items.append(item)
        diff["items"] = items
        summary = f"Best-Sales Plan für {len(items)} Shopware-SKUs (dry-run)."
    elif intent == "import":
        diff["items"] = [{"sku": str(p.get("sku")), "type": "import", "status": "planned"} for p in products]
        summary = f"Shopware-Import-Plan für {len(diff['items'])} SKUs (dry-run)."
    else:
        diff["items"] = [{"sku": str(p.get("sku")), "type": "advice", "status": "readonly"} for p in products]
        summary = "Shopware-Chat Analyse (read-only)."

    diff["other_marketplaces_touched"] = 0
    plan = {
        "plan_id": plan_id,
        "channel_scope": CHANNEL_SCOPE,
        "intent": intent,
        "summary": summary,
        "skus": [str(i.get("sku")) for i in diff.get("items", []) if i.get("sku")],
        "diff": diff,
        "guardrails": guardrails,
        "idempotency_key": idempotency_key,
        "status": "draft",
        "created_by": actor,
        "prompt": message,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "requires_confirmation": intent in {"bundle", "best_sales", "import"},
    }
    await save_shopware_chat_plan(plan)
    return plan


async def save_shopware_chat_plan(plan: dict[str, Any]) -> None:
    from database import release_state_set
    await release_state_set(f"shopware_chat_plan:{plan['plan_id']}", plan)
    # idempotency index
    await release_state_set(f"shopware_chat_idem:{plan['idempotency_key']}", {"plan_id": plan["plan_id"], "status": plan.get("status"), "updated_at": plan.get("updated_at")})


async def get_shopware_chat_plan(plan_id: str) -> dict[str, Any] | None:
    from database import release_state_get
    row = await release_state_get(f"shopware_chat_plan:{plan_id}")
    if not row: return None
    v = row.get("value_json")
    if isinstance(v, str):
        try: v = json.loads(v)
        except Exception: v = None
    return v if isinstance(v, dict) else None


async def execute_shopware_chat_plan(plan_id: str, *, actor: str = "", idempotency_key: str = "") -> dict[str, Any]:
    plan = await get_shopware_chat_plan(plan_id)
    if not plan:
        raise ValueError("plan_not_found")
    if plan.get("channel_scope") != CHANNEL_SCOPE:
        raise ValueError("invalid_channel_scope")
    if idempotency_key and idempotency_key != plan.get("idempotency_key"):
        raise ValueError("idempotency_key_mismatch")
    if plan.get("status") == "completed":
        return plan
    if (plan.get("diff") or {}).get("other_marketplaces_touched", 0) != 0:
        raise ValueError("channel_isolation_violation")

    # Optimistic lock: only draft/failed can execute
    if plan.get("status") not in {"draft", "failed"}:
        raise ValueError(f"invalid_plan_status:{plan.get('status')}")

    plan["status"] = "executing"
    plan["updated_at"] = utc_now()
    await save_shopware_chat_plan(plan)

    results = []
    try:
        intent = plan.get("intent")
        for item in (plan.get("diff") or {}).get("items", []):
            sku = str(item.get("sku") or "").upper()
            if not sku:
                continue
            if intent == "bundle":
                components = item.get("components") or []
                # deterministic stock
                stocks = []
                for c in components:
                    cp = await fetch_product(str(c.get("sku")))
                    if not cp:
                        raise ValueError(f"bundle_component_missing:{c.get('sku')}")
                    q = max(1, _to_int(c.get("quantity"), 1))
                    stocks.append(int(_to_float(cp.get("stock_quantity") or cp.get("stock") or 0, 0) // q))
                calc_stock = min(stocks) if stocks else 0
                tax_rates = []
                for c in components:
                    cp = await fetch_product(str(c.get("sku")))
                    if cp:
                        tax_rates.append(_to_float(cp.get("tax_rate") or cp.get("tax") or 19.0, 19.0))
                if len(set(tax_rates)) > 1 and os.getenv("SHOPWARE_BUNDLE_BLOCK_MIXED_TAX", "0") == "1":
                    raise ValueError("mixed_tax_blocked")
                highest_tax = 19.0
                if tax_rates:
                    highest_tax = max(tax_rates)
                payload = {
                    "sku": sku,
                    "title": item.get("title") or f"Bundle {sku}",
                    "is_bundle": True,
                    "stock_quantity": calc_stock,
                    "tax_rate": highest_tax,
                    "bundle_components": components,
                    "custom_fields": {"omnicore_bundle_components": components, "omnicore_bundle": True, "omnicore_bundle_tax_rate": highest_tax},
                    "cross_selling_skus": [c.get("sku") for c in components],
                    "source_system": "shopware_chat",
                }
                await upsert_product_aggregate(payload, sync_rules={"mode": "full", "preserve_manual_seo": True})
                results.append({"sku": sku, "status": "bundle_upserted", "stock": calc_stock})
            elif intent == "best_sales":
                if item.get("blocked"):
                    results.append({"sku": sku, "status": "blocked", "notes": item.get("notes", [])})
                    continue
                await sales_state_upsert(sku, lifecycle_status="READY_FOR_SALE", selection_reason="shopware_best_sales", selection_score=100.0)
                results.append({"sku": sku, "status": "prepared_for_shopware_offer", "listPrice": item.get("listPrice"), "action_price": item.get("action_price")})
            elif intent == "import":
                results.append({"sku": sku, "status": "import_delegated"})
            else:
                results.append({"sku": sku, "status": "noop"})

        plan["status"] = "completed"
        plan["result"] = {"items": results, "other_marketplaces_touched": 0}
        plan["updated_at"] = utc_now()
        plan["executed_by"] = actor
        await save_shopware_chat_plan(plan)
        try:
            from database import add_audit_log
            await add_audit_log(action="shopware_chat.execute", entity_type="shopware_chat_plan", entity_id=plan_id, user_id=actor, old_value={"prompt": plan.get("prompt"), "diff": plan.get("diff")}, new_value=plan.get("result"))
        except Exception:
            pass
        return plan
    except Exception as exc:
        plan["status"] = "failed"
        plan["error"] = str(exc)[:1000]
        plan["updated_at"] = utc_now()
        await save_shopware_chat_plan(plan)
        raise


async def rollback_shopware_chat_plan(plan_id: str, *, actor: str = "") -> dict[str, Any]:
    plan = await get_shopware_chat_plan(plan_id)
    if not plan:
        raise ValueError("plan_not_found")
    # Rollback: set affected bundle/products inactive in canonical projection metadata (non-destructive)
    for item in (plan.get("diff") or {}).get("items", []):
        sku = str(item.get("sku") or "")
        if not sku: continue
        try:
            await upsert_product_aggregate({"sku": sku, "status": "inactive", "is_active": False, "source_system": "shopware_chat_rollback"}, sync_rules={"mode": "full", "preserve_manual_seo": True})
        except Exception:
            logger.exception("rollback_failed sku=%s", sku)
    plan["status"] = "rolled_back"
    plan["updated_at"] = utc_now()
    plan["rolled_back_by"] = actor
    await save_shopware_chat_plan(plan)
    try:
        from database import add_audit_log
        await add_audit_log(action="shopware_chat.rollback", entity_type="shopware_chat_plan", entity_id=plan_id, user_id=actor, old_value={"status": "completed"}, new_value={"status": "rolled_back"})
    except Exception:
        pass
    return plan
