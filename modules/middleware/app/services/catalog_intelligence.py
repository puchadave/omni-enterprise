"""Product Intelligence Engine - Scoring, Selection, Bundle Candidates, Dry-Run Reports.

Slice 1: OMNI-CORE DB as source of truth. No external writes. Configurable scoring.
"""
from __future__ import annotations

import json, math, uuid, hashlib
from datetime import datetime, timezone
from typing import Any

DEFAULT_WEIGHTS = {
    "data_quality": 15,
    "content_quality": 10,
    "media_quality": 10,
    "price_margin": 15,
    "availability": 15,
    "category_relevance": 10,
    "cross_selling": 10,
    "seo_potential": 10,
    "freshness": 5,
}
DEFAULT_CONFIG = {
    "weights": DEFAULT_WEIGHTS.copy(),
    "min_score": 0,
    "target_count": 600,
    "min_count": 500,
    "max_count": 750,
    "mode": "automatic",  # automatic|quality|expansion
    "category_min_share": 0.0,
    "category_max_share": 0.35,
    "min_images": 1,
    "require_price": True,
    "require_name": True,
    "require_stock": False,
    "exclude_no_images": True,
    "exclude_no_price": True,
    "exclude_no_name": True,
    "exclude_inactive": True,
    "exclude_duplicates": True,
}

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))

