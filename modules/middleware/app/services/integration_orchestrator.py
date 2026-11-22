"""Integration Orchestrator / One-Click Go-Live foundation.

This module performs real checks against configured systems and persists
launch/run state in the DB. It does NOT fake external connectivity or
publication. Missing credentials/access are reported as blockers.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from database import (
    count_dashboard_metrics,
    get_setting,
    list_products,
    mcc_list_marketplaces,
    mcc_overview,
    recent_logs,
    set_setting,
    write_log,
)
from services.marketplace_command_center import build_connector

LAUNCH_SETTING_KEY = "omni_one_click_launch_state"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_load(raw: Any, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


async def load_launch_state() -> dict[str, Any]:
    raw = await get_setting(LAUNCH_SETTING_KEY)
    state = _json_load(raw, {})
    if not isinstance(state, dict):
        state = {}
    state.setdefault("status", "idle")
    state.setdefault("target_time", None)
    state.setdefault("timezone", "Europe/Berlin")
    state.setdefault("approved", False)
    state.setdefault("approved_at", None)
    state.setdefault("approved_by", "")
    state.setdefault("last_run_at", None)
    state.setdefault("last_preflight", None)
    state.setdefault("history", [])
    return state


async def save_launch_state(state: dict[str, Any]) -> dict[str, Any]:
    state["updated_at"] = utc_now()
    await set_setting(LAUNCH_SETTING_KEY, json.dumps(state, ensure_ascii=False, default=str))
    return state


async def discover_integrations(runtime: Any) -> dict[str, Any]:
    db_ok = False
    db_error = ""
    try:
        await count_dashboard_metrics()
        db_ok = True
    except Exception as e:
        db_error = type(e).__name__

    omniroute = {
        "configured": bool(getattr(runtime, "omniroute", None)),
        "healthy": bool(getattr(runtime, "omniroute_healthy", False)),
        "latency_ms": getattr(runtime, "omniroute_latency_ms", None),
        "base_url_configured": bool(os.getenv("OMNIROUTE_URL") or os.getenv("OMNIROUTE_BASE_URL")),
    }
    shopware_connections = []
    for conn_id, cm in (getattr(runtime, "shopware_connections", {}) or {}).items():
        shopware_connections.append({
            "id": conn_id,
            "shop_url": getattr(cm, "shop_url", ""),
            "api_version": getattr(cm, "api_version", ""),
            "configured": bool(getattr(cm, "shop_url", "") and getattr(cm, "client_id", "")),
        })

    marketplaces = await mcc_list_marketplaces()
    mp_status = []
    for mp in marketplaces:
        connector = build_connector(mp)
        mp_status.append({
            "id": mp.get("id"),
            "name": mp.get("name"),
            "connector_type": mp.get("connector_type"),
            "configured": bool(mp.get("credentials_configured")),
            "connector_registered": connector is not None,
            "last_health_status": mp.get("last_health_status"),
        })

    return {
        "status": "ok",
        "discovered_at": utc_now(),
        "systems": {
            "database": {"connected": db_ok, "error": db_error},
            "middleware": {"running": True, "version": getattr(runtime, "version", "unknown") if hasattr(runtime, "version") else "unknown"},
            "omniroute": omniroute,
            "shopware": {"connections": shopware_connections, "count": len(shopware_connections)},
            "marketplaces": {"items": mp_status, "count": len(mp_status)},
            "mcp": {
                "shopware_mcp_url_configured": bool(os.getenv("SHOPWARE_MCP_URL")),
                "shopware_mcp_enabled": os.getenv("SHOPWARE_MCP_ENABLED", "").lower() in {"1","true","yes","on"},
            },
        },
    }


async def run_preflight(runtime: Any) -> dict[str, Any]:
    discovery = await discover_integrations(runtime)
    systems = discovery["systems"]
    products = await list_products(limit=5000)
    overview = await mcc_overview()
    blockers: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if not systems["database"]["connected"]:
        blockers.append({"code": "DB_UNAVAILABLE", "message": "Database connection failed."})
    if not systems["omniroute"]["configured"]:
        blockers.append({"code": "OMNIROUTE_NOT_CONFIGURED", "message": "OmniRoute client not configured."})
    if not systems["shopware"]["count"]:
        blockers.append({"code": "SHOPWARE_NOT_CONFIGURED", "message": "No Shopware connection configured in middleware runtime."})
    if not products:
        warnings.append({"code": "NO_PRODUCTS", "message": "No products in middleware catalog; Shopware import/sync required."})

    mp_items = systems["marketplaces"]["items"]
    if not mp_items:
        warnings.append({"code": "NO_MARKETPLACES", "message": "No marketplaces configured in Marketplace Command Center."})
    for mp in mp_items:
        if not mp.get("connector_registered"):
            blockers.append({"code": "CONNECTOR_MISSING", "message": f"Connector not registered for {mp.get('name') or mp.get('id')} ({mp.get('connector_type')})."})
        if not mp.get("configured"):
            warnings.append({"code": "MARKETPLACE_CREDENTIALS_MISSING", "message": f"Credentials missing for {mp.get('name') or mp.get('id')}."})

    # External go-live requirements that cannot be safely assumed
    blockers.append({"code": "PAYMENT_NOT_VERIFIED", "message": "Payment provider configuration/test transaction not verified by this run."})
    blockers.append({"code": "SHIPPING_NOT_VERIFIED", "message": "Shipping/tax/legal checkout configuration not verified by this run."})
    blockers.append({"code": "LEGAL_NOT_VERIFIED", "message": "Legal/commercial required content not verified by this run."})

    approved_products = 0
    for p in products:
        price = p.get("retail_price") or p.get("sale_price") or p.get("supplier_price")
        stock = p.get("stock") or p.get("stock_quantity") or 0
        has_img = bool(p.get("main_image_url") or _json_load(p.get("gallery_json"), []))
        if price and float(stock or 0) >= 0 and has_img and str(p.get("status") or "active").lower() in {"active","enabled"}:
            approved_products += 1

    result = {
        "status": "blocked" if blockers else ("warning" if warnings else "ready"),
        "checked_at": utc_now(),
        "discovery": discovery,
        "counts": {
            "products": len(products),
            "approved_products_candidate": approved_products,
            "marketplaces": len(mp_items),
            "ai_selected_total": overview.get("ai_selected_total", 0),
        },
        "blockers": blockers,
        "warnings": warnings,
    }
    await write_log("INFO", "one_click_preflight", f"Preflight {result['status']}", {"blockers": len(blockers), "warnings": len(warnings)})
    return result


async def plan_launch(runtime: Any, *, target_time: str, approved: bool, approved_by: str = "") -> dict[str, Any]:
    state = await load_launch_state()
    preflight = await run_preflight(runtime)
    state["target_time"] = target_time
    state["timezone"] = "Europe/Berlin"
    state["approved"] = bool(approved)
    state["approved_by"] = approved_by
    state["approved_at"] = utc_now() if approved else None
    state["last_preflight"] = preflight
    state["status"] = "scheduled" if approved and preflight["status"] != "blocked" else "blocked"
    state.setdefault("history", []).insert(0, {
        "id": str(uuid.uuid4()),
        "at": utc_now(),
        "action": "plan_launch",
        "status": state["status"],
        "target_time": target_time,
        "approved": bool(approved),
        "blockers": len(preflight.get("blockers", [])),
    })
    state["history"] = state["history"][:50]
    await save_launch_state(state)
    await write_log("INFO", "one_click_launch_planned", f"Launch planned: {state['status']}", {"target_time": target_time, "approved": approved})
    return {"state": state, "preflight": preflight}


async def execute_due_launch(runtime: Any) -> dict[str, Any]:
    state = await load_launch_state()
    if state.get("status") != "scheduled":
        return {"executed": False, "reason": "not_scheduled", "state": state}
    target = state.get("target_time")
    if not target:
        return {"executed": False, "reason": "missing_target_time", "state": state}
    try:
        target_dt = datetime.fromisoformat(str(target).replace("Z", "+00:00"))
        if target_dt.tzinfo is None:
            target_dt = target_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return {"executed": False, "reason": "invalid_target_time", "state": state}
    now = datetime.now(timezone.utc)
    if now < target_dt:
        return {"executed": False, "reason": "not_due", "state": state, "seconds_remaining": int((target_dt-now).total_seconds())}

    preflight = await run_preflight(runtime)
    state["last_preflight"] = preflight
    state["last_run_at"] = utc_now()
    if preflight["status"] == "blocked":
        state["status"] = "blocked"
        result = {"executed": True, "launched": False, "reason": "preflight_blocked", "preflight": preflight}
    else:
        # Safe behavior: do not publish blindly. Mark as ready_for_manual_release if checks incomplete.
        state["status"] = "ready_for_manual_release"
        result = {"executed": True, "launched": False, "reason": "external_validation_required", "preflight": preflight}
    state.setdefault("history", []).insert(0, {"id": str(uuid.uuid4()), "at": utc_now(), "action": "execute_due_launch", "result": result["reason"]})
    state["history"] = state["history"][:50]
    await save_launch_state(state)
    await write_log("WARNING", "one_click_launch_executed", f"Launch execution result: {result['reason']}", {"target_time": target})
    return {**result, "state": state}
