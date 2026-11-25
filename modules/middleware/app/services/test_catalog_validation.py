import unittest

from .catalog_validation import CatalogValidationService
from .enrichment_contracts import ProductSource


class TestCatalogValidationService(unittest.TestCase):
    def setUp(self):
        self.service = CatalogValidationService(
            required_fields=["sku", "name", "price", "tax", "manufacturer", "category"],
            whitelisted_categories=["Electronics", "Clothing"],
            whitelisted_manufacturers=["Apple", "Nike"],
        )

    def test_validate_valid_product(self):
        product = ProductSource(
            sku="123", name="Test Product", description="Test Description", price=10.0, tax=19.0,
            manufacturer="Apple", category="Electronics", gtin="4006381333931", images=[], variants=[],
        )
        result = self.service.validate(product)
        self.assertTrue(result.is_valid)
        self.assertEqual(result.errors, [])

    def test_validate_missing_required_fields(self):
        product = ProductSource(
            sku="", name="", description="Test Description", price=10.0, tax=19.0,
            manufacturer="", category="", gtin="", images=[], variants=[],
        )
        result = self.service.validate(product)
        self.assertFalse(result.is_valid)
        self.assertGreaterEqual(len(result.errors), 1)
