import io
import unittest
from unittest.mock import patch

from PIL import Image

from .media_ingestion import MediaIngestionService
from .enrichment_contracts import ProductSource


class _Resp:
    def __init__(self, content):
        self.content = content
        self.headers = {"content-length": str(len(content))}

    def raise_for_status(self):
        return None


class TestMediaIngestionService(unittest.TestCase):
    def setUp(self):
        self.service = MediaIngestionService(download_dir="/tmp/omni-media-test")

    @patch("services.media_ingestion.requests.get")
    @patch("services.media_ingestion.socket.getaddrinfo", return_value=[(None, None, None, None, ("93.184.216.34", 0))])
    def test_ingest(self, _mock_dns, mock_get):
        def make_img(color):
            buf = io.BytesIO()
            Image.new("RGB", (2, 2), color=color).save(buf, format="JPEG")
            return _Resp(buf.getvalue())

        mock_get.side_effect = [make_img("white"), make_img("black")]
        product = ProductSource(
            sku="123", name="Test Product", description="Test Description", price=10.0, tax=19.0,
            manufacturer="Apple", category="Electronics", gtin="1234567890128",
            images=["https://example.com/image1.jpg", "https://example.com/image2.jpg"], variants=[],
        )
        result = self.service.ingest(product)
        self.assertEqual(result.cover_url, "https://example.com/image1.jpg")
        self.assertEqual(len(result.image_urls), 2)
        self.assertEqual(result.errors, [])
