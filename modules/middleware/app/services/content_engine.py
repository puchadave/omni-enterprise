"""Multi-channel AI content engine (scaffold, OmniRoute-integrated when configured).

Generates platform-specific drafts from canonical/enriched product data, validates basic
constraints, versions outputs, and enforces approval-before-export. No invented facts.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from database import content_version_create, content_version_list, knowledge_list

PLATFORM_PROFILES: dict[str, dict[str, Any]] = {
    "shopware": {"title_max": 255, "short_max": 500, "desc_max": 20000, "html": True, "bullets": True},
    "ebay": {"title_max": 80, "short_max": 500, "desc_max": 40000, "html": True, "bullets": True},
    "amazon": {"title_max": 200, "short_max": 2000, "desc_max": 2000, "html": False, "bullets": True},
    "google_shopping": {"title_max": 150, "short_max": 5000, "desc_max": 5000, "html": False, "bullets": False},
    "social": {"title_max": 120, "short_max": 2200, "desc_max": 2200, "html": False, "bullets": True},
}


def _safe_text(v: Any, limit: int = 5000) -> str:
    s = str(v or "").strip()
    return s[:limit]


def _known_product_facts(product: dict[str, Any]) -> dict[str, Any]:
    keys = ["sku", "title", "name", "ai_title", "brand", "manufacturer", "category", "color", "material", "size", "ean", "gtin", "supplier_price", "price", "raw_description", "description"]
    facts = {}
    for k in keys:
        if k in product and product.get(k) not in (None, "", [], {}):
            facts[k] = product.get(k)
    return facts


def validate_platform_content(platform: str, content: dict[str, Any]) -> dict[str, Any]:
    profile = PLATFORM_PROFILES.get(platform, {"title_max": 255, "short_max": 500, "desc_max": 20000})
    issues = []
    title = _safe_text(content.get("title") or content.get("seo_title") or "")
    desc = _safe_text(content.get("description") or content.get("long_description") or "", 50000)
    if len(title) > profile.get("title_max", 255):
        issues.append(f"title_too_long>{profile['title_max']}")
    if len(desc) > profile.get("desc_max", 20000):
        issues.append(f"description_too_long>{profile['desc_max']}")
    if not profile.get("html", True):
        if re.search(r"<[^>]+>", desc):
            issues.append("html_not_allowed")
    # claim guard (very basic)
    if re.search(r"\b(zertifiziert|garantiert|medizinisch|heilend)\b", desc.lower()):
        issues.append("potential_unverified_claim_manual_review")
    return {"valid": len([i for i in issues if not i.startswith('potential_')]) == 0, "issues": issues, "profile": profile}


async def generate_channel_draft(product: dict[str, Any], channel: str, model: str | None = None, omniroute_client: Any = None) -> dict[str, Any]:
    facts = _known_product_facts(product)
    base_title = _safe_text(facts.get("ai_title") or facts.get("title") or facts.get("name") or facts.get("sku") or "Produkt")
    base_desc = _safe_text(facts.get("raw_description") or facts.get("description") or "", 4000)
    attrs = [f"{k}: {v}" for k, v in facts.items() if k in ("brand","manufacturer","category","color","material","size","ean","gtin")]

    draft = {
        "channel": channel,
        "title": base_title[:PLATFORM_PROFILES.get(channel, {}).get("title_max", 255)],
        "short_description": (base_desc[:300] or f"{base_title} – Details gemäß vorliegenden Produktdaten."),
        "description": base_desc or f"{base_title}. " + (" ".join(attrs) if attrs else "Weitere Eigenschaften nur bei belegter Quelle ergänzen."),
        "bullets": [a for a in attrs[:8]] or ["Produktdaten gemäß Lieferanten-/kanonischer Quelle"],
        "seo_title": base_title[:70],
        "meta_description": (base_desc[:150] or base_title)[:160],
        "keywords": [str(facts.get("category") or "").strip(), str(facts.get("brand") or "").strip()] if facts else [],
        "facts_used": facts,
        "generated_by": "template_fallback" if omniroute_client is None else "omniroute_requested",
        "model": model or "",
        "approval_status": "draft",
    }

    # If omniroute client provided and configured, try to generate richer text (best-effort)
    if omniroute_client is not None:
        try:
            prompt = (
                "Erzeuge plattformgerechten Produktcontent als JSON mit keys title, short_description, description, bullets, seo_title, meta_description, keywords. "
                "Nutze ausschließlich belegte Fakten, erfinde keine Eigenschaften/Zertifikate/Claims. "
                f"Kanal: {channel}. Fakten: {json.dumps(facts, ensure_ascii=False)}"
            )
            # support common client shapes
            if hasattr(omniroute_client, "chat_completions"):
                resp = await omniroute_client.chat_completions(messages=[{"role": "user", "content": prompt}], model=model)
            elif hasattr(omniroute_client, "generate_text"):
                resp = await omniroute_client.generate_text(prompt=prompt, model=model)
            else:
                resp = None
            if resp:
                text = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
                # try extract json
                m = re.search(r"\{.*\}", text, re.S)
                if m:
                    parsed = json.loads(m.group(0))
                    for k in ["title","short_description","description","bullets","seo_title","meta_description","keywords"]:
                        if k in parsed and parsed[k]:
                            draft[k] = parsed[k]
                    draft["generated_by"] = "omniroute"
        except Exception:
            pass

    validation = validate_platform_content(channel, draft)
    draft["validation"] = validation
    return draft


async def generate_and_store(product: dict[str, Any], channels: list[str], omniroute_client: Any = None, model: str | None = None) -> dict[str, Any]:
    results = []
    for ch in channels:
        d = await generate_channel_draft(product, ch, model=model, omniroute_client=omniroute_client)
        vid = await content_version_create(
            product_sku=str(product.get("sku") or ""),
            channel=ch,
            content=d,
            status="draft",
            model=str(d.get("model") or ""),
            prompt_version="v1",
        )
        d["version_id"] = vid
        results.append(d)
    return {"sku": product.get("sku"), "count": len(results), "items": results}
