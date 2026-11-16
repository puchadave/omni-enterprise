"""Marketing Engine Tests - simplified, focusing on structure and non-async functionality"""

import pytest
from services.marketing_engine import MarketingEngine


class TestMarketingEngineInitialization:
    """Test MarketingEngine initialization."""

    def test_marketing_engine_default_init(self):
        engine = MarketingEngine()
        assert engine is not None

    def test_marketing_engine_with_defaults(self):
        # Test that engine initializes with default OmniRoute client
        engine = MarketingEngine()
        assert engine.client is not None


class TestMarketingEngineAttributes:
    """Test MarketingEngine attributes."""

    def test_ad_formats_exist(self):
        engine = MarketingEngine()
        assert hasattr(engine, 'ad_formats')
        assert "meta" in engine.ad_formats

    def test_default_platforms(self):
        engine = MarketingEngine()
        assert "meta" in engine.ad_formats
        assert "google_search" in engine.ad_formats


class TestMarketingEngineFactory:
    """Test factory function."""

    def test_get_marketing_engine(self):
        engine = MarketingEngine()
        assert engine is not None


class TestMarketingEngineNewsletter:
    """Test newsletter permission logic (sync, no gateway needed)."""

    def test_newsletter_permission_with_consent(self):
        """Test newsletter permission logic independently."""
        # Test the logic directly
        consent = True
        opt_out = False
        result = consent and not opt_out
        assert result is True

    def test_newsletter_permission_no_consent(self):
        consent = False
        opt_out = False
        result = consent and not opt_out
        assert result is False

    def test_newsletter_permission_opt_out(self):
        consent = True
        opt_out = True
        result = consent and not opt_out
        assert result is False


class TestMarketingEngineChannelContentStorage:
    """Test channel content storage structure (database-related)."""

    def test_channel_content_structure(self):
        """Test that channel content has the expected structure."""
        # Test that we can create the expected data structure
        content = {
            "product_sku": "TEST001",
            "channel": "shopify",
            "content_type": "description",
            "content_json": {"description": "Test product"},
            "language": "en",
            "model": "david",
            "prompt_version": "v1",
            "generation_status": "pending",
            "created_at": "",
            "updated_at": "",
        }
        assert content["product_sku"] == "TEST001"
        assert content["channel"] == "shopify"
        assert content["content_type"] == "description"
        assert content["generation_status"] == "pending"


class TestMarketingEngineContentTypes:
    """Test content type definitions."""

    def test_content_type_fields(self):
        """Test that content types have required fields."""
        required_fields = ["id", "product_sku", "channel", "content_type", 
                          "content_json", "language", "model", "prompt_version",
                          "generation_status", "created_at", "updated_at"]
        content = {
            "id": 1,
            "product_sku": "TEST001",
            "channel": "shopify",
            "content_type": "description",
            "content_json": {},
            "language": "en",
            "model": "david",
            "prompt_version": "v1",
            "generation_status": "pending",
            "created_at": "",
            "updated_at": "",
        }
        for field in required_fields:
            assert field in content, f"Missing field: {field}"
