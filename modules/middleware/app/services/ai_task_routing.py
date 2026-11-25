from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Awaitable

from .enrichment_contracts import ProductFacts, AITaskResult

logger = logging.getLogger(__name__)

TASK_FACTS = "facts_extraction"
TASK_DESCRIPTION = "description"
TASK_SEO = "seo"
TASK_VISION_ALT = "vision_alt_text"
TASK_MARKETPLACE = "marketplace_dataset"
TASK_STYLE_SCORING = "style_scoring"
ALL_TASKS = [TASK_FACTS, TASK_DESCRIPTION, TASK_SEO, TASK_VISION_ALT, TASK_MARKETPLACE, TASK_STYLE_SCORING]


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def facts_hash(facts: dict[str, Any] | ProductFacts) -> str:
    raw = facts.to_dict() if isinstance(facts, ProductFacts) else facts
    return hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()


@dataclass
class AITaskProfile:
    task: str
    preferred_models: list[str] = field(default_factory=list)
    fallback_models: list[str] = field(default_factory=list)
    prompt_version: str = "v1"
    temperature: float = 0.2
    max_tokens: int = 1200
    timeout: float = 45.0
    capability: str = "text_generation"
    deterministic_fallback: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_PROFILES: dict[str, AITaskProfile] = {
    TASK_FACTS: AITaskProfile(TASK_FACTS, ["auto", "gpt-4.1-mini", "gemini-2.0-flash"], ["auto"], temperature=0.0, max_tokens=900),
    TASK_DESCRIPTION: AITaskProfile(TASK_DESCRIPTION, ["gpt-4.1", "claude-3.7-sonnet", "auto"], ["auto"], temperature=0.4, max_tokens=1400),
    TASK_SEO: AITaskProfile(TASK_SEO, ["gpt-4.1-mini", "gemini-2.0-flash", "auto"], ["auto"], temperature=0.2, max_tokens=700),
    TASK_VISION_ALT: AITaskProfile(TASK_VISION_ALT, ["gpt-4.1-mini", "gemini-2.0-flash", "auto"], ["auto"], capability="image_analysis", temperature=0.1, max_tokens=500),
    TASK_MARKETPLACE: AITaskProfile(TASK_MARKETPLACE, ["gpt-4.1-mini", "auto"], ["auto"], temperature=0.2, max_tokens=1600),
    TASK_STYLE_SCORING: AITaskProfile(TASK_STYLE_SCORING, ["gpt-4.1-mini", "gemini-2.0-flash", "auto"], ["auto"], temperature=0.0, max_tokens=500),
}


def get_profile(task: str, settings: dict[str, Any] | None = None) -> AITaskProfile:
    base = DEFAULT_PROFILES.get(task, AITaskProfile(task))
    settings = settings or {}
    raw = settings.get(f"ai_task_profile.{task}") or settings.get("ai_task_profiles", {}).get(task)
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = None
    if isinstance(raw, dict):
        return AITaskProfile(
            task=task,
            preferred_models=list(raw.get("preferred_models") or base.preferred_models),
            fallback_models=list(raw.get("fallback_models") or base.fallback_models),
            prompt_version=str(raw.get("prompt_version") or base.prompt_version),
            temperature=float(raw.get("temperature", base.temperature)),
            max_tokens=int(raw.get("max_tokens", base.max_tokens)),
            timeout=float(raw.get("timeout", base.timeout)),
            capability=str(raw.get("capability") or base.capability),
            deterministic_fallback=bool(raw.get("deterministic_fallback", base.deterministic_fallback)),
        )
    return base


def _slugify(text: str) -> str:
    t = (text or "").lower().strip()
    t = t.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t[:200] or "produkt"


