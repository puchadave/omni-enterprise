"""Matterhorn Feed Manager.

Manages product feeds in various formats (XML, CSV, XLS) from Matterhorn
and other suppliers. Provides parsing, validation, mapping, and import
functionality for the Omni-Core ERP system.

Supported formats:
1. XML Standard
2. XML 2.0
3. XML 2.0 EAN
4. XLS
5. CSV
6. Shopify Feed
7. WooCommerce Feed
8. PrestaShop Categories
9. PrestaShop Products
10. PrestaShop Stock
11. XLS Dictionary
"""

from __future__ import annotations

import csv
import io
import logging
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

import httpx
import xlrd

logger = logging.getLogger(__name__)

# ── Format Identifiers ─────────────────────────────────────────────────────

FORMAT_XML = "xml"
FORMAT_XML_V2 = "xml_v2"
FORMAT_XML_EAN = "xml_ean"
FORMAT_CSV = "csv"
FORMAT_XLS = "xls"
FORMAT_SHOPIFY = "shopify"
FORMAT_WOOCOMMERCE = "woocommerce"
FORMAT_PRESTASHOP_CATEGORIES = "prestashop_categories"
FORMAT_PRESTASHOP_PRODUCTS = "prestashop_products"
FORMAT_PRESTASHOP_STOCK = "prestashop_stock"
FORMAT_XLS_DICTIONARY = "xls_dictionary"

# ── Feed Result ───────────────────────────────────────────────────────────

