import asyncio
import os

import pytest

os.environ["DATABASE_URL"] = ""


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "enrich.sqlite"
    import database
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)
    asyncio.run(database.init_db())
    yield database


def test_marketplace_enrichment_creates_channel_datasets(temp_db):
    from database import upsert_product_aggregate, marketplace_dataset_list
    from services.marketplace_enrichment_pipeline import enrich_products_for_channels

    asyncio.run(upsert_product_aggregate({
        "sku": "ENR-1",
        "title": "Enrichment Produkt",
        "raw_description": "Kurze Beschreibung für Testprodukt.",
        "brand": "BrandX",
        "category": "Test",
        "supplier_price": 10.0,
        "sale_price": 20.0,
        "stock_quantity": 5,
        "main_image_url": "https://example.com/a.jpg",
        "gallery_json": [{"url": "https://example.com/a.jpg", "local_path": "/tmp/a.jpg", "sha256": "abc"}],
    }, sync_rules={"mode": "full"}))

    res = asyncio.run(enrich_products_for_channels(skus=["ENR-1"], channels=["shopware", "ebay"], runtime=None))
    assert res["status"] == "ok"
    assert res["summary"]["total"] == 2
    rows = asyncio.run(marketplace_dataset_list(sku="ENR-1"))
    assert len(rows) == 2
    statuses = {r["channel"]: r["status"] for r in rows}
    assert statuses["shopware"] in {"ready", "incomplete", "blocked"}
    assert statuses["ebay"] in {"ready", "incomplete", "blocked"}
    for r in rows:
        assert r["facts_hash"]
        assert r["seo_json"] is not None
