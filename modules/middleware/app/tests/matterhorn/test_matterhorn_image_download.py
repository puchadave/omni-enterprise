import asyncio
import io
import os
import tempfile
from pathlib import Path

import pytest
from PIL import Image

os.environ["DATABASE_URL"] = ""


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "matterhorn.sqlite"
    import database
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)
    monkeypatch.setenv("MEDIA_IMAGE_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("MATTERHORN_IMAGE_MAX_BYTES", "1048576")
    monkeypatch.setenv("MATTERHORN_IMAGE_TIMEOUT_SECONDS", "5")
    asyncio.run(database.init_db())
    yield database


def _jpg(color="white", size=(8, 8)):
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="JPEG")
    return buf.getvalue()


class _Resp:
    def __init__(self, content, status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {"content-length": str(len(content)), "content-type": "image/jpeg"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    @property
    def is_redirect(self):
        return self.status_code in {301, 302, 303, 307, 308}


def test_matterhorn_import_downloads_images_and_dedupes(temp_db, monkeypatch, tmp_path):
    from services.matterhorn_importer import MatterhornUniversalImporter

    img = _jpg("white")
    calls = []

    def fake_get(url, stream=False, allow_redirects=False, timeout=None, headers=None):
        calls.append(url)
        if "bad" in url:
            return _Resp(b"not-an-image", headers={"content-type": "text/plain", "content-length": "12"})
        return _Resp(img)

    monkeypatch.setattr("services.media_ingestion.requests.get", fake_get)
    monkeypatch.setattr("services.media_ingestion.socket.getaddrinfo", lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 0))])

    xml = """<products><product><sku>IMG-1</sku><title>Bildtest</title><supplier_price>10</supplier_price><stock>5</stock>
    <images>https://example.com/a.jpg,https://example.com/a-dup.jpg,https://example.com/bad.jpg</images></product></products>"""
    imp = MatterhornUniversalImporter()
    res = asyncio.run(imp.import_products(mode="full", dry_run=False, confirm_full_without_preview=True, content=xml, filename="x.xml"))
    assert res["status"] == "success"
    p = asyncio.run(temp_db.fetch_product("IMG-1"))
    assert p["main_image_url"]
    gallery = p.get("gallery_json") or []
    assert len(gallery) >= 1
    first = gallery[0]
    assert first.get("sha256")
    assert first.get("local_path")
    assert os.path.exists(first["local_path"])
    # dedupe: two identical images should result in one stored asset entry
    sha_vals = [g.get("sha256") for g in gallery if isinstance(g, dict)]
    assert len(sha_vals) == len(set(sha_vals))
    # partial failure should not fail import
    issues = (res.get("protocol") or {}).get("issues") or []
    assert any(i.get("reason") == "image_partial_failure" for i in issues)


def test_matterhorn_image_ssrf_blocked(temp_db, monkeypatch, tmp_path):
    from services.matterhorn_importer import MatterhornUniversalImporter

    monkeypatch.setattr("services.media_ingestion.socket.getaddrinfo", lambda *a, **k: [(None, None, None, None, ("169.254.169.254", 0))])
    xml = """<products><product><sku>IMG-SSRF</sku><title>SSRF</title><supplier_price>10</supplier_price><stock>1</stock>
    <images>http://169.254.169.254/latest/meta-data/</images></product></products>"""
    imp = MatterhornUniversalImporter()
    res = asyncio.run(imp.import_products(mode="full", dry_run=False, confirm_full_without_preview=True, content=xml, filename="x.xml"))
    assert res["status"] == "success"
    p = asyncio.run(temp_db.fetch_product("IMG-SSRF"))
    gallery = p.get("gallery_json") or []
    assert gallery == []
    issues = (res.get("protocol") or {}).get("issues") or []
    assert any(i.get("reason") == "image_partial_failure" for i in issues)
