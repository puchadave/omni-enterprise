from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

from database import (
    fetch_product,
    list_products,
    marketplace_dataset_upsert,
    marketplace_dataset_list,
    get_connection,
)
from .ai_task_routing import (
    TASK_VISION_ALT,
    build_product_facts,
    deterministic_alt_texts,
    deterministic_description,
    deterministic_seo,
    facts_hash,
    run_task_json,
)
from .error_codes import ErrorCode

logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _load_configured_channels() -> list[str]:
    # Prefer active marketplaces/connectors; fallback to core channels
    channels: list[str] = []
    try:
        async with get_connection() as db:
            try:
                cur = await db.execute("SELECT DISTINCT COALESCE(NULLIF(marketplace_id,''), NULLIF(name,''), id) AS c FROM marketplaces WHERE status IN ('active','enabled','configured','connected')")
                rows = await cur.fetchall(); await cur.close()
                for r in rows:
                    v = str((r['c'] if isinstance(r, dict) or hasattr(r,'keys') else r[0]) or "").strip().lower()
                    if v: channels.append(v)
            except Exception:
                pass
    except Exception:
        pass
    for c in ["shopware", "ebay", "kaufland", "amazon", "hood"]:
        if c not in channels:
            channels.append(c)
    # dedupe preserve order
    out = []
    seen = set()
    for c in channels:
        c = str(c).strip().lower()
        if not c or c in seen: continue
        seen.add(c); out.append(c)
    return out


