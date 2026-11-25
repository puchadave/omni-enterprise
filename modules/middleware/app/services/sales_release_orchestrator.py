"""MID Release / AI selection / automatic import / Shopware sync / Go-Live orchestration.

Real backend orchestration. No fake success: external checks report blockers/errors.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from database import (
    count_products,
    fetch_product,
    lifecycle_event_add,
    list_products,
    release_state_get,
    release_state_set,
    sales_go_live_run_create,
    sales_go_live_run_update,
    sales_go_live_runs_list,
    sales_state_get,
    sales_state_list,
    sales_state_upsert,
    upsert_product,
    write_log,
)

LIFECYCLE = [
    "DISCOVERED","AI_ANALYZED","AI_SELECTED","VALIDATED","IMPORT_APPROVED",
    "IMPORTED","SHOPWARE_SYNCED","READY_FOR_SALE","ONLINE"
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _j(v: Any, default: Any) -> Any:
    if v is None or v == "":
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default


async def _set_state(sku: str, status: str, *, actor: str = "system", message: str = "", details: Any = None, **fields: Any) -> dict[str, Any]:
    fields.setdefault("lifecycle_status", status)
    row = await sales_state_upsert(sku, **fields)
    await lifecycle_event_add(sku, "lifecycle", status, actor=actor, message=message, details=details)
    return row


def _score_product(p: dict[str, Any]) -> tuple[float, list[str]]:
    reasons=[]; score=0.0
    title=str(p.get("title") or p.get("product_name") or "").strip()
    desc=str(p.get("raw_description") or p.get("description") or "").strip()
    imgs=_j(p.get("gallery_json"), []) or ([] if not p.get("main_image_url") else [p.get("main_image_url")])
    stock=float(p.get("stock_quantity") or p.get("stock") or 0)
    buy=float(p.get("purchase_price") or p.get("supplier_price") or 0)
    sell=float(p.get("sale_price") or p.get("retail_price") or 0)
    if title: score += 15
    else: reasons.append("missing_title")
    if len(desc) >= 40: score += 15
    else: reasons.append("weak_description")
    if imgs: score += 15
    else: reasons.append("missing_images")
    if p.get("category"): score += 10
    else: reasons.append("missing_category")
    if p.get("ean") or p.get("gtin") or p.get("barcode"): score += 10
    else: reasons.append("missing_gtin")
    if stock > 0: score += 10
    else: reasons.append("no_stock")
    if sell > 0: score += 10
    else: reasons.append("missing_sale_price")
    if sell>0 and buy>0:
        margin=(sell-buy)/sell*100
        if margin>=15: score += 15; reasons.append(f"margin_ok:{margin:.1f}%")
        elif margin>=0: score += 5; reasons.append(f"margin_low:{margin:.1f}%")
        else: reasons.append(f"margin_negative:{margin:.1f}%")
    elif sell>0:
        score += 5; reasons.append("margin_unknown")
    return min(score,100.0), reasons


def _candidate_payload(p: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    imgs=_j(p.get("gallery_json"), []) or ([] if not p.get("main_image_url") else [p.get("main_image_url")])
    score = float(state.get("selection_score") or 0)
    reasons = [r for r in str(state.get("selection_reason") or "").split("; ") if r]
    return {
        "sku": str(p.get("sku") or state.get("sku") or ""),
        "title": p.get("title") or p.get("product_name") or "",
        "description": p.get("raw_description") or p.get("description") or "",
        "category": p.get("category") or "",
        "brand": p.get("brand") or "",
        "ean": p.get("ean") or p.get("gtin") or p.get("barcode") or "",
        "stock": float(p.get("stock_quantity") or p.get("stock") or 0),
        "price": float(p.get("sale_price") or p.get("retail_price") or p.get("price") or 0),
        "images": imgs[:10],
        "main_image_url": p.get("main_image_url") or (imgs[0] if imgs else ""),
        "lifecycle_status": state.get("lifecycle_status"),
        "selection_score": score,
        "selection_reasons": reasons,
        "validation_errors": _j(state.get("validation_errors_json"), []),
        "shopware_product_id": state.get("shopware_product_id") or "",
        "shopware_readback": _j(state.get("shopware_readback_json"), {}),
        "last_error": state.get("last_error") or "",
        "imported_at": state.get("imported_at"),
        "synced_at": state.get("synced_at"),
        "ready_for_sale_at": state.get("ready_for_sale_at"),
        "online_at": state.get("online_at"),
        "updated_at": state.get("updated_at"),
    }


async def select_products(*, limit: int = 50, min_score: float = 55.0, actor: str = "system") -> dict[str, Any]:
    products = await list_products(limit=max(limit*3,100))
    selected=[]; rejected=[]
    for p in products:
        sku=str(p.get("sku") or "").strip()
        if not sku: continue
        score,reasons=_score_product(p)
        await _set_state(sku, "AI_ANALYZED", actor=actor, message="AI analysis completed", details={"score":score,"reasons":reasons}, selection_score=score, selection_reason="; ".join(reasons[:12]))
        if score >= min_score and "missing_title" not in reasons and "missing_sale_price" not in reasons:
            await _set_state(sku, "AI_SELECTED", actor=actor, message="Selected by AI", details={"score":score}, selection_score=score)
            selected.append({"sku":sku,"score":score,"reasons":reasons})
        else:
            rejected.append({"sku":sku,"score":score,"reasons":reasons})
        if len(selected)>=limit: break
    await write_log("INFO","ai_product_selection",f"Selected {len(selected)} products",{"min_score":min_score})
    return {"status":"ok","selected":selected,"rejected":rejected[:200],"counts":{"selected":len(selected),"rejected":len(rejected)}}


def _validate_product(p: dict[str, Any]) -> list[str]:
    errs=[]
    if not str(p.get("sku") or "").strip(): errs.append("missing_sku")
    if not str(p.get("title") or "").strip(): errs.append("missing_title")
    sell=float(p.get("sale_price") or p.get("retail_price") or 0)
    if sell <= 0: errs.append("invalid_price")
    imgs=_j(p.get("gallery_json"), [])
    if not imgs and not p.get("main_image_url"): errs.append("missing_images")
    if not str(p.get("category") or "").strip(): errs.append("missing_category")
    return errs


async def validate_selected(*, actor: str = "system", skus: list[str] | None = None) -> dict[str, Any]:
    wanted = {str(s).strip() for s in (skus or []) if str(s).strip()}
    selected = await sales_state_list(limit=5000, status="AI_SELECTED")
    if wanted:
        selected = [s for s in selected if str(s.get("sku") or "").strip() in wanted]
    ok=[]; blocked=[]
    for s in selected:
        sku=s.get("sku"); p=await fetch_product(sku)
        if not p:
            await _set_state(sku,"IMPORT_BLOCKED",actor=actor,message="Product missing in catalog",validation_errors_json=["product_missing"]); blocked.append({"sku":sku,"errors":["product_missing"]}); continue
        errs=_validate_product(p)
        if errs:
            await _set_state(sku,"IMPORT_BLOCKED",actor=actor,message="Validation failed",details={"errors":errs},validation_errors_json=errs,last_error=";".join(errs))
            blocked.append({"sku":sku,"errors":errs})
        else:
            await _set_state(sku,"VALIDATED",actor=actor,message="Validation passed",validation_errors_json=[])
            await _set_state(sku,"IMPORT_APPROVED",actor=actor,message="Import approved")
            ok.append({"sku":sku})
    return {"status":"ok","approved":ok,"blocked":blocked,"counts":{"approved":len(ok),"blocked":len(blocked),"scanned":len(selected)}}


async def import_to_shopware(runtime: Any, *, limit: int = 100, actor: str = "system", skus: list[str] | None = None) -> dict[str, Any]:
    approved = await sales_state_list(limit=5000, status="IMPORT_APPROVED")
    wanted = {str(s).strip() for s in (skus or []) if str(s).strip()}
    if wanted:
        approved = [s for s in approved if str(s.get("sku") or "").strip() in wanted]
    results=[]; errors=[]
    for s in approved[:limit]:
        sku=str(s.get("sku")); p=await fetch_product(sku)
        if not p: 
            await _set_state(sku,"IMPORT_BLOCKED",actor=actor,message="Missing product",last_error="product_missing")
            errors.append({"sku":sku,"error":"product_missing"}); continue
        # dedup by sku existing in shopware via connection manager lookup if available
        cm = next(iter((getattr(runtime,'shopware_connections',{}) or {}).values()), None)
        if not cm:
            await _set_state(sku,"IMPORT_BLOCKED",actor=actor,message="No Shopware connection configured",last_error="shopware_not_configured")
            errors.append({"sku":sku,"error":"shopware_not_configured"}); continue
        try:
            tax_id = os.getenv("SHOPWARE_DEFAULT_TAX_ID","") or getattr(cm, "default_tax_id", "") or "01994d23acaa735295fdd1be0ed54aff"
            currency_id = os.getenv("SHOPWARE_DEFAULT_CURRENCY_ID","") or getattr(cm, "default_currency_id", "") or "b7d2554b0ce847cd82f3ac9bd1c0dfca"
            gross = float(p.get("sale_price") or p.get("price") or 0)
            net = round(gross / 1.19, 2) if gross else 0.0
            sw_payload={
                "name": p.get("title") or sku,
                "productNumber": sku,
                "stock": int(float(p.get("stock_quantity") or p.get("stock") or 0)),
                "taxId": tax_id,
                "price": [{"currencyId": currency_id, "gross": gross, "net": net, "linked": False}],
                "active": False,
                "description": p.get("raw_description") or p.get("description") or "",
            }
            created = await cm.create_product(sw_payload)
            sw_id = str(created.get("id") or created.get("data",{}).get("id") or "")
            if not sw_id:
                # Shopware create often returns 204; fallback by productNumber
                try:
                    if hasattr(cm, '_request'):
                        resp = await cm._request('POST','/api/search/product', json_body={"filter":[{"type":"equals","field":"productNumber","value":sku}],"limit":1}, use_admin_api=True)
                        data = resp.json() if hasattr(resp,'json') else {}
                        els = (data.get('data') or [])
                        if els: sw_id = str(els[0].get('id') or '')
                except Exception:
                    pass
            await _set_state(sku,"IMPORTED",actor=actor,message="Imported to Shopware",shopware_product_id=sw_id, imported_at=now_iso())
            readback=None
            if sw_id and hasattr(cm, '_request'):
                try:
                    rb = await cm._request('GET', f"/api/product/{sw_id}", use_admin_api=True)
                    readback = rb.json() if hasattr(rb,'json') else {"raw": str(rb)}
                except Exception as e:
                    readback={"error":str(e)}
            ok_readback = bool(sw_id) and isinstance(readback, dict) and not readback.get("errors") and not readback.get("error")
            if ok_readback:
                await _set_state(sku,"SHOPWARE_SYNCED",actor=actor,message="Shopware readback verified",shopware_readback_json=readback, synced_at=now_iso())
                await _set_state(sku,"READY_FOR_SALE",actor=actor,message="Ready for sale",ready_for_sale_at=now_iso())
                results.append({"sku":sku,"shopware_product_id":sw_id,"status":"READY_FOR_SALE"})
            else:
                await _set_state(sku,"IMPORT_BLOCKED",actor=actor,message="Shopware readback failed",last_error="shopware_readback_failed",shopware_readback_json=readback or {"error":"missing_readback"})
                errors.append({"sku":sku,"error":"shopware_readback_failed","shopware_product_id":sw_id})
        except Exception as e:
            await _set_state(sku,"IMPORT_BLOCKED",actor=actor,message="Shopware import failed",last_error=str(e)[:500])
            errors.append({"sku":sku,"error":str(e)[:300]})
    return {"status":"ok","imported":results,"errors":errors,"counts":{"imported":len(results),"errors":len(errors)}}


async def run_preflight(runtime: Any) -> dict[str, Any]:
    blockers=[]; warnings=[]
    conns = (getattr(runtime,'shopware_connections',{}) or {})
    cm = next(iter(conns.values()), None)
    shopware_url = os.getenv("SHOPWARE_URL") or os.getenv("SHOPWARE_APP_URL")
    if not cm and shopware_url:
        warnings.append({"code":"SHOPWARE_RUNTIME_CONNECTION_MISSING","message":"SHOPWARE_URL configured but no runtime Shopware connection registered; import requires configured connection/credentials."})
    if not cm and not conns:
        blockers.append({"code":"SHOPWARE_NOT_CONFIGURED","message":"No Shopware connection configured"})
    elif cm:
        try:
            h = await cm.check_health()
            if not isinstance(h,dict) or h.get("status") in {"error","unhealthy","auth_error"}:
                blockers.append({"code":"SHOPWARE_UNHEALTHY","message":f"Shopware health check failed: {(h or {}).get('error','unknown')}"})
        except Exception as e:
            blockers.append({"code":"SHOPWARE_HEALTH_ERROR","message":f"Shopware health check exception: {e}"})
    total = await count_products()
    if total <= 0: warnings.append({"code":"NO_PRODUCTS","message":"No products in catalog"})
    ready = await sales_state_list(limit=5000, status="READY_FOR_SALE")
    if not ready: blockers.append({"code":"NO_READY_FOR_SALE","message":"No products in READY_FOR_SALE state"})
    # critical business checks cannot be auto-verified safely here
    for code,msg in [
        ("PAYMENT_NOT_VERIFIED","Payment provider/test transaction not verified"),
        ("SHIPPING_NOT_VERIFIED","Shipping/tax configuration not verified"),
        ("LEGAL_NOT_VERIFIED","Legal/compliance shop content not verified"),
    ]:
        blockers.append({"code":code,"message":msg})
    return {"status":"blocked" if blockers else ("warning" if warnings else "ready"),"checked_at":now_iso(),"blockers":blockers,"warnings":warnings,"counts":{"products":total,"ready_for_sale":len(ready)}}


async def list_ai_candidates(*, limit: int = 100, offset: int = 0, status: str | None = None, search: str | None = None, min_score: float | None = None) -> dict[str, Any]:
    states = await sales_state_list(limit=5000, status=status)
    q = str(search or "").strip().lower()
    out=[]
    for st in states:
        sku=str(st.get("sku") or "").strip()
        if not sku: continue
        if min_score is not None and float(st.get("selection_score") or 0) < float(min_score):
            continue
        p = await fetch_product(sku)
        if not p:
            if q and q not in sku.lower():
                continue
            out.append(_candidate_payload({"sku": sku, "title": ""}, st))
            continue
        hay = " ".join([sku, str(p.get("title") or ""), str(p.get("brand") or ""), str(p.get("category") or ""), str(p.get("ean") or p.get("gtin") or "")]).lower()
        if q and q not in hay:
            continue
        out.append(_candidate_payload(p, st))
    total=len(out)
    page=out[max(0,offset):max(0,offset)+max(1,min(limit,500))]
    return {"status":"ok","total":total,"limit":limit,"offset":offset,"items":page}


async def get_ai_candidate(sku: str) -> dict[str, Any] | None:
    st = await sales_state_get(str(sku or "").strip())
    if not st:
        return None
    p = await fetch_product(str(sku or "").strip()) or {"sku": sku}
    return _candidate_payload(p, st)


async def start_sales(runtime: Any, *, actor: str = "system", mode: str = "manual", approved: bool = False, target_time: str | None = None) -> dict[str, Any]:
    run = await sales_go_live_run_create({"status":"running","mode":mode,"target_time":target_time,"requested_by":actor,"approved":approved,"preflight_json":{},"result_json":{}})
    try:
        sel = await select_products(actor=actor)
        val = await validate_selected(actor=actor)
        imp = await import_to_shopware(runtime, actor=actor)
        pre = await run_preflight(runtime)
        status = "blocked"
        result={"selection":sel["counts"],"validation":{"approved":len(val["approved"]),"blocked":len(val["blocked"])},"import":imp["counts"],"preflight":pre}
        if pre["status"] == "blocked" or not approved:
            status="blocked"
            if not approved: pre["blockers"].append({"code":"APPROVAL_REQUIRED","message":"Explicit approval required"})
            result["preflight"]=pre
        else:
            # activate only READY_FOR_SALE products; actual storefront activation requires verified shop config
            ready = await sales_state_list(limit=5000, status="READY_FOR_SALE")
            activated=[]
            for s in ready:
                sku=s["sku"]
                await _set_state(sku,"ONLINE",actor=actor,message="Marked online by controlled go-live (requires Shopware active+readback in production)", online_at=now_iso())
                activated.append(sku)
            status="completed_with_manual_gates"
            result["activated"]=activated
        await sales_go_live_run_update(run["id"], status=status, preflight_json=pre, result_json=result, finished_at=now_iso(), approved=approved)
        await release_state_set("mid_release", {"status":"PRODUCTION_CANDIDATE" if status.startswith("completed") else "BLOCKED", "last_run_id":run["id"], "updated_at":now_iso()})
        return {"status":"ok","run_id":run["id"],"run_status":status,"result":result}
    except Exception as e:
        await sales_go_live_run_update(run["id"], status="failed", error_message=str(e)[:500], finished_at=now_iso())
        raise


# --- AI enrichment/import decision helpers (phase 2 pipeline contract) ---
def build_shopware_import_decision(product: Any, media_cover_url: str | None = None):
    from .enrichment_contracts import ShopwareImportDecision

    blocking_errors = []
    price_fallback_used = False
    price = getattr(product, "price", None)
    if price is None or float(price) <= 0:
        blocking_errors.append("ERR_INVALID_PRICE")
        price_fallback_used = True
    for field in ("sku", "name", "category", "manufacturer"):
        value = getattr(product, field, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            blocking_errors.append("ERR_MISSING_REQUIRED_FIELD")
    cover_url = media_cover_url or (getattr(product, "images", []) or [None])[0]
    if not cover_url:
        blocking_errors.append("ERR_MISSING_COVER_IMAGE")
    blocking_errors = sorted(set(blocking_errors))
    approved = len(blocking_errors) == 0
    return ShopwareImportDecision(
        active=approved,
        price_fallback_used=price_fallback_used,
        blocking_errors=blocking_errors,
        export_gate="APPROVED" if approved else "BLOCKED",
        hallucination_blocked=False,
        cover_url=cover_url,
    )
