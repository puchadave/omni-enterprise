import unittest

from .description_enrichment import DescriptionEnrichmentService
from .enrichment_contracts import ProductSource

class TestDescriptionEnrichmentService(unittest.TestCase):
    def setUp(self):
        self.service = DescriptionEnrichmentService(locale="de_DE")

    def test_enrich(self):
        product = ProductSource(
            sku="123",
            name="Test Product",
            description="Test Description",
            price=10.0,
            tax=19.0,
            manufacturer="Apple",
            category="Electronics",
            gtin="1234567890128",
            images=["image1.jpg", "image2.jpg"],
            variants=[],
        )
        result = self.service.enrich(product)
        self.assertEqual(result.title, "Test Product")
        self.assertEqual(result.meta_description, "Test Description")
        self.assertEqual(result.slug, "test-product")
        self.assertEqual(result.alt_texts, ["Test Product image 1", "Test Product image 2"])

if __name__ == "__main__":
    unittest.main()