def _validate_dataset(dataset: dict[str, Any], facts) -> tuple[str, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    title = str(dataset.get("title") or "").strip()
    desc = str(dataset.get("description") or "").strip()
    seo = dataset.get("seo") or {}
    images = dataset.get("images") or []
    if not title: errors.append("missing_title")
    if len(desc) < 40: errors.append("weak_description")
    if not str(seo.get("seo_title") or "").strip(): errors.append("missing_seo_title")
    if not str(seo.get("meta_description") or "").strip(): errors.append("missing_meta_description")
    if not images: warnings.append("missing_images")
    if facts.price is None or float(facts.price or 0) <= 0: errors.append("missing_price")
    if facts.stock <= 0: warnings.append("no_stock")
    if dataset.get("hallucination_blocked"): errors.append(ErrorCode.WARN_HALLUCINATION_BLOCKED.value)
    status = "ready"
    if errors:
        status = "blocked" if any(e in {"missing_title","missing_price"} for e in errors) else "incomplete"
    elif warnings:
        status = "incomplete"
    return status, errors, warnings


async def _generate_channel_dataset(product: dict[str, Any], channel: str, *, runtime: Any = None, import_run_id: str = "") -> dict[str, Any]:
    facts = build_product_facts(product)
    fh = facts_hash(facts)
    desc = deterministic_description(facts)
    seo = deterministic_seo(facts, channel)
    alt_texts = deterministic_alt_texts(facts)

    # AI refinement with strict fallback
    if runtime is not None and getattr(runtime, "omniroute", None) is not None:
        desc_prompt = f"Erzeuge JSON title,short_description,description,bullets. Nur Fakten. Kanal={channel}. FACTS={facts.to_dict()}"
        seo_prompt = f"Erzeuge JSON seo_title,meta_description,slug,keywords. Nur Fakten. Kanal={channel}. FACTS={facts.to_dict()}"
        vision_prompt = f"Erzeuge JSON alt_texts=[{{position,alt_text}}] aus Bildmetadaten/Fakten. Keine erfundenen Details. FACTS={facts.to_dict()}"
        desc_res = await run_task_json("description", facts=facts, prompt=desc_prompt, runtime=runtime)
        seo_res = await run_task_json("seo", facts=facts, prompt=seo_prompt, runtime=runtime)
        vis_res = await run_task_json(TASK_VISION_ALT, facts=facts, prompt=vision_prompt, runtime=runtime)
        if isinstance(desc_res.output, dict) and desc_res.status in {"ok", "fallback"}:
            for k in ["title","short_description","description","bullets"]:
                if desc_res.output.get(k): desc[k] = desc_res.output.get(k)
            desc["model"] = desc_res.model; desc["prompt_version"] = desc_res.prompt_version
            desc["hallucination_blocked"] = bool(desc_res.output.get("hallucination_blocked"))
        if isinstance(seo_res.output, dict) and seo_res.status in {"ok","fallback"}:
            for k in ["seo_title","meta_description","slug","keywords"]:
                if seo_res.output.get(k): seo[k] = seo_res.output.get(k)
            seo["model"] = seo_res.model; seo["prompt_version"] = seo_res.prompt_version
        if isinstance(vis_res.output, dict) and isinstance(vis_res.output.get("alt_texts"), list):
            by_pos = {int(x.get("position", -1)): str(x.get("alt_text") or "") for x in vis_res.output.get("alt_texts", []) if isinstance(x, dict)}
            for a in alt_texts:
                if by_pos.get(int(a.get("position", -1))):
                    a["alt_text"] = by_pos[int(a["position"])][:125]
                    a["model"] = vis_res.model

    images = []
    for i, img in enumerate(facts.images or []):
        alt = next((a.get("alt_text") for a in alt_texts if int(a.get("position",-1)) == i), None)
        images.append({**img, "alt_text": alt, "position": i})
    dataset = {
        "product_sku": facts.sku,
        "channel": channel,
        "import_run_id": import_run_id,
        "facts_hash": fh,
        "title": str(desc.get("title") or facts.title or facts.sku)[:180],
        "short_description": str(desc.get("short_description") or "")[:500],
        "description": str(desc.get("description") or "")[:8000],
        "bullets": desc.get("bullets") or [],
        "seo": seo,
        "alt_texts": alt_texts,
        "attributes": facts.attributes,
        "category": facts.category,
        "brand": facts.brand,
        "price": facts.price,
        "currency": facts.currency,
        "stock": facts.stock,
        "images": images,
        "hallucination_blocked": bool(desc.get("hallucination_blocked")),
        "ai_model": str(desc.get("model") or seo.get("model") or "deterministic_fallback"),
        "prompt_version": str(desc.get("prompt_version") or seo.get("prompt_version") or "fallback-v1"),
        "generated_at": utc_now(),
    }
    status, errors, warnings = _validate_dataset(dataset, facts)
    dataset["status"] = status
    dataset["validation_errors"] = errors
    dataset["warnings"] = warnings
    return dataset


async def enrich_products_for_channels(*, skus: list[str] | None = None, channels: list[str] | None = None, import_run_id: str = "", runtime: Any = None, limit: int = 100) -> dict[str, Any]:
    selected_channels = [str(c).strip().lower() for c in (channels or await _load_configured_channels()) if str(c).strip()]
    if skus:
        products = []
        for sku in skus[:limit]:
            p = await fetch_product(str(sku).strip())
            if p: products.append(p)
    else:
        products = await list_products(limit=limit)

    sem = asyncio.Semaphore(int(os.getenv("ENRICHMENT_CONCURRENCY", "4")))
    results: list[dict[str, Any]] = []

    async def _one(product: dict[str, Any], channel: str):
        async with sem:
            sku = str(product.get("sku") or "").strip()
            try:
                ds = await _generate_channel_dataset(product, channel, runtime=runtime, import_run_id=import_run_id)
                await marketplace_dataset_upsert(ds)
                return {"sku": sku, "channel": channel, "status": ds["status"], "facts_hash": ds["facts_hash"]}
            except Exception as exc:
                logger.exception("dataset_generation_failed sku=%s channel=%s", sku, channel)
                fail = {
                    "product_sku": sku, "channel": channel, "import_run_id": import_run_id,
                    "status": "blocked", "facts_hash": "", "title": product.get("title") or sku,
                    "description": "", "seo": {}, "images": product.get("gallery_json") or [],
                    "validation_errors": ["generation_failed"], "warnings": [str(exc)[:300]],
                    "ai_model": "", "prompt_version": "",
                }
                try: await marketplace_dataset_upsert(fail)
                except Exception: pass
                return {"sku": sku, "channel": channel, "status": "blocked", "error": str(exc)[:300]}

    tasks = []
    for p in products:
        sku = str(p.get("sku") or "").strip()
        if not sku: continue
        for ch in selected_channels:
            tasks.append(_one(p, ch))
    for coro in asyncio.as_completed(tasks):
        results.append(await coro)

    summary = {"total": len(results), "ready": sum(1 for r in results if r.get("status") == "ready"), "incomplete": sum(1 for r in results if r.get("status") == "incomplete"), "blocked": sum(1 for r in results if r.get("status") == "blocked")}
    return {"status": "ok", "channels": selected_channels, "summary": summary, "results": results[:2000]}


async def enrich_after_matterhorn_import(*, import_run_id: str, skus: list[str], runtime: Any = None) -> dict[str, Any]:
    if not skus:
        return {"status": "skipped", "reason": "no_skus"}
    return await enrich_products_for_channels(skus=skus, import_run_id=import_run_id, runtime=runtime)
