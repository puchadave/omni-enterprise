import asyncio

from services.ai_task_routing import (
    build_product_facts,
    facts_hash,
    deterministic_seo,
    validate_no_hallucinated_claims,
    run_task_json,
    TASK_DESCRIPTION,
)


def test_facts_hash_stable_and_changes_on_relevant_fact():
    p = {"sku": "A1", "title": "Shirt", "brand": "X", "category": "Apparel", "sale_price": 10, "stock_quantity": 2}
    f1 = build_product_facts(p)
    f2 = build_product_facts({**p, "stock_quantity": 2})
    f3 = build_product_facts({**p, "title": "Shirt XL"})
    assert facts_hash(f1) == facts_hash(f2)
    assert facts_hash(f1) != facts_hash(f3)


def test_hallucination_guard_blocks_unsupported_claims():
    facts = build_product_facts({"sku": "A1", "title": "Shirt", "description": "Baumwolle", "sale_price": 10, "stock_quantity": 1})
    ok, warns = validate_no_hallucinated_claims({"description": "Medizinisch zertifiziert und Testsieger."}, facts)
    assert ok is False
    assert any(w.startswith("blocked_claim:") for w in warns)


def test_deterministic_fallback_when_no_runtime():
    facts = build_product_facts({"sku": "A1", "title": "Shirt", "brand": "Brand", "category": "Cat", "description": "Desc", "sale_price": 10, "stock_quantity": 1})
    res = asyncio.run(run_task_json(TASK_DESCRIPTION, facts=facts, prompt="test", runtime=None))
    assert res.status == "fallback"
    assert res.output.get("facts_hash") == facts_hash(facts)
    assert res.output.get("title")


def test_deterministic_seo_channel_specific():
    facts = build_product_facts({"sku": "A1", "title": "Shirt", "brand": "Brand", "category": "Cat", "description": "Desc", "sale_price": 10, "stock_quantity": 1})
    seo = deterministic_seo(facts, "ebay")
    assert seo["channel"] == "ebay"
    assert seo["seo_title"]
    assert seo["meta_description"]
    assert seo["facts_hash"] == facts_hash(facts)