def build_product_facts(product: dict[str, Any]) -> ProductFacts:
    gallery = product.get("gallery_json") or []
    if isinstance(gallery, str):
        try:
            gallery = json.loads(gallery)
        except Exception:
            gallery = []
    if not isinstance(gallery, list):
        gallery = []
    norm_gallery = []
    for i, img in enumerate(gallery):
        if isinstance(img, dict):
            norm_gallery.append({
                "url": img.get("url") or img.get("source_url"),
                "local_path": img.get("local_path") or img.get("path"),
                "sha256": img.get("sha256"),
                "mime_type": img.get("mime_type"),
                "position": img.get("position", i),
            })
        elif isinstance(img, str) and img.strip():
            norm_gallery.append({"url": img.strip(), "position": i})
    attrs = product.get("attributes") or product.get("attributes_json") or []
    if isinstance(attrs, str):
        try: attrs = json.loads(attrs)
        except Exception: attrs = []
    variants = product.get("variants") or []
    if isinstance(variants, str):
        try: variants = json.loads(variants)
        except Exception: variants = []
    facts = ProductFacts(
        sku=str(product.get("sku") or "").strip(),
        title=str(product.get("title") or product.get("product_name") or "").strip(),
        brand=str(product.get("brand") or "").strip(),
        category=str(product.get("category") or product.get("category_name") or "").strip(),
        description=str(product.get("raw_description") or product.get("description") or "").strip(),
        price=float(product.get("sale_price") or product.get("retail_price") or product.get("price") or 0) or None,
        currency=str(product.get("currency") or "EUR"),
        ean=str(product.get("ean") or product.get("gtin") or product.get("barcode") or "").strip(),
        stock=int(float(product.get("stock_quantity") or product.get("stock") or 0)),
        images=norm_gallery,
        attributes=attrs if isinstance(attrs, list) else [],
        variants=variants if isinstance(variants, list) else [],
        logistics={k: product.get(k) for k in ["weight_net_kg","weight_gross_kg","length_cm","width_cm","height_cm","hs_code","country_of_origin","warehouse_location","bin_code","lead_time_days","reorder_level"] if product.get(k) not in (None, "", 0)},
        source_system=str(product.get("source_system") or "").strip(),
    )
    return facts


def deterministic_description(facts: ProductFacts) -> dict[str, Any]:
    parts = [facts.title or facts.sku]
    if facts.brand: parts.append(f"von {facts.brand}")
    if facts.category: parts.append(f"aus der Kategorie {facts.category}")
    base = " ".join([p for p in parts if p]).strip()
    desc = facts.description.strip() or f"{base}. Produktdetails basieren auf den gelieferten Stammdaten."
    bullets = []
    if facts.ean: bullets.append(f"EAN/GTIN: {facts.ean}")
    if facts.price is not None: bullets.append(f"Preis: {facts.price:.2f} {facts.currency}")
    bullets.append(f"Bestand: {facts.stock}")
    for a in (facts.attributes or [])[:6]:
        if isinstance(a, dict):
            n, v = a.get("name") or a.get("key"), a.get("value")
            if n and v: bullets.append(f"{n}: {v}")
    return {
        "title": (facts.title or facts.sku)[:120],
        "short_description": desc[:280],
        "description": desc[:4000],
        "bullets": bullets[:8],
        "facts_hash": facts_hash(facts),
        "model": "deterministic_fallback",
        "prompt_version": "fallback-v1",
        "hallucination_blocked": False,
    }


def deterministic_seo(facts: ProductFacts, channel: str) -> dict[str, Any]:
    title = (facts.title or facts.sku).strip()
    brand = facts.brand.strip()
    cat = facts.category.strip()
    seo_title = " ".join([x for x in [brand, title, cat] if x])[:70]
    meta = facts.description.strip() or f"{title} {('von ' + brand) if brand else ''} {('in ' + cat) if cat else ''}. Jetzt Details prüfen."
    meta = re.sub(r"\s+", " ", meta).strip()[:160]
    slug = _slugify(f"{brand}-{title}-{facts.sku}")
    keywords = [x for x in [brand, cat, facts.sku, title] if x][:10]
    return {
        "channel": channel,
        "seo_title": seo_title,
        "meta_description": meta,
        "slug": slug,
        "keywords": keywords,
        "facts_hash": facts_hash(facts),
        "model": "deterministic_fallback",
        "prompt_version": "fallback-v1",
        "hallucination_blocked": False,
    }


def deterministic_alt_texts(facts: ProductFacts) -> list[dict[str, Any]]:
    out = []
    base = " ".join([x for x in [facts.brand, facts.title or facts.sku, facts.category] if x]).strip()
    for i, img in enumerate(facts.images or []):
        alt = f"{base} - Ansicht {i+1}".strip(" -")[:125]
        out.append({"position": i, "sha256": img.get("sha256"), "local_path": img.get("local_path"), "alt_text": alt, "model": "deterministic_fallback"})
    return out


