"""Tests for Matterhorn Inventory Synchronization.

Tests the InventorySyncService for correct sync operations,
stock buffer application, and error handling.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from services.inventory_sync import InventorySyncService, InventorySyncResult
from services.canonical_product_mapping import CanonicalProduct, merge_product_keep_supplier_ids


# ── Inventory Sync Tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_all_basic():
    """Test basic sync all operation."""
    # Mock products in database
    db_products = {
        "TEST001": CanonicalProduct(
            supplier_id="test_supplier",
            supplier_product_id="TP001",
            sku="TEST001",
            product_name="Test Product",
            name_without_number="Test Product",
            description="A test product",
            brand="Test Brand",
            category="Test Category",
            category_id="CAT001",
            category_path="Test/Category",
            color=None,
            creation_date=None,
            supplier_price_netto=10.00,
            supplier_price_brutto=19.00,
            stock_total=5,
            stock_reserved=0,
            stock_available=5,
            variants=[],
            images=[],
            ean_codes=[],
            material=None,
            size_table_url=None,
            size_table_text=None,
            processing_time=None,
            product_url=None,
        ),
    }
    
    # Mock API client
    mock_cm = MagicMock()
    mock_client = MagicMock()
    mock_cm.retrieve_item = AsyncMock(return_value={"sku": "TEST001", "stock_total": 10})
    mock_cm.close = AsyncMock()
    
    service = InventorySyncService(mock_cm, db_products)
    result = await service.sync_all(force_full=False)
    
    assert result is not None
    assert hasattr(result, "products_synced")


@pytest.mark.asyncio
async def test_sync_single_product():
    """Test syncing a single product."""
    db_products = {
        "TEST001": CanonicalProduct(
            supplier_id="test_supplier",
            supplier_product_id="TP001",
            sku="TEST001",
            product_name="Test Product",
            name_without_number="Test Product",
            description="A test product",
            brand="Test Brand",
            category="Test Category",
            category_id="CAT001",
            category_path="Test/Category",
            color=None,
            creation_date=None,
            supplier_price_netto=10.00,
            supplier_price_brutto=19.00,
            stock_total=5,
            stock_reserved=0,
            stock_available=5,
            variants=[],
            images=[],
            ean_codes=[],
            material=None,
            size_table_url=None,
            size_table_text=None,
            processing_time=None,
            product_url=None,
        ),
    }
    
    mock_cm = MagicMock()
    mock_cm.retrieve_item = AsyncMock(return_value={"sku": "TEST001", "stock_total": 10})
    mock_cm.close = AsyncMock()
    
    service = InventorySyncService(mock_cm, db_products)
    result = await service.sync_product("TEST001")
    
    assert result is not None
    assert hasattr(result, "products_synced")


# ── Canonical Product Mapping Tests ────────────────────────────────────

def test_from_matterhorn_basic():
    """Test creating CanonicalProduct from Matterhorn data."""
    data = {
        "supplier_product_id": "TP001",
        "sku": "TEST001",
        "product_name": "Test Product",
        "name_without_number": "Test Product",
        "description": "A test product description",
        "brand": "Test Brand",
        "brand_id": "BR001",
        "category": "Test Category",
        "category_id": "CAT001",
        "category_path": "Test/Category",
        "color": "Red",
        "supplier_price_netto": "10.00",
        "supplier_price_brutto": "19.00",
        "currency": "EUR",
        "stock_total": "5",
        "stock_reserved": "0",
        "stock_available": "5",
        "variants": [{"variant_uid": "VAR001", "size": "M", "ean": "4012345678901"}],
        "material": "Cotton",
        "size_table_text": "S-M-L",
        "max_processing_time": "3",
        "product_url": "https://example.com/product/1",
    }
    
    product = CanonicalProduct.from_matterhorn("TEST-SUPPLIER", data)
    
    assert product.supplier_id == "TEST-SUPPLIER"
    assert product.supplier_product_id == "TP001"
    assert product.sku == "TEST001"
    assert product.product_name == "Test Product"
    assert product.brand == "Test Brand"
    assert product.brand_id == "BR001"
    assert product.category == "Test Category"
    assert product.category_id == "CAT001"
    assert product.color == "Red"
    assert product.stock_total == 5
    assert len(product.variants) == 1
    assert product.variants[0]["variant_uid"] == "VAR001"
    assert product.ean_codes == ["4012345678901"]
    assert product.material == "Cotton"
    assert product.size_table_text == "S-M-L"
    assert product.processing_time == 3


def test_merge_product_keep_supplier_ids():
    """Test merging products while preserving supplier IDs."""
    existing = CanonicalProduct(
        supplier_id="SUPPLIER-A",
        supplier_product_id="ORIG-001",
        sku="EXISTING",
        product_name="Existing Product",
        name_without_number="Existing",
        description="Existing description",
        brand="Existing Brand",
        category="Existing Category",
        category_id="CAT-EXISTING",
        category_path="Existing/Category",
        color="Blue",
        supplier_price_netto=15.00,
        supplier_price_brutto=25.00,
        stock_total=10,
        stock_reserved=2,
        stock_available=8,
        variants=[{"variant_uid": "VAR-EXISTING", "size": "L"}],
        images=[],
        ean_codes=["012345678901"],
        material=" Cotton",
        size_table_text="L-XL",
        processing_time=5,
        product_url="https://example.com/existing",
    )
    
    incoming = CanonicalProduct(
        supplier_id="",  # Empty - should not overwrite existing
        supplier_product_id="NEW-001",  # Should be preserved from existing
        sku="NEW",
        product_name="New Product",
        name_without_number="New",
        description="New description",
        brand="New Brand",
        category="New Category",
        category_id="CAT-NEW",
        category_path="New/Category",
        color="Red",
        supplier_price_netto=20.00,
        supplier_price_brutto=30.00,
        stock_total=20,
        stock_reserved=5,
        stock_available=15,
        variants=[{"variant_uid": "VAR-NEW", "size": "M"}],
        images=[],
        ean_codes=["999888777666"],
        material="Polyester",
        size_table_text="S-M-L",
        processing_time=3,
        product_url="https://example.com/new",
    )
    
    merged = merge_product_keep_supplier_ids(existing, incoming)
    
    # Supplier IDs should be preserved from existing
    assert merged.supplier_id == "SUPPLIER-A"
    assert merged.supplier_product_id == "ORIG-001"
    
    # Other fields should be updated from incoming where they exist
    assert merged.product_name == "New Product"  # Updated
    assert merged.description == "New description"  # Updated
    assert merged.stock_total == 20  # Updated
    # New variant should be added (appended to end)
    assert any(v["variant_uid"] == "VAR-NEW" for v in merged.variants)
    assert merged.variants[0]["variant_uid"] == "VAR-EXISTING"  # Existing preserved
    
    # EAN codes should be merged without duplicates
    assert "012345678901" in merged.ean_codes
    assert "999888777666" in merged.ean_codes