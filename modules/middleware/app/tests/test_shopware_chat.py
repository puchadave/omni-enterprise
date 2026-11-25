import asyncio
import os

import pytest

os.environ["DATABASE_URL"] = ""


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "swchat.sqlite"
    import database
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)
    asyncio.run(database.init_db())
    yield database


async def _seed(temp_db):
    await temp_db.upsert_product_aggregate({"sku": "A1", "title": "A1", "supplier_price": 10, "sale_price": 20, "stock_quantity": 10, "tax_rate": 19, "status": "active"}, sync_rules={"mode": "full"})
    await temp_db.upsert_product_aggregate({"sku": "A2", "title": "A2", "supplier_price": 12, "sale_price": 30, "stock_quantity": 5, "tax_rate": 19, "status": "active"}, sync_rules={"mode": "full"})
    await temp_db.upsert_product_aggregate({"sku": "E1", "title": "Ebay Only", "supplier_price": 5, "sale_price": 15, "stock_quantity": 5, "tax_rate": 19, "status": "active"}, sync_rules={"mode": "full"})
    await temp_db.marketplace_dataset_upsert({"product_sku": "E1", "channel": "ebay", "status": "ready", "facts_hash": "x", "title": "Ebay", "description": "desc", "seo": {"seo_title": "t", "meta_description": "m"}, "images": []})


def test_channel_isolation_shopware_plan_does_not_touch_other_marketplaces(temp_db):
    from services.shopware_chat_service import plan_shopware_chat, execute_shopware_chat_plan

    asyncio.run(_seed(temp_db))
    plan = asyncio.run(plan_shopware_chat("Erstelle Best-Sales Angebot für A1", actor="tester", skus=["A1"]))
    assert plan["channel_scope"] == "shopware"
    assert plan["diff"]["other_marketplaces_touched"] == 0
    done = asyncio.run(execute_shopware_chat_plan(plan["plan_id"], actor="tester", idempotency_key=plan["idempotency_key"]))
    assert done["diff"]["other_marketplaces_touched"] == 0
    # ebay dataset must remain unchanged
    rows = asyncio.run(temp_db.marketplace_dataset_list(channel="ebay"))
    assert len(rows) == 1 and rows[0]["product_sku"] == "E1"
    # no shopware chat side effect on other channel dataset rows
    all_rows = asyncio.run(temp_db.marketplace_dataset_list(limit=100))
    assert all(str(r.get("channel")) in {"ebay", "shopware"} for r in all_rows)


def test_bundle_stock_tax_and_idempotency(temp_db):
    from services.shopware_chat_service import plan_shopware_chat, execute_shopware_chat_plan

    asyncio.run(_seed(temp_db))
    plan = asyncio.run(plan_shopware_chat("Erstelle Bundle aus A1 und A2", actor="tester", skus=["BND-TEST", "A1", "A2"]))
    b = plan["diff"]["bundle"]
    assert b["sku"].startswith("BND-")
    assert b["calculated_stock"] == 5  # min(floor(10/1), floor(5/1))
    assert plan["guardrails"]["bundle_validated"] is True
    done = asyncio.run(execute_shopware_chat_plan(plan["plan_id"], actor="tester", idempotency_key=plan["idempotency_key"]))
    assert done["status"] == "completed"
    done2 = asyncio.run(execute_shopware_chat_plan(plan["plan_id"], actor="tester", idempotency_key=plan["idempotency_key"]))
    assert done2["status"] == "completed"


def test_best_sales_margin_and_paangv_guardrails(temp_db):
    from services.shopware_chat_service import plan_shopware_chat

    asyncio.run(_seed(temp_db))
    plan = asyncio.run(plan_shopware_chat("Best-Sales Angebot für A1", actor="tester", skus=["A1"]))
    item = plan["diff"]["items"][0]
    assert item["type"] == "best_sales"
    assert "margin_pct" in item
    # without 30d history, no listPrice
    assert item["listPrice"] is None
    assert item["paangv_ok"] is False


def test_plan_execute_rollback(temp_db):
    from services.shopware_chat_service import plan_shopware_chat, execute_shopware_chat_plan, rollback_shopware_chat_plan

    asyncio.run(_seed(temp_db))
    plan = asyncio.run(plan_shopware_chat("Erstelle Bundle aus A1 und A2", actor="tester", skus=["BND-RB", "A1", "A2"]))
    done = asyncio.run(execute_shopware_chat_plan(plan["plan_id"], actor="tester", idempotency_key=plan["idempotency_key"]))
    assert done["status"] == "completed"
    rb = asyncio.run(rollback_shopware_chat_plan(plan["plan_id"], actor="tester"))
    assert rb["status"] == "rolled_back"


def test_shopware_channel_scope_constant():
    from services.shopware_chat_service import CHANNEL_SCOPE
    assert CHANNEL_SCOPE == "shopware"
