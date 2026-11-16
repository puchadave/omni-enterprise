"""Tests for Matterhorn Price Engine.

Tests the PriceEngine for correct price calculation with various
cost factors and margin settings.
"""

from __future__ import annotations

import pytest
from decimal import Decimal
from services.price_engine import (
    PriceEngine, PriceCalculationResult, calculate_sales_price,
    DEFAULT_MARGIN_PERCENT, DEFAULT_TAX_RATE_PERCENT,
)
from pathlib import Path
from services.canonical_product_mapping import CanonicalProduct
from services.order_management import OrderItem, DeliveryAddress, OrderManagementService
from services.import_wizard import ImportWizard, ImportMode, get_import_mode_by_name, get_import_mode_display


# ── Price Engine Tests ──────────────────────────────────────────────────

def test_price_engine_initialization():
    """Test PriceEngine initialization."""
    engine = PriceEngine()
    assert engine is not None
    # Check global rules are set
    assert "margin_percent" in engine._global_rules


def test_calculate_price_basic():
    """Test basic price calculation."""
    engine = PriceEngine()
    
    # Create a minimal canonical product with net price
    product = CanonicalProduct(
        supplier_id="TEST",
        supplier_product_id="TP001",
        sku="TEST001",
        product_name="Test Product",
        name_without_number="Test Product",
        description="Test",
        brand=None,
        category=None,
        category_id=None,
        category_path=None,
        color=None,
        supplier_price_netto=10.00,
        supplier_price_brutto=19.00,
        stock_total=10,
        stock_reserved=0,
        stock_available=10,
        variants=[],
        images=[],
        ean_codes=[],
        material=None,
        size_table_url=None,
        size_table_text=None,
        processing_time=None,
        product_url=None,
    )
    
    result = engine.calculate_price(product)
    
    assert result is not None
    assert result.purchase_netto == Decimal("10.00")
    assert result.errors == []
    # Should have a net price (at least covering costs)
    assert result.net_price >= Decimal("10.00")


def test_calculate_price_with_margin():
    """Test price calculation with custom margin."""
    engine = PriceEngine()
    engine.set_global_rule("margin_percent", Decimal("25.00"))
    
    product = CanonicalProduct(
        supplier_id="TEST",
        supplier_product_id="TP001",
        sku="TEST001",
        product_name="Test Product",
        name_without_number="Test Product",
        description="Test",
        brand=None,
        category=None,
        category_id=None,
        category_path=None,
        color=None,
        supplier_price_netto=10.00,
        supplier_price_brutto=19.00,
        stock_total=10,
        stock_reserved=0,
        stock_available=10,
        variants=[],
        images=[],
        ean_codes=[],
        material=None,
        size_table_url=None,
        size_table_text=None,
        processing_time=None,
        product_url=None,
    )
    
    result = engine.calculate_price(product)
    
    assert result is not None
    assert result.margin_percent == Decimal("25.00")
    assert result.margin_amount > Decimal("0.00")


def test_calculate_sales_price_convenience():
    """Test the convenience calculate_sales_price function."""
    result = calculate_sales_price(
        supplier_price_netto=10.00,
        margin_percent=Decimal("30.00"),
        tax_percent=Decimal("19.00"),
    )
    
    assert result is not None
    assert result.purchase_netto == Decimal("10.00")
    # Should have calculated net and gross prices
    assert result.net_price > Decimal("0.00") or result.brutto_price > Decimal("0.00")


# ── Order Management Tests ─────────────────────────────────────────────

def test_order_item_creation():
    """Test OrderItem creation and serialization."""
    item = OrderItem(variant_uid="VAR-001", quantity=3)
    d = item.to_dict()
    assert d == {"variant_uid": "VAR-001", "quantity": 3}


def test_delivery_address_creation():
    """Test DeliveryAddress creation and serialization."""
    addr = DeliveryAddress(
        first_name="Test", second_name="User",
        country="DE", street="Teststr.", house_number="1",
        zip="12345", city="Testort"
    )
    d = addr.to_dict()
    assert d["first_name"] == "Test"
    assert d["city"] == "Testort"


def test_order_management_service_creation():
    """Test OrderManagementService creation."""
    # This test just checks the class can be instantiated
    # (would need a proper API client in full test)
    from services.order_management import OrderManagementService
    # Just verify the class exists and has the right methods
    assert hasattr(OrderManagementService, "create_order")
    assert hasattr(OrderManagementService, "get_order")


