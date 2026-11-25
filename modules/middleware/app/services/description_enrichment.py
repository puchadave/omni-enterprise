from __future__ import annotations

from typing import Any

from .enrichment_contracts import ProductSource, DescriptionSeoResult
from .ai_task_routing import build_product_facts, deterministic_description, deterministic_seo


class DescriptionEnrichmentService:
    def __init__(self, locale: str = "de-DE"):
        self.locale = locale

    def enrich(self, product: ProductSource) -> DescriptionSeoResult:
        facts = build_product_facts({
            "sku": product.sku,
            "title": product.name,
            "description": product.description,
            "price": product.price,
            "brand": product.manufacturer,
            "category": product.category,
            "ean": product.gtin,
            "gallery_json": [{"url": u} for u in (product.images or [])],
        })
        desc = deterministic_description(facts)
        seo = deterministic_seo(facts, "default")
        alt_texts = [f"{product.name} - Ansicht {i + 1}" for i in range(len(product.images or []))]
        return DescriptionSeoResult(
            title=str(desc.get("title") or product.name)[:70],
            meta_description=str(seo.get("meta_description") or product.description)[:160],
            slug=str(seo.get("slug") or "")[:200],
            alt_texts=alt_texts,
        )

    async def enrich_async(self, product: dict[str, Any], *, runtime: Any = None, channel: str = "default") -> dict[str, Any]:
        from .ai_task_routing import TASK_DESCRIPTION, TASK_SEO, run_task_json

        facts = build_product_facts(product)
        desc_prompt = (
            "Erzeuge JSON mit Feldern title,short_description,description,bullets. "
            "Nutze ausschließlich Fakten. Keine erfundenen Zertifikate/Garantien/medizinischen Claims.\n"
            f"FACTS={facts.to_dict()}"
        )
        seo_prompt = (
            f"Erzeuge JSON für Kanal {channel} mit Feldern seo_title,meta_description,slug,keywords. "
            "Nur aus Fakten, keine Halluzinationen.\n"
            f"FACTS={facts.to_dict()}"
        )
        desc_res = await run_task_json(TASK_DESCRIPTION, facts=facts, prompt=desc_prompt, runtime=runtime)
        seo_res = await run_task_json(TASK_SEO, facts=facts, prompt=seo_prompt, runtime=runtime)
        desc_out = desc_res.output if isinstance(desc_res.output, dict) else {}
        seo_out = seo_res.output if isinstance(seo_res.output, dict) else {}
        if channel != "default":
            # channel-specific deterministic fallback/normalization
            seo_out.setdefault("channel", channel)
            seo_out["seo_title"] = str(seo_out.get("seo_title") or desc_out.get("title") or facts.title or facts.sku)[:70]
            seo_out["meta_description"] = str(seo_out.get("meta_description") or desc_out.get("short_description") or facts.description)[:160]
        return {
            "description": desc_out,
            "seo": seo_out,
            "facts_hash": facts.to_dict(),
            "description_result": desc_res.__dict__,
            "seo_result": seo_res.__dict__,
        }
