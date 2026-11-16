from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import database
import main
from main import app


def _api_headers(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_KEY", "api-contract-test-key")
    return {"X-Internal-API-Key": "api-contract-test-key"}


def test_registered_routes_have_explicit_permissions():
    public_paths = {"/healthz", "/login", "/api/auth/login", "/api/auth/csrf"}
    unclassified = [
        (method, route.path)
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods - {"OPTIONS"}
        if route.path not in public_paths
        and main._required_permission(method, route.path) == "system.unclassified"
    ]

    assert unclassified == []


def test_product_pagination_and_status_api_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "products.sqlite")
    headers = _api_headers(monkeypatch)
    calls = []

    async def list_products(limit, offset):
        calls.append((limit, offset))
        return []

    async def upsert_product(**kwargs):
        calls.append(kwargs)
        return True

    async def upsert_product_aggregate(data, **kwargs):
        calls.append(data)
        return {"sku": data.get("sku")}

    async def refresh_bundle_stocks():
        return None

    monkeypatch.setattr(main, "list_products", list_products)
    monkeypatch.setattr(main, "upsert_product", upsert_product)
    monkeypatch.setattr(main, "upsert_product_aggregate", upsert_product_aggregate)
    monkeypatch.setattr(main, "refresh_all_bundle_stocks", refresh_bundle_stocks)

    with TestClient(app) as client:
        page = client.get("/api/products?limit=500&offset=0", headers=headers)
        assert page.status_code == 200
        assert calls.pop() == (500, 0)
        assert client.get("/api/products?limit=501", headers=headers).status_code == 422

        payload = {
            "sku": "SKU-1",
            "title": "Product",
            "category": "Category",
            "supplier_price": 12.5,
            "stock": 10,
            "raw_description": "",
            "status": "paused",
        }
        created = client.post("/api/products", json=payload, headers=headers)
        assert created.status_code == 201
        assert calls.pop()["status"] == "paused"

        mismatch = client.put(
            "/api/products/SKU-2",
            json=payload,
            headers=headers,
        )
        assert mismatch.status_code == 422

        marketing = client.post(
            "/api/marketing/products/SKU-1/generate-content",
            headers=headers,
        )
        assert marketing.status_code == 200
        assert marketing.json()["product_sku"] == "SKU-1"


def test_export_queue_limits_reject_out_of_range_values(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "export-queues.sqlite")
    headers = _api_headers(monkeypatch)

    with TestClient(app) as client:
        for endpoint in ("/api/google/export/queue", "/api/shopware/export/queue"):
            assert client.get(f"{endpoint}?limit=0", headers=headers).status_code == 422
            assert client.get(f"{endpoint}?limit=501", headers=headers).status_code == 422


def test_product_pages_request_only_the_supported_limit():
    root = Path(__file__).resolve().parents[1]
    for template in ("products.html", "inventory.html", "orders.html"):
        source = (root / "templates" / template).read_text(encoding="utf-8")
        assert "limit=500" in source
        assert "limit=1000" not in source


@pytest.mark.asyncio
async def test_product_status_is_persisted_and_can_be_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", "")
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "product-status.sqlite")
    await database.init_db()

    await database.upsert_product(
        sku="SKU-STATUS",
        title="Status product",
        category="",
        supplier_price=1.0,
        stock=1,
        source_hash="draft-source",
        status="draft",
    )
    assert (await database.fetch_product("SKU-STATUS"))["status"] == "draft"

    await database.upsert_product(
        sku="SKU-STATUS",
        title="Updated status product",
        category="",
        supplier_price=1.0,
        stock=1,
        source_hash="paused-source",
        status="paused",
    )
    assert (await database.fetch_product("SKU-STATUS"))["status"] == "paused"

    with pytest.raises(ValueError, match="Product status"):
        await database.upsert_product(
            sku="SKU-STATUS",
            title="Invalid status",
            category="",
            supplier_price=1.0,
            stock=1,
            source_hash="invalid-source",
            status="unknown",
        )