def _extract_json(content: str) -> dict[str, Any] | None:
    if not content:
        return None
    s = content.strip()
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {"value": obj}
    except Exception:
        pass
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {"value": obj}
        except Exception:
            return None
    return None


def validate_no_hallucinated_claims(output: dict[str, Any], facts: ProductFacts) -> tuple[bool, list[str]]:
    """Conservative guard: block obviously unsupported numeric/identifier claims."""
    warnings: list[str] = []
    text = canonical_json(output).lower()
    allowed_numbers = set(re.findall(r"\b\d+(?:[\.,]\d+)?\b", canonical_json(facts.to_dict()).lower()))
    for n in re.findall(r"\b\d+(?:[\.,]\d+)?\b", text):
        if n not in allowed_numbers and len(n) > 2:
            warnings.append(f"unverified_number:{n}")
    # hard block on common risky claim patterns
    blocked_patterns = [r"\bgarantie\b", r"\bzertifiziert\b", r"\bmedizinisch\b", r"\btestsieger\b"]
    for p in blocked_patterns:
        if re.search(p, text) and not re.search(p, canonical_json(facts.to_dict()).lower()):
            warnings.append(f"blocked_claim:{p}")
    hallucinated = any(w.startswith("blocked_claim:") for w in warnings)
    return (not hallucinated), warnings


async def run_task_json(
    task: str,
    *,
    facts: ProductFacts,
    prompt: str,
    runtime: Any = None,
    settings: dict[str, Any] | None = None,
) -> AITaskResult:
    profile = get_profile(task, settings)
    fh = facts_hash(facts)
    models = list(dict.fromkeys([*(profile.preferred_models or []), *(profile.fallback_models or []), "auto"]))
    client = getattr(runtime, "omniroute", None) if runtime is not None else None
    last_error = "no_model_available"
    for model in models:
        if client is None:
            last_error = "omniroute_not_configured"
            break
        try:
            res = await client.chat_completion(
                model=model,
                messages=[
                    {"role": "system", "content": "Return ONLY valid JSON. Do not invent facts not present in input. If unknown, omit or set null/empty."},
                    {"role": "user", "content": prompt},
                ],
                timeout=profile.timeout,
            )
            parsed = _extract_json(str(res.get("content") or ""))
            if not parsed:
                last_error = "invalid_json"
                continue
            ok, warns = validate_no_hallucinated_claims(parsed, facts)
            parsed["facts_hash"] = fh
            parsed["model"] = model
            parsed["prompt_version"] = profile.prompt_version
            parsed["hallucination_blocked"] = not ok
            parsed["validation_warnings"] = warns
            status = "ok" if ok else "blocked"
            return AITaskResult(task=task, status=status, output=parsed, model=model, prompt_version=profile.prompt_version, facts_hash=fh, warnings=warns, errors=[] if ok else ["hallucination_blocked"])
        except Exception as exc:
            last_error = str(exc)[:300]
            logger.warning("ai_task_failed task=%s model=%s error=%s", task, model, last_error)
            continue

    if profile.deterministic_fallback:
        if task == TASK_DESCRIPTION:
            out = deterministic_description(facts)
        elif task == TASK_SEO:
            out = deterministic_seo(facts, "default")
        elif task == TASK_VISION_ALT:
            out = {"alt_texts": deterministic_alt_texts(facts), "facts_hash": fh, "model": "deterministic_fallback", "prompt_version": "fallback-v1"}
        elif task == TASK_STYLE_SCORING:
            from .assortment_profile import deterministic_style_from_facts
            out = deterministic_style_from_facts(facts)
            out.update({"facts_hash": fh, "model": "deterministic_fallback", "prompt_version": "fallback-v1"})
        else:
            out = {"facts_hash": fh, "model": "deterministic_fallback", "prompt_version": "fallback-v1", "status": "fallback"}
        return AITaskResult(task=task, status="fallback", output=out, model="deterministic_fallback", prompt_version="fallback-v1", facts_hash=fh, warnings=[last_error], errors=[])
    return AITaskResult(task=task, status="error", output={"facts_hash": fh}, model="", prompt_version=profile.prompt_version, facts_hash=fh, warnings=[], errors=[last_error])
