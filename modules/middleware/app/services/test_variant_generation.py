import unittest

from .variant_generation import VariantGenerationService
from .enrichment_contracts import ProductSource

class TestVariantGenerationService(unittest.TestCase):
    def setUp(self):
        self.service = VariantGenerationService()

    def test_generate(self):
        product = ProductSource(
            sku="123",
            name="Test Product",
            description="Test Description",
            price=10.0,
            tax=19.0,
            manufacturer="Apple",
            category="Electronics",
            gtin="1234567890128",
            images=[],
            variants=[
                ProductSource(
                    sku="123-1",
                    name="Test Product Variant 1",
                    description="Test Description",
                    price=10.0,
                    tax=19.0,
                    manufacturer="Apple",
                    category="Electronics",
                    gtin="1234567890129",
                    images=[],
                    variants=[],
                )
            ],
        )
        result = self.service.generate(product)
        self.assertEqual(result.parent_sku, "123")
        self.assertEqual(result.child_skus, ["123-1"])

if __name__ == "__main__":
    unittest.main()
