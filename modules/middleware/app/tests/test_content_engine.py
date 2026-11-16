from __future__ import annotations

import pytest

from services.content_engine import generate_channel_draft, validate_platform_content


def test_validate_platform_content_title_too_long():
    content = {"title": "x" * 300, "description": "ok"}
    res = validate_platform_content("ebay", content)
    assert res["valid"] is False
    assert any("title_too_long" in i for i in res["issues"])


def test_validate_platform_content_html_not_allowed_for_google():
    content = {"title": "Produkt", "description": "<p>Beschreibung</p>"}
    res = validate_platform_content("google_shopping", content)
    assert res["valid"] is False
    assert "html_not_allowed" in res["issues"]


def test_validate_platform_content_unverified_claim_warns_but_valid():
    content = {"title": "Produkt", "description": "Dieses Produkt ist garantiert zertifiziert."}
    res = validate_platform_content("shopware", content)
    assert any(i.startswith("potential_unverified_claim") for i in res["issues"])
    assert res["valid"] is True


@pytest.mark.asyncio
async def test_generate_channel_draft_template_fallback_uses_facts_only():
    product = {
        "sku": "SKU-1",
        "title": "Clubwear Top",
        "brand": "UPTEMPO",
        "category": "Fashion",
        "color": "Black",
        "ean": "4006381333931",
        "raw_description": "Schwarzes Top aus Mesh.",
    }
    draft = await generate_channel_draft(product, "shopware", omniroute_client=None)
    assert draft["channel"] == "shopware"
    assert draft["generated_by"] == "template_fallback"
    assert draft["facts_used"]["sku"] == "SKU-1"
    assert draft["facts_used"]["ean"] == "4006381333931"
    assert "Clubwear Top" in draft["title"]
    assert draft["validation"]["valid"] is True
    assert draft["approval_status"] == "draft"