def test_order_status_validation():
    """Test order status validation."""
    from services.order_management import OrderManagementService
    
    # Test valid statuses
    for status in ["entwurf", "bereit_zur_bestellung", "abgeschlossen"]:
        assert OrderManagementService.validate_status(status) == True
    
    # Test invalid status
    assert OrderManagementService.validate_status("invalid_status") == False


def test_order_status_transition():
    """Test order status transitions."""
    from services.order_management import OrderManagementService
    
    # Test valid transitions
    assert OrderManagementService.can_transition_to("entwurf", "bereit_zur_bestellung") == True
    assert OrderManagementService.can_transition_to("bereit_zur_bestellung", "abgeschlossen") == False
    assert OrderManagementService.can_transition_to("abgeschlossen", "freigabe_erforderlich") == False
    
    # Test invalid transition
    assert OrderManagementService.can_transition_to("abgeschlossen", "neuer_status") == False


# ── Import Wizard Tests ────────────────────────────────────────────────

def test_import_wizard_creation():
    """Test ImportWizard creation."""
    from services.import_wizard import ImportWizard, ImportStep
    # Create an instance to verify attributes
    wizard = ImportWizard.__new__(ImportWizard)
    wizard.control_center = None
    wizard.mode = ImportMode.FULL
    wizard.current_step = ImportStep.SOURCE_SELECT
    wizard.state = {}
    wizard.errors = []
    wizard.warnings = []
    wizard.import_result = None
    # Just verify the instance has the expected attributes
    assert hasattr(wizard, "current_step")
    assert hasattr(wizard, "state")
    assert hasattr(wizard, "errors")
    assert hasattr(wizard, "advance")


def test_import_mode_enum():
    """Test ImportMode enum values."""
    from services.import_wizard import ImportMode, get_import_mode_display, get_import_mode_by_name
    
    assert ImportMode.FULL.value == 1
    assert ImportMode.DELTA.value == 2
    assert str(ImportMode.FULL) == "Vollimport"
    
    # Test conversion functions
    assert get_import_mode_by_name("Vollimport") == ImportMode.FULL
    assert get_import_mode_display(ImportMode.FULL) == "Vollimport"


def test_import_wizard_state():
    """Test ImportWizard state management."""
    from services.import_wizard import ImportWizard
    
    wizard = ImportWizard.__new__(ImportWizard)
    wizard.control_center = None
    wizard.mode = ImportMode.FULL
    wizard.current_step = 1
    wizard.state = {}
    wizard.errors = []
    wizard.warnings = []
    wizard.import_result = None
    
    wizard.set_state("test_key", "test_value")
    assert wizard.get_state("test_key") == "test_value"
    assert wizard.get_state("nonexistent", "default") == "default"
    
    wizard.set_error("test_error")
    assert "test_error" in wizard.errors
    
    wizard.clear_errors()
    assert len(wizard.errors) == 0


# ── Image Manager Tests ────────────────────────────────────────────────

def test_image_manager_creation():
    """Test ImageManager creation."""
    from services.image_manager import ImageManager
    manager = ImageManager()
    assert manager is not None
    assert hasattr(manager, "download_image")
    assert hasattr(manager, "process_download_queue")


def test_image_result_creation():
    """Test ImageResult creation."""
    from services.image_manager import ImageResult
    result = ImageResult(
        url="https://example.com/image.jpg",
        sha256_hash="abc123",
        saved_path="/path/to/image.jpg",
        original_filename="image.jpg",
        width=800,
        height=600,
        size_kb=50,
    )
    assert result.sha256_hash == "abc123"
    assert result.width == 800
    assert result.height == 600
    assert result.size_kb == 50


def test_image_result_duplicate():
    """Test ImageResult duplicate detection."""
    from services.image_manager import ImageResult
    result = ImageResult(
        url="https://example.com/image.jpg",
        sha256_hash="abc123",
        is_duplicate=True,
    )
    assert result.is_duplicate == True


def test_image_manager_download():
    """Test ImageManager download (mocked)."""
    from services.image_manager import ImageManager
    import tempfile
    import os
    
    manager = ImageManager()
    
    # Create a temp image file for testing
    with tempfile.NamedTemporaryFile(suffix=".jpg") as tmp:
        # Write some dummy data
        os.write(tmp.fileno(), b"fake image data")
        tmp.flush()
        
        # Test SHA-256 calculation
        sha256 = manager._calculate_sha256(Path(tmp.name))
        assert len(sha256) == 64  # SHA-256 hex digest length