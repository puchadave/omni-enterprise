import unittest

from .sales_release_orchestrator import build_shopware_import_decision
from .enrichment_contracts import ProductSource


class TestSalesReleasePipeline(unittest.TestCase):
    def test_release_product_decision(self):
        product = ProductSource(
            sku="123", name="Test Product", description="Test Description", price=10.0, tax=19.0,
            manufacturer="Apple", category="Electronics", gtin="4006381333931",
            images=["https://example.com/image1.jpg"], variants=[],
        )
        result = build_shopware_import_decision(product)
        self.assertTrue(result.active)
        self.assertFalse(result.price_fallback_used)
        self.assertEqual(result.blocking_errors, [])
        self.assertEqual(result.export_gate, "APPROVED")
        self.assertEqual(result.cover_url, "https://example.com/image1.jpg")
