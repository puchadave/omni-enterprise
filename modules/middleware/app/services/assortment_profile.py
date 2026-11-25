"""Configurable assortment & style-scoring profiles.

Default profile ``clubwear_sexy`` targets clubwear / nightlife / partywear while
strictly excluding classic evening wear (Abendkleid, Ballkleid, Galakleid,
Brautkleid, Evening Gown, Prom Dress, Formal Dress, ...).

The evaluation is two-staged:

1. ``classify_product`` - deterministic pre-filter + keyword/attribute scoring.
   Evening-wear matches are hard-excluded and can never enter the Shopware
   sales channel.
2. ``ai_style_score`` - optional AI refinement using a factual fashion/retail
   merchandising taxonomy. Any AI failure falls back to stage 1.

Profile selection is configurable via ``SHOPWARE_ASSORTMENT_PROFILE`` (default
``clubwear_sexy``) and can be overridden per request/workflow.
"""
from __future__ import annotations

import os
import re
from typing import Any

DEFAULT_PROFILE_NAME = "clubwear_sexy"

PROFILE_CLUBWEAR_SEXY = "clubwear_sexy"
PROFILE_ALL = "all"

# Positive taxonomy: normalised term -> style tag.
_CLUBWEAR_POSITIVE_TERMS: dict[str, str] = {
    # explicit clubwear / nightlife
    "clubwear": "clubwear",
    "club wear": "clubwear",
    "club outfit": "clubwear",
    "nightlife": "nightlife",
    "night out": "nightlife",
    "party": "party",
    "partywear": "party",
    "party wear": "party",
    "party outfit": "party",
    "rave": "rave",
    "ravewear": "rave",
    "rave wear": "rave",
    "festival": "festival",
    "festival outfit": "festival",
    # silhouettes / cuts
    "bodycon": "bodycon",
    "body con": "bodycon",
    "bodycon dress": "bodycon",
    "bandage dress": "bodycon",
    "figure hugging": "figure_hugging",
    "figure-hugging": "figure_hugging",
    "figurbetont": "figure_hugging",
    "korperbetont": "figure_hugging",
    "körperbetont": "figure_hugging",
    "tight fit": "figure_hugging",
    "mini": "mini",
    "mini dress": "mini",
    "minikleid": "mini",
    "mini kleid": "mini",
    "hot pants": "shorts",
    "playsuit": "playsuit",
    "jumpsuit": "jumpsuit",
    "bodysuit": "bodysuit",
    "catsuit": "catsuit",
    "cut out": "cutout",
    "cutout": "cutout",
    "cut outs": "cutout",
    "cut-outs": "cutout",
    "backless": "backless",
    "off shoulder": "off_shoulder",
    "off-shoulder": "off_shoulder",
    "bardot": "off_shoulder",
    "one shoulder": "off_shoulder",
    "halter neck": "halter",
    "halter": "halter",
    "plunge": "plunge",
    "deep v": "plunge",
    "corset": "corset",
    "corsage": "corset",
    "bustier": "corset",
    # materials / finishes
    "wetlook": "wetlook",
    "wet look": "wetlook",
    "wet-look": "wetlook",
    "faux leather": "faux_leather",
    "fake leather": "faux_leather",
    "lederoptik": "faux_leather",
    "leather look": "faux_leather",
    "latex": "latex",
    "pvc": "latex",
    "vinyl": "latex",
    "mesh": "mesh",
    "netz": "mesh",
    "fishnet": "mesh",
    "sheer": "sheer",
    "transparent": "sheer",
    "see through": "sheer",
    "sequin": "sequin",
    "pailletten": "sequin",
    "glitter": "glitter",
    "metallic": "metallic",
    # styling / positioning
    "sexy": "sexy",
    "seductive": "sexy",
    "sinnlich": "sexy",
    "lingerie": "lingerie",
    "bralette": "lingerie",
    "negligee": "lingerie",
    "nightdress": "lingerie",
}

_CLUBWEAR_TAG_WEIGHTS: dict[str, float] = {
    "clubwear": 20.0,
    "nightlife": 16.0,
    "party": 16.0,
    "rave": 18.0,
    "festival": 12.0,
    "bodycon": 25.0,
    "figure_hugging": 16.0,
    "mini": 15.0,
    "cutout": 15.0,
    "backless": 12.0,
    "off_shoulder": 9.0,
    "halter": 8.0,
    "plunge": 10.0,
    "corset": 12.0,
    "wetlook": 22.0,
    "faux_leather": 16.0,
    "latex": 18.0,
    "mesh": 14.0,
    "sheer": 11.0,
    "sequin": 7.0,
    "glitter": 7.0,
    "metallic": 6.0,
    "sexy": 12.0,
    "lingerie": 9.0,
    "shorts": 7.0,
    "playsuit": 9.0,
    "jumpsuit": 6.0,
    "bodysuit": 11.0,
    "catsuit": 13.0,
}

