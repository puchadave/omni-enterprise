"""Channel Adapter Tests - simplified structure focus"""

import pytest
import asyncio
from services.channel_adapters import (
    get_shopify_adapter,
    get_amazon_adapter,
    get_google_shopping_adapter,
    get_ebay_adapter,
    get_zalando_adapter,
    ChannelAdapter,
)


class TestChannelAdapterBase:
    """Test base ChannelAdapter class."""

    def test_base_adapter_init(self):
        adapter = ChannelAdapter()
        assert adapter is not None

    def test_base_adapter_has_client(self):
        adapter = ChannelAdapter()
        assert adapter.client is not None


class TestShopifyAdapter:
    """Test Shopify adapter factory."""

    def test_shopify_adapter_init(self):
        adapter = get_shopify_adapter()
        assert adapter is not None

    def test_shopify_adapter_type(self):
        adapter = get_shopify_adapter()
        assert adapter.__class__.__name__ == "ShopifyAdapter"


class TestAmazonAdapter:
    """Test Amazon adapter factory."""

    def test_amazon_adapter_init(self):
        adapter = get_amazon_adapter()
        assert adapter is not None

    def test_amazon_adapter_type(self):
        adapter = get_amazon_adapter()
        assert adapter.__class__.__name__ == "AmazonAdapter"


class TestGoogleShoppingAdapter:
    """Test Google Shopping adapter factory."""

    def test_google_shopping_adapter_init(self):
        adapter = get_google_shopping_adapter()
        assert adapter is not None

    def test_google_shopping_adapter_type(self):
        adapter = get_google_shopping_adapter()
        assert adapter.__class__.__name__ == "GoogleShoppingAdapter"


class TestEbayAdapter:
    """Test eBay adapter factory."""

    def test_ebay_adapter_init(self):
        adapter = get_ebay_adapter()
        assert adapter is not None

    def test_ebay_adapter_type(self):
        adapter = get_ebay_adapter()
        assert adapter.__class__.__name__ == "EbayAdapter"


class TestZalandoAdapter:
    """Test Zalando adapter factory."""

    def test_zalando_adapter_init(self):
        adapter = get_zalando_adapter()
        assert adapter is not None

    def test_zalando_adapter_type(self):
        adapter = get_zalando_adapter()
        assert adapter.__class__.__name__ == "ZalandoAdapter"


class TestAdapterFactoryFunctions:
    """Test all adapter factory functions."""

    def test_get_shopify_adapter(self):
        adapter = get_shopify_adapter()
        assert adapter is not None

    def test_get_amazon_adapter(self):
        adapter = get_amazon_adapter()
        assert adapter is not None

    def test_get_google_shopping_adapter(self):
        adapter = get_google_shopping_adapter()
        assert adapter is not None

    def test_get_ebay_adapter(self):
        adapter = get_ebay_adapter()
        assert adapter is not None

    def test_get_zalando_adapter(self):
        adapter = get_zalando_adapter()
        assert adapter is not None


class TestAdapterImportStructure:
    """Test that adapters can be imported and have expected structure."""

    def test_channel_adapter_imports(self):
        from services.channel_adapters import ChannelAdapter
        assert ChannelAdapter is not None

    def test_all_adapters_importable(self):
        from services.channel_adapters import (
            ShopifyAdapter,
            AmazonAdapter,
            GoogleShoppingAdapter,
            EbayAdapter,
            ZalandoAdapter,
        )
        assert ShopifyAdapter is not None
        assert AmazonAdapter is not None
        assert GoogleShoppingAdapter is not None
        assert EbayAdapter is not None
        assert ZalandoAdapter is not None