class FeedResult:
    """Result of a feed parsing/import operation."""
    def __init__(
        self,
        format: str,
        products: list[dict[str, Any]] | None = None,
        errors: list[str] | None = None,
        warnings: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.format = format
        self.products = products or []
        self.errors = errors or []
        self.warnings = warnings or []
        self.metadata = metadata or {}

    @property
    def product_count(self) -> int:
        return len(self.products)

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0


# ── XML Parsers ───────────────────────────────────────────────────────────

def parse_xml_standard(text: str) -> list[dict[str, Any]]:
    """Parse standard Matterhorn XML offer feed."""
    products: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(text)
        for item in root.findall(".//product"):
            product: dict[str, Any] = {}
            for child in item:
                tag = child.tag.lower().strip()
                # Map common Matterhorn XML tags
                tag_map: dict[str, str] = {
                    "sku": "sku",
                    "title": "title",
                    "name": "title",
                    "description": "description",
                    "price": "price",
                    "ekp": "price",  # Einkaufskosten-Preis
                    "category": "category",
                    "cat": "category",
                    "stock": "stock",
                    "ean": "ean",
                    "gtin": "ean",
                }
                value = (child.text or "").strip()
                if tag in tag_map:
                    product[tag_map[tag]] = value
                else:
                    product[tag] = value
            # Ensure required fields
            if product.get("sku") or product.get("title"):
                products.append(product)
    except ET.ParseError as exc:
        logger.error("XML parse error: %s", exc)
    return products


def parse_xml_v2(text: str) -> list[dict[str, Any]]:
    """Parse Matterhorn XML 2.0 feed."""
    products: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(text)
        # XML 2.0 may use different structure - wrapper elements, etc.
        for item in root.findall(".//offer") or root.findall(".//product"):
            product: dict[str, Any] = {}
            for child in item:
                tag = child.tag.lower().strip()
                value = (child.text or "").strip()
                # Map common XML 2.0 tags
                if tag in ("sku", "articlenumber", "article-number"):
                    product["sku"] = value
                elif tag in ("name", "title", "bezeichnung"):
                    product["title"] = value
                elif tag in ("description", "desc", "beschreibung"):
                    product["description"] = value
                elif tag in ("price", "ekp", "einkaufspreis"):
                    product["price"] = value
                elif tag in ("category", "kategorie"):
                    product["category"] = value
                elif tag in ("ean", "gtin", "ean13"):
                    product["ean"] = value
                elif tag in ("stock", "vorrat", "bestand"):
                    product["stock"] = value
            if product.get("sku") or product.get("title"):
                products.append(product)
    except ET.ParseError as exc:
        logger.error("XML v2 parse error: %s", exc)
    return products


def parse_xml_ean(text: str) -> list[dict[str, Any]]:
    """Parse Matterhorn XML 2.0 EAN-focused feed, emphasizing EAN/GTIN."""
    products: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(text)
        for element in root.iter():
            tag = element.tag.lower().strip()
            # Prioritize EAN/GTIN fields
            if tag in ("ean", "gtin", "ean13"):
                # Collect EAN info
                pass
            # General product collection
            if tag in ("product", "offer", "item"):
                # Collect children
                pass
        # Fallback to standard parsing
        products = parse_xml_standard(text)
        # Enrich with EAN validation
        for p in products:
            if "ean" not in p and "gtin" not in p:
                # Try to extract from description or other fields
                pass
    except ET.ParseError as exc:
        logger.error("XML EAN parse error: %s", exc)
    return products


# ── CSV Parser ────────────────────────────────────────────────────────────

def parse_csv_feed(
    text: str,
    header_mapping: dict[str, str] | None = None,
    delimiter: str = ";",
) -> list[dict[str, Any]]:
    """Parse CSV feed with optional header mapping.

    Matterhorn CSV feeds often use semicolon delimiter and localized headers.
    The header_mapping maps CSV headers to canonical field names.
    """
    products: list[dict[str, Any]] = []
    try:
        # Detect delimiter if not specified
        if delimiter not in (text[:10].count(";"), text[:10].count(",")):
            delimiter = ","

        reader = csv.reader(text.splitlines(), delimiter=delimiter)
        headers: list[str] | None = None
        row_index = 0

        for row in reader:
            if row_index == 0:
                headers = [h.strip().lower() for h in row]
                if header_mapping:
                    # Rename headers according to mapping
                    headers = [
                        header_mapping.get(h, h) for h in headers
                    ]
                row_index += 1
                continue

            if headers is None:
                headers = [f"col_{i}" for i in range(len(row))]

            product: dict[str, Any] = {}
            for i, value in enumerate(row):
                if i < len(headers):
                    product[headers[i]] = value.strip() if value else ""

            # Only add product if it has essential fields
            if product.get("sku") or product.get("title") or product.get("ean"):
                products.append(product)

            row_index += 1
    except Exception as exc:
        logger.error("CSV parse error: %s", exc)
    return products


# ── XLS Parser ────────────────────────────────────────────────────────────

def parse_xls_feed(file_data: bytes, sheet_index: int = 0) -> list[dict[str, Any]]:
    """Parse XLS (Excel) feed file.

    Supports Matterhorn XLS feeds with product data in worksheets.
    """
    products: list[dict[str, Any]] = []
    try:
        workbook = xlrd.open_workbook(file_contents=file_data)
        sheet = workbook.sheet_by_index(sheet_index)

        # Use first row as headers
        headers: list[str] = []
        for col in range(sheet.ncols):
            headers.append(sheet.cell_value(0, col))

        # Map known Matterhorn column names
        column_map: dict[str, int] = {}
        for col_idx, header in enumerate(headers):
            h_lower = header.lower().strip()
            col_map_lower: dict[str, str] = {
                "sku": "sku",
                "artikelnr": "sku",
                "artikelnummer": "sku",
                "ean": "ean",
                "gtin": "ean",
                "title": "title",
                "name": "title",
                "bezeichnung": "title",
                "description": "description",
                "price": "price",
                "ekp": "price",
                "einkaufspreis": "price",
                "category": "category",
                "kategorie": "category",
                "stock": "stock",
                "bestand": "stock",
                "variant": "variant",
                "variante": "variant",
            }
            if h_lower in col_map_lower:
                column_map[h_lower] = col_idx

        # Data rows
        for row_idx in range(1, min(sheet.nrows, 10000)):  # Limit for safety
            row = sheet.row_values(row_idx)
            product: dict[str, Any] = {}

            # Extract fields using column map
            for mapped_name, col_idx in column_map.items():
                if col_idx < len(row):
                    value = row[col_idx]
                    if isinstance(value, float) and mapped_name == "price":
                        value = round(value, 2)
                    elif isinstance(value, float) and mapped_name == "stock":
                        value = int(value)
                    product[mapped_name] = str(value) if value else ""

            # Also check by header name for unmapped columns
            for col_idx in range(len(headers)):
                if col_idx not in column_map.values():
                    header = headers[col_idx].lower().strip()
                    value = row[col_idx]
                    # Try to detect important fields
                    if "sku" in header and not product.get("sku"):
                        product["sku"] = str(value) if value else ""
                    elif "ean" in header and not product.get("ean"):
                        product["ean"] = str(value) if value else ""
                    elif "title" in header and not product.get("title"):
                        product["title"] = str(value) if value else ""
                    elif "price" in header and not product.get("price"):
                        product["price"] = str(value) if value else ""
                    elif "category" in header and not product.get("category"):
                        product["category"] = str(value) if value else ""
                    elif "stock" in header and not product.get("stock"):
                        product["stock"] = str(value) if value else ""

            # Only add product if it has essential fields
            if product.get("sku") or product.get("title") or product.get("ean"):
                products.append(product)

    except Exception as exc:
        logger.error("XLS parse error: %s", exc)
    return products


# ── Shopify Feed Parser ──────────────────────────────────────────────────

def parse_shopify_feed(text: str) -> list[dict[str, Any]]:
    """Parse Shopify product feed (CSV format)."""
    products: list[dict[str, Any]] = []
    products = parse_csv_feed(text, delimiter=",")
    # Shopify CSV typically has these columns:
    # Handle, Title, Body (HTML), Vendor, Type, Tags, Published, Variant SKU, Variant Price, Variant Inventory Tracker, Variant Inventory Qty
    for p in products:
        # Normalize Shopify fields
        if "body_html" not in p and "body" in p:
            p["body_html"] = p.pop("body")
    return products


# ── WooCommerce Feed Parser ─────────────────────────────────────────────

def parse_wooocommerce_feed(text: str) -> list[dict[str, Any]]:
    """Parse WooCommerce product feed (CSV format)."""
    products: list[dict[str, Any]] = []
    products = parse_csv_feed(text, delimiter=",")
    # WooCommerce CSV typically has these columns:
    # ID, Product Name, SKU, Product Categories, Description, Short Description, Date Published, Tax Status, Tax Class, Shipping Class, Images, Featured Image, VIP, On Sale, Stock SKU, Stock Status, Allow Backorders, Stock Quantity, Sold Individually, Weight, Dimensions, Note, Meta Data
    for p in products:
        # Normalize: ensure SKU and Price are present
        if "price" not in p and "stock" not in p:
            # Might be a header-only row
            if not any(p.values()):
                continue
    return products


# ── PrestaShop Feed Parsers ─────────────────────────────────────────────

def parse_presta_shop_categories(text: str) -> list[dict[str, Any]]:
    """Parse PrestaShop category feed (XML or CSV)."""
    # Try XML first, fall back to CSV
    products: list[dict[str, Any]] = []
    try:
        root = ET.fromstring(text)
        for cat in root.findall(".//category") or root.findall(".//category"):
            product: dict[str, Any] = {
                "id": cat.findtext("id") or "",
                "name": cat.findtext("name") or "",
                "parent": cat.findtext("parent") or "",
                "level": cat.findtext("level") or "",
                "active": cat.findtext("active") or "",
            }
            if product["name"]:
                products.append(product)
    except ET.ParseError:
        # Fall back to CSV parsing
        products = parse_csv_feed(text)
    return products


def parse_presta_shop_products(text: str) -> list[dict[str, Any]]:
    """Parse PrestaShop product feed (CSV or XML)."""
    # Try CSV first
    products = parse_csv_feed(text)
    if not products:
        # Try XML
        try:
            root = ET.fromstring(text)
            for product in root.findall(".//product") or root.findall(".//item"):
                p: dict[str, Any] = {}
                for child in product:
                    tag = child.tag.lower().strip()
                    if tag in ("id", "reference", "name", "price", "category", "condition", "supplier"):
                        p[tag] = child.text or ""
                if p.get("name") or p.get("reference"):
                    products.append(p)
        except ET.ParseError:
            pass
    return products


def parse_presta_shop_stock(text: str) -> list[dict[str, Any]]:
    """Parse PrestaShop stock feed."""
    products: list[dict[str, Any]] = []
    products = parse_csv_feed(text)
    # PrestaShop stock CSV typically has: ID, Reference, Location, Available quantity, Date
    for p in products:
        # Normalize stock fields
        if "available_quantity" not in p and "stock" not in p:
            for key in p:
                if "stock" in key.lower() or "quantite" in key.lower() or "available" in key.lower():
                    p["stock"] = p.pop(key, "")
                    break
    return products


# ── XLS Dictionary Parser ───────────────────────────────────────────────

def parse_xls_dictionary(text: bytes | str) -> dict[str, Any]:
    """Parse XLS dictionary/glossary file.

    Used for mapping EAN codes, country codes, size systems, etc.
    Returns a dict with category:code -> meaning mappings.
    """
    mappings: dict[str, Any] = {}
    try:
        if isinstance(text, str):
            text = text.encode("utf-8")
        workbook = xlrd.open_workbook(file_contents=text)
        for sheet_index in range(workbook.nsheets):
            sheet = workbook.sheet_by_index(sheet_index)
            # Assume first row is header, first column is key
            for row_idx in range(1, sheet.nrows):
                key = str(sheet.cell_value(row_idx, 0)).strip()
                value = str(sheet.cell_value(row_idx, 1)).strip() if sheet.ncols > 1 else ""
                if key:
                    mappings[key] = value
    except Exception as exc:
        logger.error("XLS dictionary parse error: %s", exc)
    return mappings


# ── Feed Discovery ────────────────────────────────────────────────────────

def detect_feed_format(
    text: str,
    filename: str | None = None,
    header_sample: int = 5,
) -> Tuple[str, dict[str, Any]]:
    """Automatically detect the format of a Matterhorn feed.

    Analyzes the content and returns the detected format plus metadata.

    Returns:
        (format_string, metadata_dict)
    """
    metadata: dict[str, Any] = {}

    # Check filename extensions
    if filename:
        fname = filename.lower()
        if fname.endswith(".xml"):
            detected = FORMAT_XML
        elif fname.endswith(".csv"):
            detected = FORMAT_CSV
        elif fname.endswith(".xls") or fname.endswith(".xlsx"):
            detected = FORMAT_XLS
        else:
            detected = _detect_by_content(text, header_sample)
    else:
        detected = _detect_by_content(text, header_sample)

    metadata["filename"] = filename
    metadata["detected"] = detected
    return detected, metadata


def _detect_by_content(text: str, header_sample: int) -> str:
    """Internal format detection by content analysis."""
    # Check for XML
    text_lower = text.lower()
    if text_lower.strip().startswith("<") and ("<product>" in text_lower or "<offer>" in text_lower or "<item>" in text_lower):
        # Could be XML 2.0 if has specific v2 markers
        if "xml_version" in text_lower or "version=" in text_lower:
            return FORMAT_XML_V2
        return FORMAT_XML

    # Check for CSV
    if ";" in text[:200] or text[:200].count(",") > 5:
        # Might be semicolon-separated (European format)
        lines = text.splitlines()
        if lines and ";" in lines[0]:
            return FORMAT_CSV

    # Check for XLS indicators (tab characters, multiple lines with same structure)
    if "\t" in text[:200] and text.count("\t") > 3:
        return FORMAT_XLS

    # Default
    return FORMAT_XML


# ── Export ────────────────────────────────────────────────────────────────

def format_product_for_csv(product: dict[str, Any]) -> dict[str, str]:
    """Format a product dict for CSV export with canonical field order."""
    canonical_fields: list[str] = [
        "sku", "title", "description", "price", "category", "ean", "stock",
        "variant", "brand", "supplier_id"
    ]
    result: dict[str, str] = {}
    for field in canonical_fields:
        if field in product:
            result[field] = str(product[field])
        else:
            result[field] = ""
    return result


# ── Helper: Stock buffer application ──────────────────────────────────────

STOCK_BUFFER_PERCENT: float = 0.10  # 10% safety buffer (same as matterhorn_importer)

def apply_stock_buffer(stock: int, percent: float = STOCK_BUFFER_PERCENT) -> int:
    """Apply safety stock buffer (reduce stock by percentage).

    Used when importing from supplier feeds to avoid overselling.
    """
    if stock < 0:
        return 0
    buffered = int(stock * (1 - percent))
    return max(0, buffered)


# ── Helper: Price normalization ──────────────────────────────────────────

def normalize_price(price_str: str | float | None, currency: str = "EUR") -> tuple[float, str]:
    """Normalize a price string to a float value with currency.

    Handles various formats: "19.99", "19,99", "19.99 EUR", etc.
    """
    if price_str is None:
        return 0.0, currency

    price_str = str(price_str).strip()

    # Remove currency suffix if present
    for curr in ("EUR", "USD", "GBP", "CHF"):
        if curr in price_str.upper():
            price_str = price_str.replace(curr, "").strip()
            break

    # Replace comma with period for European format
    price_str = price_str.replace(",", ".")

    try:
        value = float(price_str)
    except ValueError:
        value = 0.0

    return value, currency


# ── Unified feed parsing entry point ──────────────────────────────────────

def parse_feed(
    text: str,
    filename: str | None = None,
    format_hint: str | None = None,
) -> FeedResult:
    """Unified entry point for parsing Matterhorn feeds.

    Automatically detects the format and parses accordingly.

    Args:
        text: The feed content as text
        filename: Optional filename for extension-based detection
        format_hint: Optional format hint (overrides auto-detection)

    Returns:
        FeedResult with parsed products and any errors/warnings
    """
    # Determine format
    # detect_feed_format returns (format_string, metadata_dict)
    detected_format, _ = detect_feed_format(text, filename)
    format_hint = format_hint or detected_format

    # Parse based on format
    if format_hint == FORMAT_XML:
        products = parse_xml_standard(text)
    elif format_hint == FORMAT_XML_V2:
        products = parse_xml_v2(text)
    elif format_hint == FORMAT_XML_EAN:
        products = parse_xml_ean(text)
    elif format_hint == FORMAT_CSV:
        products = parse_csv_feed(text)
    elif format_hint == FORMAT_XLS:
        # For XLS we need binary data; this is a text fallback
        products = []
    elif format_hint == FORMAT_CSV:  # duplicate, keep for clarity
        products = parse_csv_feed(text)
    elif format_hint == FORMAT_SHOPIFY:
        products = parse_shopify_feed(text)
    elif format_hint == FORMAT_WOOCOMMERCE:
        products = parse_wooocommerce_feed(text)
    elif format_hint == FORMAT_PRESTASHOP_CATEGORIES:
        products = parse_presta_shop_categories(text)
    elif format_hint == FORMAT_PRESTASHOP_PRODUCTS:
        products = parse_presta_shop_products(text)
    elif format_hint == FORMAT_PRESTASHOP_STOCK:
        products = parse_presta_shop_stock(text)
    elif format_hint == FORMAT_XLS_DICTIONARY:
        # Need binary data
        products = []
    else:
        # Default to XML standard
        products = parse_xml_standard(text)
        format_hint = FORMAT_XML

    # Deduplicate by SKU if available
    seen_skus: set[str] = set()
    unique_products: list[dict[str, Any]] = []
    for p in products:
        sku = p.get("sku", "").strip()
        if sku and sku not in seen_skus:
            seen_skus.add(sku)
            unique_products.append(p)
        elif not sku:
            # Include products without SKU if they have other identifiers
            if not any(p2.get("sku") for p2 in unique_products):
                unique_products.append(p)

    # Apply stock buffer if stock data present
    for p in unique_products:
        if p.get("stock"):
            try:
                original = int(p["stock"])
                p["stock"] = apply_stock_buffer(original)
            except (ValueError, TypeError):
                pass

    warnings: list[str] = []
    if format_hint not in (FORMAT_XML, FORMAT_XML_V2, FORMAT_XML_EAN):
        warnings.append(f"Non-XML format detected: {format_hint}")

    return FeedResult(
        format=format_hint,
        products=unique_products,
        warnings=warnings,
        metadata={"detected_format": format_hint, "total_raw": len(products)},
    )