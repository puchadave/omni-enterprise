import asyncio

import pytest

from services.assortment_profile import (
    PROFILE_CLUBWEAR_SEXY,
    ai_style_score,
    classify_product,
    get_profile_name,
    profile_public,
)
from services.catalog_intelligence import merge_config, score_product, select_products
from services.catalog_validation import CatalogValidationService
from services.enrichment_contracts import ProductSource
from services.sales_release_orchestrator import _score_product


def _clubwear_product(**kw):
    p = {
        "sku": "CLUB-1",
        "title": "Sexy Bodycon Wetlook Mini Clubwear Party Dress",
        "category": "Clubwear",
        "raw_description": "Figurbetontes Bodycon-Minikleid mit Cut-Out und Mesh-Einsätzen für Club und Party.",
        "sale_price": 59.99,
        "purchase_price": 18.0,
        "stock_quantity": 25,
        "status": "active",
        "main_image_url": "https://example.com/a.jpg",
        "gallery_json": ["https://example.com/a.jpg"],
        "seo_title": "Bodycon Wetlook Clubwear Minikleid",
        "meta_description": "Bodycon Wetlook Minikleid für Party und Club.",
        "slug": "bodycon-wetlook-clubwear-minikleid",
        "ean": "4006381333931",
        "brand": "NightBrand",
        "manufacturer": "NightBrand",
        "updated_at": "2026-11-20T00:00:00+00:00",
    }
    p.update(kw)
    return p


def _evening_gown_product(**kw):
    p = {
        "sku": "EVEN-1",
        "title": "Elegantes langes Abendkleid Gala",
        "category": "Abendkleider",
        "raw_description": "Klassisches bodenlanges Abendkleid für Gala und Ball.",
        "sale_price": 499.0,
        "purchase_price": 90.0,
        "stock_quantity": 50,
        "status": "active",
        "main_image_url": "https://example.com/evening.jpg",
        "gallery_json": ["https://example.com/evening.jpg"],
        "ean": "4006381333931",
        "brand": "GalaBrand",
        "manufacturer": "GalaBrand",
        "updated_at": "2026-11-20T00:00:00+00:00",
    }
    p.update(kw)
    return p


def test_profile_defaults_to_clubwear_sexy():
    assert get_profile_name() == PROFILE_CLUBWEAR_SEXY
    public = profile_public()
    assert public["name"] == PROFILE_CLUBWEAR_SEXY
    assert any("abendkleid" in t for t in public["exclusion_terms"])
    assert any("bodycon" in t for t in public["positive_terms"])


def test_bodycon_wetlook_scores_high_and_has_tags():
    result = classify_product(_clubwear_product())
    assert result["excluded_eveningwear"] is False
    assert result["style_score"] >= 70
    assert "bodycon" in result["style_tags"]
    assert "wetlook" in result["style_tags"]
    assert "clubwear" in result["style_tags"] or "party" in result["style_tags"]


def test_evening_gown_is_hard_excluded():
    result = classify_product(_evening_gown_product())
    assert result["excluded_eveningwear"] is True
    assert result["is_formal_eveningwear"] is True
    assert result["style_score"] == 0.0
    assert "abendkleid" in result["exclusion_reason"]


def test_ballgown_english_is_hard_excluded():
    result = classify_product({"sku": "X", "title": "Prom Dress Ball Gown", "description": "Evening Gown for formal events"})
    assert result["excluded_eveningwear"] is True


def test_scoring_selects_clubwear_and_rejects_eveningwear_despite_margin():
    cfg = merge_config({"min_count": 1, "target_count": 2, "max_count": 2})
    club = score_product(_clubwear_product(), cfg)
    gown = score_product(_evening_gown_product(), cfg)

    assert club["eligible"] is True
    assert club["style_score"] >= 70
    assert "excluded_eveningwear" not in club["exclusions"]

    assert gown["eligible"] is False
    assert "excluded_eveningwear" in gown["exclusions"]
    assert gown["style_score"] == 0.0

    selection = select_products([club, gown], cfg)
    selected_skus = {s["sku"] for s in selection["selected"]}
    assert "CLUB-1" in selected_skus
    assert "EVEN-1" not in selected_skus
    assert selection["excluded_eveningwear_count"] == 1


def test_catalog_validation_excludes_eveningwear():
    svc = CatalogValidationService(
        required_fields=["sku", "name", "price", "tax", "manufacturer", "category"],
        whitelisted_categories=[],
        whitelisted_manufacturers=[],
    )
    product = ProductSource(
        sku="EVEN-2", name="Ballkleid Gala", description="Klassisches Ballkleid",
        price=399.0, tax=19.0, manufacturer="GalaBrand", category="Abendkleider",
        gtin="4006381333931", images=[], variants=[],
    )
    result = svc.validate(product)
    assert result.is_valid is False
    assert "ERR_EXCLUDED_EVENINGWEAR" in result.errors
    assert result.excluded_eveningwear is True


def test_sales_release_scoring_blocks_eveningwear():
    score, reasons = _score_product(_evening_gown_product())
    assert score == 0.0
    assert "excluded_eveningwear" in reasons
    club_score, club_reasons = _score_product(_clubwear_product())
    assert club_score > 0
    assert any(r.startswith("clubwear_fit") for r in club_reasons)


def test_ai_style_score_falls_back_without_runtime():
    result = asyncio.run(ai_style_score(_clubwear_product(), runtime=None))
    assert result["style_source"] == "deterministic"
    assert result["style_score"] >= 70


def test_assortment_profile_all_disables_filter():
    result = classify_product(_evening_gown_product(), profile="all")
    assert result["excluded_eveningwear"] is False


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    import os
    os.environ["DATABASE_URL"] = ""
    import database
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "assortment.sqlite")
    asyncio.run(database.init_db())
    return database


def test_shopware_chat_excludes_eveningwear_and_scores_clubwear(temp_db):
    from services.shopware_chat_service import plan_shopware_chat

    asyncio.run(temp_db.upsert_product_aggregate(
        {"sku": "CLUB-9", "title": "Bodycon Wetlook Clubwear Party Minikleid", "category": "Clubwear",
         "supplier_price": 15, "sale_price": 49.99, "stock_quantity": 10, "tax_rate": 19, "status": "active"},
        sync_rules={"mode": "full"},
    ))
    asyncio.run(temp_db.upsert_product_aggregate(
        {"sku": "EVEN-9", "title": "Langes Abendkleid Gala", "category": "Abendkleider",
         "supplier_price": 120, "sale_price": 599.0, "stock_quantity": 20, "tax_rate": 19, "status": "active"},
        sync_rules={"mode": "full"},
    ))

    plan = asyncio.run(plan_shopware_chat("Best-Sales Angebot für CLUB-9 und EVEN-9", skus=["CLUB-9", "EVEN-9"]))
    items = {i["sku"]: i for i in plan["diff"]["items"]}
    assert items["EVEN-9"]["excluded_eveningwear"] is True
    assert items["EVEN-9"]["blocked"] is True
    assert items["CLUB-9"]["style_score"] >= 70
    assert plan["guardrails"]["excluded_eveningwear_count"] >= 1