# Hard exclusion terms: classic evening / formal / bridal wear.
# Matched as substrings on normalised text (safe because terms are specific).
_CLUBWEAR_EXCLUSION_TERMS: list[str] = [
    # German
    "abendkleid", "abendkleider", "abendrobe", "abendroben", "abendgarderobe",
    "ballkleid", "ballkleider", "ballrobe",
    "galakleid", "galakleider", "gala robe", "gala kleid",
    "brautkleid", "brautkleider", "hochzeitskleid", "hochzeitskleider",
    "festkleid", "festkleider", "festmode", "festliche robe",
    "cocktailkleid", "cocktail kleid", "cocktail robe", "cocktailrobe",
    "businesskleid", "business kleid", "business robe", "businessrobe",
    "bodenlang", "bodenlange", "bodenlanges",
    # English
    "evening gown", "evening dress", "evening wear", "eveningwear",
    "ball gown", "ballgown", "gala gown", "gala dress",
    "prom dress", "prom gown", "formal dress", "formal gown", "formal wear",
    "wedding dress", "wedding gown", "bridal gown", "bridal dress",
    "maxi gown", "floor length gown", "floor-length gown",
    "cocktail dress", "cocktail gown", "business dress",
]

PROFILES: dict[str, dict[str, Any]] = {
    PROFILE_CLUBWEAR_SEXY: {
        "name": PROFILE_CLUBWEAR_SEXY,
        "label": "Clubwear & Nightlife",
        "description": "Clubwear, Partywear, Bodycon, Wetlook, Mesh und figurbetonte Nightlife-Styles; klassische Abend-/Ball-/Brautmode ist ausgeschlossen.",
        "positive_terms": dict(_CLUBWEAR_POSITIVE_TERMS),
        "tag_weights": dict(_CLUBWEAR_TAG_WEIGHTS),
        "exclusion_terms": list(_CLUBWEAR_EXCLUSION_TERMS),
        "base_score": 10.0,
        "min_style_score": 35.0,
        "exclusion_code": "excluded_eveningwear",
    },
    PROFILE_ALL: {
        "name": PROFILE_ALL,
        "label": "All assortments (no style filter)",
        "description": "Disables style targeting and evening-wear exclusion.",
        "positive_terms": {},
        "tag_weights": {},
        "exclusion_terms": [],
        "base_score": 50.0,
        "min_style_score": 0.0,
        "exclusion_code": "",
    },
}


def get_profile_name(name: str | None = None) -> str:
    raw = (name or os.getenv("SHOPWARE_ASSORTMENT_PROFILE") or DEFAULT_PROFILE_NAME).strip().lower()
    return raw if raw in PROFILES else DEFAULT_PROFILE_NAME


def get_profile(name: str | None = None) -> dict[str, Any]:
    return PROFILES[get_profile_name(name)]


def profile_public(name: str | None = None) -> dict[str, Any]:
    """Serialisable profile description for UI / API."""
    p = get_profile(name)
    return {
        "name": p["name"],
        "label": p["label"],
        "description": p["description"],
        "base_score": p["base_score"],
        "min_style_score": p["min_style_score"],
        "positive_terms": sorted(p["positive_terms"].keys()),
        "exclusion_terms": sorted(p["exclusion_terms"]),
        "tag_weights": dict(p["tag_weights"]),
    }


def _norm(value: Any) -> str:
    text = str(value or "").lower().replace("-", " ").replace("_", " ")
    return re.sub(r"\s+", " ", text).strip()


def _collect_text(product: dict[str, Any]) -> str:
    if not isinstance(product, dict):
        return _norm(product)
    keys = [
        "title", "name", "product_name", "category", "categories", "category_path",
        "description", "raw_description", "short_description", "long_description",
        "brand", "manufacturer", "keywords", "seo_title", "meta_description", "slug",
    ]
    parts: list[str] = []
    for key in keys:
        parts.append(_norm(product.get(key)))
    for key in ("ai_tags_json", "tags", "attributes", "variants"):
        value = product.get(key)
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, dict):
                    parts.append(_norm(item.get("name")))
                    parts.append(_norm(item.get("value")))
                    parts.append(_norm(item.get("key")))
                else:
                    parts.append(_norm(item))
        elif isinstance(value, dict):
            parts.extend(_norm(v) for v in value.values())
        elif value:
            parts.append(_norm(value))
    return " ".join(p for p in parts if p)


def _matches(text: str, term: str) -> bool:
    term = _norm(term)
    if not term:
        return False
    if " " in term:
        return term in text
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None


def _classify_text(text: str, profile: dict[str, Any]) -> dict[str, Any]:
    text = _norm(text)
    matched_exclusions = [t for t in profile.get("exclusion_terms", []) if _norm(t) in text]
    if matched_exclusions:
        return {
            "assortment_profile": profile["name"],
            "excluded_eveningwear": True,
            "is_formal_eveningwear": True,
            "exclusion_reason": matched_exclusions[0],
            "matched_exclusion_terms": matched_exclusions,
            "style_tags": [],
            "style_score": 0.0,
            "style_source": "deterministic",
        }
    tags = sorted({tag for term, tag in profile.get("positive_terms", {}).items() if _matches(text, term)})
    score = float(profile.get("base_score", 0.0) or 0.0)
    for tag in tags:
        score += float(profile.get("tag_weights", {}).get(tag, 6.0))
    if len(tags) >= 3:
        score += 5.0
    score = max(0.0, min(100.0, round(score, 2)))
    return {
        "assortment_profile": profile["name"],
        "excluded_eveningwear": False,
        "is_formal_eveningwear": False,
        "exclusion_reason": "",
        "matched_exclusion_terms": [],
        "style_tags": tags,
        "style_score": score,
        "style_source": "deterministic",
    }


