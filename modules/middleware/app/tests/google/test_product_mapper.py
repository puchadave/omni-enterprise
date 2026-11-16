"""Tests for Google Product Mapper and Validator."""

from __future__ import annotations

import pytest

from services.google_product_mapper import (
    GoogleProductMapper,
    GoogleProductValidator,
    build_export_record,
    GOOGLE_PRODUCT_CATEGORIES,
)
from services.canonical_product_mapping import CanonicalProduct


# ── Mapper Tests ────────────────────────────────────────────────────────

def test_mapper_initialization():
    """Test mapper initialization."""
    mapper = GoogleProductMapper(country="DE", language="de", currency="EUR")
    assert mapper.country == "DE"
    assert mapper.language == "de"
    assert mapper.currency == "EUR"


def test_map_product_basic():
    """Test basic product mapping."""
    mapper = GoogleProductMapper(country="DE", language="de", currency="EUR")
    
    product = CanonicalProduct(
        supplier_id="SUPPLIER-A",
        supplier_product_id="TP001",
        sku="TEST001",
        product_name="Test Product",
        name_without_number="Test Product",
        description="A test product",
        brand="Test Brand",
        category="Clothing",
        category_id="CAT001",
        category_path="Clothing/Shirts",
        color="Red",
        supplier_price_netto=10.00,
        supplier_price_brutto=19.00,
        stock_total=10,
        stock_reserved=0,
        stock_available=10,
        variants=[{"variant_uid": "VAR001", "size": "M", "color": "Red"}],
        images=[{"url": "https://example.com/image.jpg"}],
        ean_codes=["4012345678901"],
        material="Cotton",
        size_table_text="S-M-L",
        processing_time=3,
        product_url="https://example.com/product/1",
    )
    
    result = mapper.map_product(product, shop_url="https://shop.de")
    
    assert result["offerId"] == "TEST001-DE"
    assert result["title"] == "Test Product"
    assert result["description"] == "A test product"
    assert result["link"] == "https://shop.de/product/TEST001"
    assert result["imageLink"] == "https://example.com/image.jpg"
    assert result["contentLanguage"] == "de"
    assert result["feedCountry"] == "DE"
    assert result["availability"] == "in stock"
    assert result["condition"] == "new"
    assert result["price"]["value"] == "10.00"
    assert result["price"]["currency"] == "EUR"
    assert result["brand"] == "Test Brand"
    assert result["gtin"] == "4012345678901"
    assert result["itemGroupId"] == "TP001"
    assert result["color"] == "Red"
    assert result["size"] == "M"  # From variant, takes priority over size_table_text
    assert result["gender"] == "unisex"


def test_map_product_with_ai_optimization():
    """Test product mapping with AI-optimized title and description."""
    mapper = GoogleProductMapper(country="DE", language="de", currency="EUR")
    
    product = CanonicalProduct(
        supplier_id="S", supplier_product_id="P1", sku="SKU1",
        product_name="Original Title", name_without_number="Original",
        description="Original desc", brand="Brand", category="Shoes",
        category_id="C1", category_path="Shoes", color="Blue",
        supplier_price_netto=20.00, supplier_price_brutto=23.80,
        stock_total=5, stock_reserved=0, stock_available=5,
        variants=[], images=[], ean_codes=["1234567890123"],
        material="Leather", size_table_text="39-45", processing_time=2,
        product_url="https://shop.de/p1",
    )
    
    result = mapper.map_product(
        product,
        shop_url="https://shop.de",
        ai_title="AI Optimierter Titel",
        ai_description="AI optimierte Beschreibung",
        ai_image_url="https://example.com/ai-image.jpg",
    )
    
    assert result["title"] == "AI Optimierter Titel"
    assert result["description"] == "AI optimierte Beschreibung"
    assert result["imageLink"] == "https://example.com/ai-image.jpg"


def test_map_product_out_of_stock():
    """Test product mapping with zero stock."""
    mapper = GoogleProductMapper()
    
    product = CanonicalProduct(
        supplier_id="S", supplier_product_id="P1", sku="SKU1",
        product_name="Product", name_without_number="Product",
        description="Desc", brand="Brand", category="Clothing",
        category_id="C1", category_path="Clothing", color="Red",
        supplier_price_netto=10.00, supplier_price_brutto=11.90,
        stock_total=0, stock_reserved=0, stock_available=0,
        variants=[], images=[], ean_codes=[],
        material=None, size_table_text=None, processing_time=None,
        product_url=None,
    )
    
    result = mapper.map_product(product)
    assert result["availability"] == "out of stock"


