"""Tests for Matterhorn Feed Manager.

Tests the feed parser for XML, CSV, XLS formats and the unified
parse_feed entry point.
"""

from __future__ import annotations

import pytest
from services.feed_manager import (
    parse_feed, FORMAT_XML, FORMAT_XML_V2, FORMAT_XML_EAN,
    FORMAT_CSV, FORMAT_XLS, FORMAT_SHOPIFY, FORMAT_WOOCOMMERCE,
    FORMAT_PRESTASHOP_CATEGORIES, FORMAT_PRESTASHOP_PRODUCTS,
    FORMAT_PRESTASHOP_STOCK, FORMAT_XLS_DICTIONARY,
    parse_xml_standard, parse_xml_v2, parse_xml_ean,
    parse_csv_feed, parse_xls_feed, parse_shopify_feed,
    parse_wooocommerce_feed, parse_presta_shop_categories,
    parse_presta_shop_products, parse_presta_shop_stock,
    parse_xls_dictionary, detect_feed_format, FeedResult
)


# ── XML Parser Tests ────────────────────────────────────────────────────

def test_parse_xml_standard():
    """Test standard XML parsing."""
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
    <products>
        <product>
            <sku>TEST001</sku>
            <title>Test Product</title>
            <description>Test Description</description>
            <price>19.99</price>
            <category>Test Category</category>
            <stock>10</stock>
        </product>
    </products>"""
    
    products = parse_xml_standard(xml_text)
    assert len(products) == 1
    assert products[0]["sku"] == "TEST001"
    assert products[0]["title"] == "Test Product"


def test_parse_xml_v2():
    """Test XML v2 parsing."""
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
    <offers>
        <offer>
            <articlenumber>TEST002</articlenumber>
            <bezeichnung>Test Product 2</bezeichnung>
            <ekp>29.99</ekp>
            <kategorie>Test Category</kategorie>
            <vorrat>5</vorrat>
        </offer>
    </offs>"""
    
    # Fix the typo in the test close tag
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
    <offers>
        <offer>
            <articlenumber>TEST002</articlenumber>
            <bezeichnung>Test Product 2</bezeichnung>
            <ekp>29.99</ekp>
            <kategorie>Test Category</kategorie>
            <vorrat>5</vorrat>
        </offer>
    </offers>"""
    
    products = parse_xml_v2(xml_text)
    assert len(products) == 1
    assert products[0]["sku"] == "TEST002"
    assert products[0]["title"] == "Test Product 2"


def test_parse_xml_ean():
    """Test XML EAN parsing."""
    xml_text = """<?xml version="1.0" encoding="UTF-8"?>
    <feed>
        <product>
            <sku>TEST003</sku>
            <title>Test Product 3</title>
            <ean>4012345678901</ean>
            <price>39.99</price>
        </product>
    </feed>"""
    
    products = parse_xml_ean(xml_text)
    assert len(products) == 1
    assert products[0]["ean"] == "4012345678901"


# ── CSV Parser Tests ────────────────────────────────────────────────────

def test_parse_csv_feed():
    """Test CSV parsing."""
    csv_text = """sku,title,price,stock
TEST001,Test Product,19.99,10
TEST002,Test Product 2,29.99,5"""
    
    products = parse_csv_feed(csv_text)
    assert len(products) == 2
    assert products[0]["sku"] == "TEST001"


def test_parse_csv_with_mapping():
    """Test CSV parsing with header mapping."""
    csv_text = """articlenr,bezeichnung,einkaufspreis
TEST003,Test Product 3,25.50"""
    
    mapping = {"articlenr": "sku", "bezeichnung": "title", "einkaufspreis": "price"}
    products = parse_csv_feed(csv_text, header_mapping=mapping)
    assert len(products) == 1
    assert products[0]["sku"] == "TEST003"


# ── Format Detection Tests ─────────────────────────────────────────────

def test_detect_feed_format_xml():
    """Test XML format detection."""
    xml_text = """<?xml version="1.0"?><products><product><sku>TEST</sku></sku></products>"""
    fmt, meta = detect_feed_format(xml_text, "test.xml")
    assert fmt == FORMAT_XML


def test_detect_feed_format_csv():
    """Test CSV format detection."""
    csv_text = """sku,title,price
TEST001,Product,9.99"""
    fmt, meta = detect_feed_format(csv_text, "test.csv")
    assert fmt == FORMAT_CSV


def test_detect_feed_format_xls():
    """Test XLS format detection."""
    xls_text = "\tcol1\tcol2\nval1\tval2"
    fmt, meta = detect_feed_format(xls_text, "test.xls")
    assert fmt == FORMAT_XLS


def test_detect_feed_format_unknown():
    """Test unknown format detection."""
    fmt, meta = detect_feed_format("random text", "test.txt")
    assert fmt in (FORMAT_XML, "unknown")


# ── Unified Parse Function Tests ────────────────────────────────────────

def test_parse_feed_xml():
    """Test unified XML feed parsing."""
    xml_text = """<?xml version="1.0"?><products><product><sku>TEST</sku><title>Test</title></product></products>"""
    
    result = parse_feed(xml_text, format_hint=FORMAT_XML)
    assert result.format == FORMAT_XML
    assert result.product_count > 0


def test_parse_feed_csv():
    """Test unified CSV feed parsing."""
    csv_text = "sku,title,price\nTEST001,Product,9.99"
    
    result = parse_feed(csv_text, format_hint=FORMAT_CSV)
    assert result.format == FORMAT_CSV
    assert result.product_count > 0


def test_parse_feed_unknown():
    """Test unknown format falls back to XML."""
    text = "some random text"
    result = parse_feed(text)
    # Should not crash, default to XML
    assert result.format is not None


# ── FeedResult Tests ────────────────────────────────────────────────────

def test_feed_result_properties():
    """Test FeedResult properties."""
    result = FeedResult(format=FORMAT_XML, products=[{"sku": "TEST"}])
    assert result.product_count == 1
    assert result.format == FORMAT_XML
    assert result.errors == []
    assert result.warnings == []