def classify_product(product: dict[str, Any], profile: str | dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic stage-1 evaluation (hard exclusion + keyword scoring)."""
    prof = profile if isinstance(profile, dict) else get_profile(profile if isinstance(profile, str) else None)
    return _classify_text(_collect_text(product), prof)


def deterministic_style_from_facts(facts: Any, profile: str | dict[str, Any] | None = None) -> dict[str, Any]:
    """Fallback used by ``ai_task_routing`` when no AI runtime is available."""
    prof = profile if isinstance(profile, dict) else get_profile(profile if isinstance(profile, str) else None)
    data = facts.to_dict() if hasattr(facts, "to_dict") else (facts if isinstance(facts, dict) else {})
    return _classify_text(_collect_text(data), prof)


def _build_ai_prompt(facts: Any, profile: dict[str, Any]) -> str:
    facts_dict = facts.to_dict() if hasattr(facts, "to_dict") else facts
    positives = ", ".join(sorted(profile.get("positive_terms", {}).keys())[:60])
    exclusions = ", ".join(sorted(profile.get("exclusion_terms", []))[:60])
    return (
        "Du bist ein Mode-Retail-Merchandising-Klassifikator für ein Clubwear-/Nightlife-Sortiment. "
        "Bewerte ausschließlich anhand der gelieferten Produktfakten, ob der Artikel als Clubwear, "
        "Partywear oder figurbetonter Nightlife-Style für den Verkaufskanal geeignet ist. "
        "Nutze eine sachliche Fashion-/Retail-Taxonomie. Triff keine Aussagen über Personen, "
        "sondern nur über Produktmerkmale (Silhouette, Material, Anlass). "
        f"Positiv-Merkmale (Beispiele): {positives}. "
        f"Harte Ausschluss-Merkmale (klassische Abend-/Ball-/Braut-/Business-Mode): {exclusions}. "
        "Wenn ein Ausschluss-Merkmal zutrifft, setze is_formal_eveningwear=true und style_score=0. "
        "Antworte NUR mit gültigem JSON in exakt diesem Schema: "
        '{"style_score": <0-100>, "style_tags": [<strings>], "is_formal_eveningwear": <bool>, "reason": "<kurz>"}. '
        f"FACTS={facts_dict}"
    )


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _normalise_ai_tags(raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [x.strip() for x in re.split(r"[,;|]", raw)]
    if not isinstance(raw, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in raw:
        tag = _norm(item).replace(" ", "_")
        if tag and tag not in out:
            out.append(tag)
    return out


async def ai_style_score(
    product: dict[str, Any],
    profile: str | dict[str, Any] | None = None,
    runtime: Any = None,
) -> dict[str, Any]:
    """Two-stage evaluation: deterministic pre-filter, then AI style scoring."""
    prof = profile if isinstance(profile, dict) else get_profile(profile if isinstance(profile, str) else None)
    deterministic = classify_product(product, prof)
    if deterministic["excluded_eveningwear"] or runtime is None:
        deterministic["style_source"] = "deterministic"
        return deterministic

    from .ai_task_routing import TASK_STYLE_SCORING, build_product_facts, run_task_json

    facts = build_product_facts(product)
    try:
        result = await run_task_json(
            TASK_STYLE_SCORING,
            facts=facts,
            prompt=_build_ai_prompt(facts, prof),
            runtime=runtime,
        )
    except Exception:
        deterministic["style_source"] = "deterministic_fallback"
        return deterministic

    output = result.output if isinstance(result.output, dict) else {}
    ai_score = _safe_float(output.get("style_score"))
    ai_tags = _normalise_ai_tags(output.get("style_tags"))
    ai_formal = bool(output.get("is_formal_eveningwear"))
    excluded = deterministic["excluded_eveningwear"] or ai_formal
    tags = sorted(set(deterministic["style_tags"]) | set(ai_tags))
    score = 0.0 if excluded else max(float(deterministic["style_score"]), float(ai_score or 0.0))
    score = max(0.0, min(100.0, round(score, 2)))
    return {
        "assortment_profile": prof["name"],
        "excluded_eveningwear": excluded,
        "is_formal_eveningwear": excluded,
        "exclusion_reason": deterministic["exclusion_reason"] or ("formal_eveningwear_ai" if ai_formal else ""),
        "matched_exclusion_terms": deterministic["matched_exclusion_terms"],
        "style_tags": tags,
        "style_score": score,
        "style_source": "ai" if result.status == "ok" else "ai_fallback",
        "ai_model": result.model,
        "ai_reason": str(output.get("reason") or "")[:300],
    }


evaluate_assortment = ai_style_score