# ── Category Mapping Tests ─────────────────────────────────────────────

def test_category_mapping():
    """Test Google product category mapping."""
    mapper = GoogleProductMapper()
    
    assert mapper._map_category("Clothing") == "1734"
    assert mapper._map_category("Shoes") == "2582"
    assert mapper._map_category("Unknown Category") == "1664"  # Default
    assert mapper._map_category(None) == "1664"  # Default


# ── Validator Tests ────────────────────────────────────────────────────

def test_validator_initialization():
    """Test validator initialization."""
    validator = GoogleProductValidator(country="DE", language="de", currency="EUR")
    assert validator.country == "DE"


def test_validator_valid_product():
    """Test validation of a valid product."""
    validator = GoogleProductValidator()
    
    product = CanonicalProduct(
        supplier_id="S", supplier_product_id="P1", sku="SKU1",
        product_name="Product", name_without_number="Product",
        description="Desc", brand="Brand", category="Clothing",
        category_id="C1", category_path="Clothing", color="Red",
        supplier_price_netto=10.00, supplier_price_brutto=11.90,
        stock_total=5, stock_reserved=0, stock_available=5,
        variants=[], images=[{"url": "https://example.com/img.jpg"}],
        ean_codes=["1234567890123"], material=None,
        size_table_text=None, processing_time=None, product_url="https://shop.de/p1",
    )
    
    google_product = {
        "offerId": "SKU1-DE",
        "title": "Product",
        "description": "Desc",
        "link": "https://shop.de/p1",
        "imageLink": "https://example.com/img.jpg",
        "price": {"value": "10.00", "currency": "EUR"},
        "availability": "in stock",
        "condition": "new",
        "brand": "Brand",
        "googleProductCategory": "1734",
        "gtin": "1234567890123",
        "color": "Red",
        "size": "M",
    }
    
    result = validator.validate(product, google_product, shop_url="https://shop.de")
    
    assert result["valid"] is True
    assert result["errors"] == []
    assert result["score"] > 0


def test_validator_invalid_product():
    """Test validation of an invalid product."""
    validator = GoogleProductValidator()
    
    product = CanonicalProduct(
        supplier_id="S", supplier_product_id="P1", sku="SKU1",
        product_name="", name_without_number="", description="",
        brand=None, category=None, category_id=None, category_path=None,
        color=None, supplier_price_netto=10.00, supplier_price_brutto=11.90,
        stock_total=0, stock_reserved=0, stock_available=0,
        variants=[], images=[], ean_codes=[],
        material=None, size_table_text=None, processing_time=None,
        product_url=None,
    )
    
    google_product = {
        "offerId": "",
        "title": "",
        "description": "",
        "link": "",
        "imageLink": "",
        "price": {},
        "availability": "",
        "condition": "",
        "brand": "",
        "googleProductCategory": "",
    }
    
    result = validator.validate(product, google_product)
    
    assert result["valid"] is False
    assert len(result["errors"]) > 0
    assert result["score"] < 100


def test_validator_url_validation():
    """Test URL validation in validator."""
    validator = GoogleProductValidator()
    
    assert validator._is_valid_url("https://example.com/product/1") is True
    assert validator._is_valid_url("http://example.com") is True
    assert validator._is_valid_url("not-a-url") is False
    assert validator._is_valid_url("") is False


# ── Export Record Builder Tests ────────────────────────────────────────

def test_build_export_record():
    """Test export record builder."""
    from uuid import uuid4
    
    account_id = str(uuid4())
    product_sku = "TEST001"
    google_product = {
        "offerId": "TEST001-DE",
        "title": "Test Product",
        "price": {"value": "10.00", "currency": "EUR"},
    }
    validation_result = {
        "valid": True,
        "errors": [],
        "warnings": [],
        "score": 95,
    }
    
    record = build_export_record(account_id, product_sku, google_product, validation_result)
    
    assert record["account_id"] == account_id
    assert record["product_sku"] == product_sku
    assert record["offer_id"] == "TEST001-DE"
    assert record["status"] == "pending"
    assert record["validation_score"] == 95
    assert record["payload_json"] == google_product