import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ["DATABASE_URL"] = ""


@pytest.fixture()
def temp_db(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        db_path = Path(d) / "matterhorn.sqlite"
        import database
        monkeypatch.setattr(database, "DATABASE_PATH", db_path)
        asyncio.run(database.init_db())
        yield database


def _sample_xml():
    return """<?xml version="1.0" encoding="UTF-8"?>
<products>
  <product>
    <sku>MH-L-100</sku>
    <external_id>EXT-100</external_id>
    <title>Logistik Shirt</title>
    <category>Bekleidung/Herren/Shirts</category>
    <supplier_price>12,50</supplier_price>
    <sale_price>29.99</sale_price>
    <stock>10</stock>
    <ean>4006381333931</ean>
    <brand>TestBrand</brand>
    <warehouse_location>WH-A</warehouse_location>
    <bin_code>A-01-03</bin_code>
    <weight_net_kg>0.25</weight_net_kg>
    <weight_gross_kg>0.30</weight_gross_kg>
    <length_cm>30</length_cm>
    <width_cm>20</width_cm>
    <height_cm>2</height_cm>
    <hs_code>610910</hs_code>
    <country_of_origin>DE</country_of_origin>
    <lead_time_days>3</lead_time_days>
    <reorder_level>2</reorder_level>
    <color>Blau</color>
    <material>Baumwolle</material>
    <images>https://example.com/a.jpg,https://example.com/b.jpg</images>
    <variants>
      <variant><sku>MH-L-100-BL-S</sku><ean>4006381333948</ean><stock>4</stock><size>S</size><color>Blau</color></variant>
      <variant><sku>MH-L-100-BL-M</sku><ean>4006381333955</ean><stock>6</stock><size>M</size><color>Blau</color></variant>
    </variants>
  </product>
</products>
"""


def test_import_sample_with_variants_attributes_logistics(temp_db):
    from services.matterhorn_importer import MatterhornUniversalImporter

    imp = MatterhornUniversalImporter()
    res = asyncio.run(imp.import_products(mode="full", dry_run=False, confirm_full_without_preview=True, content=_sample_xml(), filename="sample.xml"))
    assert res["status"] == "success"
    assert res["imported"] == 1
    assert res.get("variants_linked") == 2

    p = asyncio.run(temp_db.fetch_product("MH-L-100"))
    assert p is not None
    assert p["brand"] == "TestBrand"
    assert p["warehouse_location"] == "WH-A"
    assert p["bin_code"] == "A-01-03"
    assert float(p["weight_net_kg"]) == pytest.approx(0.25)
    assert float(p["weight_gross_kg"]) == pytest.approx(0.30)
    assert float(p["length_cm"]) == pytest.approx(30.0)
    assert float(p["width_cm"]) == pytest.approx(20.0)
    assert float(p["height_cm"]) == pytest.approx(2.0)
    assert p["hs_code"] == "610910"
    assert p["country_of_origin"] == "DE"
    assert int(p["lead_time_days"]) == 3
    assert int(p["reorder_level"]) == 2
    assert int(p.get("supplier_stock") or 0) == 10
    assert isinstance(p.get("variants"), list) and len(p["variants"]) == 2
    vskus = {v.get("sku") for v in p["variants"]}
    assert {"MH-L-100-BL-S", "MH-L-100-BL-M"} <= vskus
    attrs = p.get("attributes") or []
    names = {str(a.get("name")) for a in attrs}
    assert "material" in names or "Material" in names or any(n.lower()=="material" for n in names)
    cats = p.get("categories") or []
    assert cats


def test_import_sync_rules_stock_only_preserves_price(temp_db):
    from services.matterhorn_importer import MatterhornUniversalImporter

    imp = MatterhornUniversalImporter()
    xml1 = """<products><product><sku>MH-S-1</sku><title>A</title><supplier_price>10.00</supplier_price><stock>5</stock></product></products>"""
    xml2 = """<products><product><sku>MH-S-1</sku><title>A changed</title><supplier_price>99.00</supplier_price><stock>20</stock></product></products>"""
    r1 = asyncio.run(imp.import_products(mode="full", dry_run=False, confirm_full_without_preview=True, content=xml1, filename="a.xml"))
    assert r1["status"] == "success"
    r2 = asyncio.run(imp.import_products(mode="stock_only", dry_run=False, confirm_full_without_preview=True, content=xml2, filename="b.xml", sync_rules={"mode":"stock_only"}))
    assert r2["status"] == "success"
    p = asyncio.run(temp_db.fetch_product("MH-S-1"))
    assert float(p["supplier_price"]) == pytest.approx(10.0)
    assert int(p.get("supplier_stock") or 0) == 20
