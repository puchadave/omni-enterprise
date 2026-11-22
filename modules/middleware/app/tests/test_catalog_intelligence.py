import pytest
from services.catalog_intelligence import merge_config, score_product, select_products, build_report

def mk(i, **kw):
    p = {
        "sku": f"SKU-{i:03d}", "title": f"Product {i}", "category": kw.get("category", f"Cat-{i%5}"),
        "sale_price": 100 + i, "purchase_price": 60, "stock_quantity": 50, "status": "active",
        "main_image_url": "https://example.com/img.jpg", "raw_description": "Good description " * 20,
        "seo_title": "SEO", "meta_description": "Meta", "slug": f"p-{i}", "ai_tags_json": ["a","b"],
        "brand":"Brand", "manufacturer":"Mfg", "ean":"123", "updated_at":"2026-11-20T00:00:00+00:00",
    }
    p.update(kw)
    return p


def test_scoring_weights_normalized_and_score_range():
    cfg = merge_config({"weights": {"data_quality": 30, "content_quality": 20, "media_quality": 10, "price_margin": 10, "availability": 10, "category_relevance": 5, "cross_selling": 5, "seo_potential": 5, "freshness": 5}})
    s = score_product(mk(1), cfg)
    assert 0 <= s["score"] <= 100
    assert abs(sum(s["weights"].values()) - 100.0) < 0.01
    assert s["eligible"] is True


def test_exclusions_missing_fields():
    cfg = merge_config({})
    s = score_product(mk(2, title="", sale_price=0, main_image_url="", gallery_json=[]), cfg)
    assert "missing_name" in s["exclusions"]
    assert "missing_price" in s["exclusions"]
    assert "missing_images" in s["exclusions"]
    assert s["eligible"] is False


def test_selection_respects_min_max_and_diversity():
    products = [mk(i, category=f"Cat-{i%3}") for i in range(120)]
    scored = [score_product(p, merge_config({})) for p in products]
    cfg = merge_config({"target_count": 60, "min_count": 50, "max_count": 60, "category_max_share": 0.4})
    sel = select_products(scored, cfg)
    assert sel["selected_count"] <= 60
    assert sel["selected_count"] >= 50
    for c, n in sel["category_distribution"].items():
        assert n <= 24 + 5  # cap with relaxation tolerance


def test_insufficient_does_not_lower_quality():
    products = [mk(i, sale_price=0, main_image_url="", gallery_json=[]) for i in range(10)]
    scored = [score_product(p, merge_config({})) for p in products]
    cfg = merge_config({"min_count": 5, "target_count": 5, "max_count": 10})
    sel = select_products(scored, cfg)
    assert sel["insufficient"] is True
    assert sel["selected_count"] == 0
    assert "not lowered" in sel["message"].lower()


def test_build_report_summary():
    cfg = merge_config({})
    scored = [score_product(mk(1), cfg), score_product(mk(2, sale_price=0), cfg)]
    sel = select_products(scored, cfg)
    report = build_report({"id":"r1"}, scored, sel, cfg)
    assert report["summary"]["total_products"] == 2
    assert report["summary"]["excluded_products"] == 1
    assert "summary" in report