def _safe_float(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or v == "": return d
        return float(v)
    except Exception:
        return d

def _text_len(p: dict, *keys: str) -> int:
    return sum(len(str(p.get(k) or "")) for k in keys)

def _has_img(p: dict) -> bool:
    if str(p.get("main_image_url") or "").strip(): return True
    g = p.get("gallery_json") or []
    if isinstance(g, str):
        try: g = json.loads(g)
        except Exception: g = []
    return isinstance(g, list) and len(g) > 0

def _norm_weights(weights: dict[str, Any]) -> dict[str, float]:
    w = {k: _safe_float(weights.get(k, DEFAULT_WEIGHTS.get(k, 0))) for k in DEFAULT_WEIGHTS}
    for k in w:
        if w[k] < 0: w[k] = 0.0
    total = sum(w.values()) or 1.0
    return {k: (v / total * 100.0) for k, v in w.items()}

def merge_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    if not cfg: return out
    for k, v in cfg.items():
        if k == "weights" and isinstance(v, dict):
            out["weights"].update(v)
        elif k in out:
            out[k] = v
    out["weights"] = _norm_weights(out["weights"])
    out["target_count"] = int(max(1, min(750, _safe_float(out.get("target_count"), 600))))
    out["min_count"] = int(max(1, min(out["target_count"], _safe_float(out.get("min_count"), 500))))
    out["max_count"] = int(max(out["target_count"], min(750, _safe_float(out.get("max_count"), 750))))
    out["min_score"] = _clamp(_safe_float(out.get("min_score"), 0))
    out["category_max_share"] = max(0.05, min(1.0, _safe_float(out.get("category_max_share"), 0.35)))
    out["category_min_share"] = max(0.0, min(out["category_max_share"], _safe_float(out.get("category_min_share"), 0.0)))
    return out

def score_product(p: dict[str, Any], cfg: dict[str, Any], category_counts: dict[str, int] | None = None) -> dict[str, Any]:
    cfg = merge_config(cfg)
    weights = cfg["weights"]
    reasons: list[str] = []
    exclusions: list[str] = []

    title = str(p.get("title") or p.get("ai_title") or "").strip()
    sku = str(p.get("sku") or "").strip()
    price = _safe_float(p.get("sale_price") or p.get("price"), 0)
    purchase = _safe_float(p.get("purchase_price") or p.get("supplier_price"), 0)
    stock = _safe_float(p.get("available_quantity"), None)
    if stock is None:
        stock = _safe_float(p.get("stock_quantity"), _safe_float(p.get("stock"), 0))
    stock = max(0.0, stock)
    status = str(p.get("status") or "active").lower()
    cat = str(p.get("category") or "uncategorized").strip() or "uncategorized"
    has_img = _has_img(p)
    desc_len = _text_len(p, "raw_description", "short_description", "ai_description_html")
    seo_len = _text_len(p, "seo_title", "meta_description", "slug")
    tags = p.get("ai_tags_json") or []
    if isinstance(tags, str):
        try: tags = json.loads(tags)
        except Exception: tags = []

    # Exclusions
    if cfg["exclude_no_name"] and not title: exclusions.append("missing_name")
    if cfg["exclude_no_price"] and price <= 0: exclusions.append("missing_price")
    if not sku: exclusions.append("missing_sku")
    if cfg["exclude_no_images"] and not has_img: exclusions.append("missing_images")
    if cfg["exclude_inactive"] and status not in ("active", ""): exclusions.append(f"status_{status}")
    if cfg["require_stock"] and stock <= 0: exclusions.append("out_of_stock")
    # duplicate marker - caller can set
    if cfg["exclude_duplicates"] and p.get("_is_duplicate"): exclusions.append("duplicate")

    # Criteria 0-100
    dq = 0
    for f in ["sku","title","category","brand","manufacturer","ean","raw_description","main_image_url"]:
        if str(p.get(f) or "").strip(): dq += 12.5
    dq = _clamp(dq)
    content = _clamp((min(desc_len, 1200) / 1200 * 70) + (min(len(tags), 10) / 10 * 30))
    media = 0.0
    if has_img: media += 60
    g = p.get("gallery_json") or []
    if isinstance(g, str):
        try: g = json.loads(g)
        except Exception: g = []
    if isinstance(g, list): media += min(len(g), 5) * 8
    media = _clamp(media)
    margin = 0.0
    if price > 0 and purchase >= 0 and price >= purchase:
        margin_pct = (price - purchase) / price
        margin = _clamp(margin_pct * 200)  # 50% margin => 100
    elif price > 0:
        margin = 30.0
    availability = _clamp(100 if stock > 20 else stock * 5)
    cat_rel = 70.0
    if category_counts and cat:
        # slight boost to underrepresented categories in expansion mode
        total = sum(category_counts.values()) or 1
        share = category_counts.get(cat, 0) / total
        if cfg["mode"] == "expansion":
            cat_rel = _clamp(100 - share * 100)
        else:
            cat_rel = _clamp(80 - abs(share - 0.1) * 100)
    cross = 40.0
    if cat and cat != "uncategorized": cross += 20
    if _safe_float(p.get("weight_gross_kg"), 0) > 0: cross += 10
    if str(p.get("brand") or "").strip(): cross += 10
    if tags: cross += min(len(tags), 4) * 5
    cross = _clamp(cross)
    seo = _clamp((min(seo_len, 220) / 220 * 60) + (20 if str(p.get("slug") or "").strip() else 0) + (20 if tags else 0))
    freshness = 50.0
    upd = str(p.get("updated_at") or "")
    if upd:
        try:
            dt = datetime.fromisoformat(upd.replace("Z", "+00:00"))
            days = max(0, (datetime.now(timezone.utc) - dt.replace(tzinfo=dt.tzinfo or timezone.utc)).days)
            freshness = _clamp(100 - min(days, 365) / 365 * 100)
        except Exception:
            pass

    criteria = {
        "data_quality": round(dq,2), "content_quality": round(content,2), "media_quality": round(media,2),
        "price_margin": round(margin,2), "availability": round(availability,2), "category_relevance": round(cat_rel,2),
        "cross_selling": round(cross,2), "seo_potential": round(seo,2), "freshness": round(freshness,2),
    }
    total = sum(criteria[k] * weights.get(k, 0) / 100.0 for k in DEFAULT_WEIGHTS)
    total = round(_clamp(total), 2)

    if total >= 80: reasons.append("high_overall_score")
    if margin >= 60: reasons.append("strong_margin_potential")
    if availability >= 80: reasons.append("good_availability")
    if media >= 60: reasons.append("media_ready")
    if content >= 60: reasons.append("content_ready")
    if exclusions: reasons.append("excluded")

    return {
        "sku": sku, "title": title, "category": cat, "status": status,
        "score": total, "eligible": len(exclusions) == 0 and total >= cfg["min_score"],
        "exclusions": exclusions, "reasons": reasons, "criteria": criteria, "weights": weights,
    }

def select_products(scored: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, Any]:
    cfg = merge_config(cfg)
    eligible = [s for s in scored if s.get("eligible")]
    ineligible = [s for s in scored if not s.get("eligible")]
    mode = cfg["mode"]
    target = cfg["target_count"]
    min_c, max_c = cfg["min_count"], cfg["max_count"]
    cat_max_share = cfg["category_max_share"]

    # sort
    if mode == "quality":
        eligible.sort(key=lambda x: (-x["score"], x.get("sku","")))
        # stricter minimum score floor for quality mode
        qfloor = max(cfg["min_score"], 65)
        eligible = [s for s in eligible if s["score"] >= qfloor]
    elif mode == "expansion":
        eligible.sort(key=lambda x: (x.get("category",""), -x["score"]))
        # round-robin by category for breadth
        by_cat: dict[str, list[dict]] = {}
        for s in eligible:
            by_cat.setdefault(s.get("category") or "uncategorized", []).append(s)
        rr = []
        cats = sorted(by_cat.keys(), key=lambda c: len(by_cat[c]))
        idx = 0
        while len(rr) < len(eligible):
            added = False
            for c in cats:
                lst = by_cat[c]
                if idx < len(lst):
                    rr.append(lst[idx]); added = True
            if not added: break
            idx += 1
        eligible = rr
    else: # automatic - score first, diversify by cap
        eligible.sort(key=lambda x: (-x["score"], x.get("sku","")))

    selected: list[dict[str, Any]] = []
    cat_counts: dict[str, int] = {}
    deferred: list[dict[str, Any]] = []
    cap = max(1, math.ceil(max_c * cat_max_share))
    for s in eligible:
        if len(selected) >= max_c: break
        c = s.get("category") or "uncategorized"
        if cat_counts.get(c, 0) >= cap:
            deferred.append(s); continue
        selected.append(s)
        cat_counts[c] = cat_counts.get(c, 0) + 1
    # fill from deferred if under target and we can relax cap
    if len(selected) < target:
        for s in deferred:
            if len(selected) >= max_c: break
            selected.append(s)
            c = s.get("category") or "uncategorized"
            cat_counts[c] = cat_counts.get(c, 0) + 1

    # trim to max
    selected = selected[:max_c]
    target_met = len(selected) >= min_c
    insufficient = len(selected) < min_c

    # reasons/report
    excl_counts: dict[str, int] = {}
    for s in ineligible:
        for e in s.get("exclusions", []) or ["low_score"]:
            excl_counts[e] = excl_counts.get(e, 0) + 1

    return {
        "selected": selected,
        "selected_count": len(selected),
        "eligible_count": len(eligible),
        "ineligible_count": len(ineligible),
        "target_met": target_met,
        "insufficient": insufficient,
        "min_count": min_c, "target_count": target, "max_count": max_c,
        "category_distribution": cat_counts,
        "exclusion_counts": excl_counts,
        "message": (
            f"Selection has {len(selected)} products; below minimum {min_c}. Quality thresholds were not lowered."
            if insufficient else f"Selected {len(selected)} products."
        ),
    }

def build_report(run: dict[str, Any], scored: list[dict[str, Any]], selection: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    scores = [s["score"] for s in scored]
    avg = round(sum(scores) / len(scores), 2) if scores else 0
    return {
        "run": run,
        "config": cfg,
        "summary": {
            "total_products": len(scored),
            "eligible_products": selection["eligible_count"],
            "excluded_products": selection["ineligible_count"],
            "selected_products": selection["selected_count"],
            "average_score": avg,
            "target_met": selection["target_met"],
            "insufficient": selection["insufficient"],
        },
        "category_distribution": selection["category_distribution"],
        "exclusion_counts": selection["exclusion_counts"],
        "top_selected": [{"sku": s["sku"], "title": s["title"], "score": s["score"], "category": s["category"]} for s in selection["selected"][:20]],
        "message": selection["message"],
    }
