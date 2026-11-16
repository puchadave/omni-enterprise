"""Canonical Product Mapping.

Maps Matterhorn supplier data to the internal Omni-Core canonical product model.

Critical mapping rules:
- supplier_product_id and variant_uid are SEPARATE identifiers
- No variant merging based on product names
- No EAN data invention
- All original supplier IDs stored permanently
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# ── Canonical Product Model ───────────────────────────────────────────────

@dataclass
class CanonicalProduct:
    """The canonical product model used throughout Omni-Core.

    This model represents the normalized product data after mapping
    from external sources like Matterhorn.
    """

    # ── Supplier Identifiers (preserved exactly as received) ──────────────
    supplier_id: str  # Supplier company ID/code
    supplier_product_id: str  # Supplier's product ID - SEPARATE from variant_uid
    sku: str  # Internal SKU (may be supplier SKU or generated)  # noqa: E702
    product_name: str  # Full product name
    name_without_number: str  # Name without any article/supplier numbers
    description: str  # Full product description

    # ── Description & Meta (optional) ─────────────────────────────────────
    product_url: str | None = None  # Supplier/product URL
    brand: str | None = None  # Brand name
    brand_id: str | None = None  # Brand ID (supplier's or internal)
    color: str | None = None  # Primary color

    # ── Category & Hierarchy (optional) ───────────────────────────────────
    category: str | None = None  # Category name
    category_id: str | None = None  # Category ID
    category_path: str | None = None  # Full category path (e.g. "Men/Shoes/Sneakers")

    # ── Technical & Timestamps (optional) ──────────────────────────────
    creation_date: datetime | None = None  # When product was first added
    last_sync: datetime | None = None  # Last synchronization time

    # ── Pricing ─────────────────────────────────────────────────────────
    currency: str = "EUR"  # Default currency
    supplier_price_netto: float = 0.0  # Net supplier price
    supplier_price_brutto: float = 0.0  # Gross supplier price (with VAT)

    # ── Inventory (optional) ────────────────────────────────────────────
    stock_total: int = 0  # Total stock across all variants
    stock_reserved: int = 0  # Reserved for orders
    stock_available: int = 0  # Available for sale

    # ── Variants ────────────────────────────────────────────────────────
    # Variants are stored as a list - each has its own variant_uid
    # No merging based on names - each variant_uid is unique and independent
    variants: list[dict[str, Any]] = field(default_factory=list)

    # ── Media ───────────────────────────────────────────────────────────
    images: list[dict[str, Any]] = field(default_factory=list)
    # Each image dict: {"url": "...", "title": "...", "position": N, "ean": "..."}

    # ── EAN/GTIN ────────────────────────────────────────────────────────
    # EAN codes are stored EXACTLY as received from the supplier
    # No invention or generation of EAN codes
    ean_codes: list[str] = field(default_factory=list)  # List of EAN/GTIN strings

    # ── Processing ──────────────────────────────────────────────────────
    # Material, size table info
    material: str | None = None
    size_table_url: str | None = None  # Link to size table HTML
    size_table_text: str | None = None  # Human-readable size table text
    processing_time: int | None = None  # Max processing time in days

    # ── Normalized fields (derived/computed) ─────────────────────────────
    # These are computed from the raw data but stored for convenience
    _normalized_name: str | None = None  # Cached normalized name

    # ── Construction from Matterhorn data ─────────────────────────────────

    @classmethod
    def from_matterhorn(
        cls,
        supplier_id: str,
        matterhorn_data: dict[str, Any],
    ) -> "CanonicalProduct":
        """Create a CanonicalProduct from Matterhorn API response data.

        Args:
            supplier_id: The supplier/company ID
            matterhorn_data: Raw product data from Matterhorn REST API

        Returns:
            A new CanonicalProduct instance with Matterhorn data mapped
            to the canonical model.
        """
        # ── Extract supplier IDs (preserved exactly) ─────────────────────
        # supplier_product_id is the Matterhorn supplier product ID
        # It must NOT be confused with variant_uid
        supplier_product_id: str = matterhorn_data.get(
            "supplier_product_id", ""
        ).strip() or matterhorn_data.get("product_id", "").strip()

        # ── Extract core fields ─────────────────────────────────────────
        # SKU - could be supplier SKU or internally generated
        sku: str = (
            matterhorn_data.get("sku", "").strip()
            or matterhorn_data.get("articlenumber", "").strip()
            or ""
        )

        # Product name
        product_name: str = (
            matterhorn_data.get("product_name", "").strip()
            or matterhorn_data.get("name", "").strip()
            or matterhorn_data.get("title", "").strip()
        )

        # Name without supplier/article numbers
        name_without_number: str = _strip_numbers_from_name(product_name)

        # Description
        description: str = (
            matterhorn_data.get("description", "").strip()
            or matterhorn_data.get("long_description", "").strip()
            or ""
        )

        # Brand
        brand: str | None = (
            matterhorn_data.get("brand")
            or matterhorn_data.get("manufacturer")
        )

        # Brand ID
        brand_id: str | None = matterhorn_data.get("brand_id", "").strip() or None

        # Category
        category: str | None = (
            matterhorn_data.get("category", "").strip()
            or matterhorn_data.get("category_name", "").strip()
        )

        # Category ID
        category_id: str | None = matterhorn_data.get("category_id", "").strip() or None

        # Category path (hierarchical)
        category_path: str | None = matterhorn_data.get("category_path", "").strip() or None

        # Color
        color: str | None = matterhorn_data.get("color", "").strip() or None

        # ── Pricing ─────────────────────────────────────────────────────
        # Prices are stored as received - netto/brutto distinction
        # is made at the business logic level, not here
        supplier_price_netto: float = float(
            matterhorn_data.get("supplier_price_netto", "0") or 0
        )
        supplier_price_brutto: float = float(
            matterhorn_data.get("supplier_price_brutto", "0") or 0
        )

        # Currency
        currency: str = matterhorn_data.get("currency", "EUR") or "EUR"

        # ── Inventory ────────────────────────────────────────────────────
        stock_total: int = int(
            matterhorn_data.get("stock_total", "0") or 0
        )
        stock_reserved: int = int(
            matterhorn_data.get("stock_reserved", "0") or 0
        )
        stock_available: int = int(
            matterhorn_data.get("stock_available", "0") or 0
        )

        # ── Variants ────────────────────────────────────────────────────
        # Variants are critical - each gets its own variant_uid
        # NO merging based on product names!
        variants: list[dict[str, Any]] = _extract_variants(matterhorn_data)

        # ── EAN/GTIN codes ──────────────────────────────────────────────
        # stored EXACTLY as received - no invention
        ean_codes: list[str] = _extract_ean_codes(matterhorn_data)

        # ── Material & Size tables ────────────────────────────────────────
        material: str | None = matterhorn_data.get("material", "").strip() or None
        size_table_url: str | None = matterhorn_data.get(
            "size_table_url", ""
        ).strip() or None
        size_table_text: str | None = matterhorn_data.get(
            "size_table_text", ""
        ).strip() or None

        # ── Processing time ──────────────────────────────────────────────
        processing_time_raw: str | int | None = matterhorn_data.get(
            "max_processing_time", ""
        )
        processing_time: int | None = None
        if processing_time_raw is not None:
            try:
                processing_time = int(str(processing_time_raw).strip())
            except (ValueError, TypeError):
                processing_time = None

        # ── Product URL ──────────────────────────────────────────────────
        product_url: str | None = matterhorn_data.get("product_url", "").strip() or None

        # ── Construct the canonical product ───────────────────────────────
        product = cls(
            # Supplier identifiers
            supplier_id=supplier_id,
            supplier_product_id=supplier_product_id,
            # Core identifiers
            sku=sku,
            product_name=product_name,
            name_without_number=name_without_number,
            description=description,
            # Brand & Category
            brand=brand,
            brand_id=brand_id,
            category=category,
            category_id=category_id,
            category_path=category_path,
            color=color,
            # Technical
            creation_date=datetime.now(timezone.utc) if not matterhorn_data.get(
                "creation_date"
            ) else None,
            # Pricing
            currency=currency,
            supplier_price_netto=supplier_price_netto,
            supplier_price_brutto=supplier_price_brutto,
            # Inventory
            stock_total=stock_total,
            stock_reserved=stock_reserved,
            stock_available=stock_available,
            # Variants
            variants=variants,
            # Media
            images=[],  # Will be populated separately via Image Management
            # EAN codes
            ean_codes=ean_codes,
            # Technical
            material=material,
            size_table_url=size_table_url,
            size_table_text=size_table_text,
            processing_time=processing_time,
            product_url=product_url,
        )

        return product


# ── Helper Functions ──────────────────────────────────────────────────────


def _strip_numbers_from_name(name: str) -> str:
    """Remove article/supplier numbers from product name.

    Keeps the descriptive part of the name only.
    Example: "12345 Shirt Classic" -> "Shirt Classic"
    """
    if not name:
        return name

    # Remove leading pure-numeric sequences followed by space
    cleaned = re.sub(r"^\d+\s+", "", name)

    # Remove common article numbers in parentheses or at end
    cleaned = re.sub(r"\s*\(\d+\)\s*$", "", cleaned)
    cleaned = re.sub(r"\s-\d+$", "", cleaned)

    # Clean up multiple spaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    return cleaned


def _extract_variants(matterhorn_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract variant information from Matterhorn product data.

    CRITICAL: Each variant gets its own variant_uid.
    NO merging based on product names or visual similarity.
    """
    variants: list[dict[str, Any]] = []

    # Matterhorn may provide variants in different structures
    # 1. Direct "variants" key
    raw_variants = matterhorn_data.get("variants", []) or []

    # 2. "options" key with size/color combinations
    if not raw_variants:
        raw_variants = matterhorn_data.get("options", []) or []

    # 3. Individual variant fields on the product itself
    if not raw_variants:
        # Check for per-variant fields like "variant_uid", "size", "EAN"
        # If these exist at the product level, create a single composite variant
        has_variant_fields = any(
            matterhorn_data.get(f)
            for f in ("variant_uid", "size", "ean", "gtin", "color")
        )
        if has_variant_fields:
            # Create a single variant with whatever is available
            v: dict[str, Any] = {}
            for key in ("variant_uid", "size", "ean", "gtin", "color", "price",
                        "stock"):
                if matterhorn_data.get(key) is not None:
                    v[key] = matterhorn_data[key]
            if v:
                variants.append(v)
            return variants

    # Process each raw variant
    for i, raw_variant in enumerate(raw_variants):
        if not isinstance(raw_variant, dict):
            continue

        # Build variant dict with all available fields
        v: dict[str, Any] = {
            "variant_uid": raw_variant.get("variant_uid", f"var_{i}")
            .strip()
            or f"var_{i}",
            "variant_name": raw_variant.get("variant_name", "").strip() or f"Variant {i + 1}",
        }

        # Size - stored exactly as received
        v["size"] = raw_variant.get("size", "").strip() or None

        # EAN/GTIN for this variant - stored EXACTLY as received
        v["ean"] = raw_variant.get("ean", "").strip() or None
        v["gtin"] = raw_variant.get("gtin", "").strip() or None

        # Color
        v["color"] = raw_variant.get("color", "").strip() or None

        # Price for this variant (may differ from base product)
        v["price"] = raw_variant.get("price", "")
        v["price_netto"] = raw_variant.get("price_netto", "")
        v["price_brutto"] = raw_variant.get("price_brutto", "")

        # Stock for this variant
        v["stock"] = raw_variant.get("stock", "0")
        try:
            v["stock"] = int(v["stock"])
        except (ValueError, TypeError):
            v["stock"] = 0

        # Add any additional fields from the raw variant
        for key, value in raw_variant.items():
            if key not in v and value is not None:
                v[key] = value

        variants.append(v)

    # Deduplicate by variant_uid while preserving order
    seen: set[str] = set()
    unique_variants: list[dict[str, Any]] = []
    for v in variants:
        uid = v.get("variant_uid", "")
        if uid not in seen:
            seen.add(uid)
            unique_variants.append(v)

    return unique_variants


def _extract_ean_codes(matterhorn_data: dict[str, Any]) -> list[str]:
    """Extract EAN/GTIN codes from Matterhorn product data.

    EAN codes are stored EXACTLY as received from the supplier.
    No invention, no generation of fake EAN codes.
    """
    ean_codes: list[str] = []

    # Direct ean field
    ean = matterhorn_data.get("ean", "").strip()
    if ean:
        ean_codes.append(ean)

    # gtin field
    gtin = matterhorn_data.get("gtin", "").strip()
    if gtin and gtin not in ean_codes:
        ean_codes.append(gtin)

    # Search for EAN-like patterns in string fields (only if explicitly present)
    # We do NOT invent EANs from product names or other data
    # Only extract from fields that Matterhorn explicitly provides

    # Check variant EANs
    for v in matterhorn_data.get("variants", []) or []:
        if isinstance(v, dict):
            var_ean = v.get("ean", "").strip()
            if var_ean and var_ean not in ean_codes:
                ean_codes.append(var_ean)

    return ean_codes


# ── Mapping Utility Functions ────────────────────────────────────────────

def map_to_canonical(
    supplier_id: str,
    matterhorn_data: dict[str, Any],
) -> CanonicalProduct:
    """High-level convenience function to map Matterhorn data to canonical model.

    Args:
        supplier_id: The supplier/company identifier
        matterhorn_data: Raw product data from Matterhorn API

    Returns:
        A CanonicalProduct instance with all fields mapped appropriately.
    """
    return CanonicalProduct.from_matterhorn(supplier_id, matterhorn_data)


def merge_product_keep_supplier_ids(
    existing: CanonicalProduct,
    incoming: CanonicalProduct,
) -> CanonicalProduct:
    """Merge incoming product data into existing, preserving supplier IDs.

    When re-importing the same product (e.g. price/stock update),
    the original supplier_product_id and supplier_id are preserved
    from the existing product, while other fields are updated.

    Args:
        existing: The existing CanonicalProduct (has supplier IDs)
        incoming: The new data from Matterhorn

    Returns:
        Merged product with supplier IDs from existing, other fields
        potentially updated from incoming.
    """
    # Preserve supplier IDs from existing - NEVER overwrite these
    result = existing

    # Update non-supplier fields from incoming, but keep existing supplier IDs
    if incoming.supplier_product_id and not existing.supplier_product_id:
        result.supplier_product_id = incoming.supplier_product_id
    if incoming.supplier_id and not existing.supplier_id:
        result.supplier_id = incoming.supplier_id

    # Update other fields from incoming (overwrite existing values except supplier IDs)
    if incoming.product_name:
        result.product_name = incoming.product_name
    if incoming.description:
        result.description = incoming.description
    if incoming.brand:
        result.brand = incoming.brand
    if incoming.brand_id:
        result.brand_id = incoming.brand_id
    if incoming.category:
        result.category = incoming.category
    if incoming.category_id:
        result.category_id = incoming.category_id
    if incoming.material:
        result.material = incoming.material
    if incoming.size_table_text:
        result.size_table_text = incoming.size_table_text
    if incoming.processing_time is not None:
        result.processing_time = incoming.processing_time

    # Merge variants: add new variant UIDs without removing existing ones
    existing_uids: set[str] = {v.get("variant_uid", "") for v in result.variants}
    for v in incoming.variants:
        if v.get("variant_uid", "") not in existing_uids:
            result.variants.append(v)

    # Merge EAN codes (no duplicates)
    existing_eans: set[str] = set(result.ean_codes)
    for ean in incoming.ean_codes:
        if ean not in existing_eans:
            result.ean_codes.append(ean)

    # Update stock if incoming has stock data and existing doesn't have current stock
    # Update stock from incoming
    if incoming.stock_total > 0:
        result.stock_total = incoming.stock_total
    if incoming.stock_available > 0:
        result.stock_available = incoming.stock_available
    if incoming.stock_reserved >= 0:
        result.stock_reserved = incoming.stock_reserved

    # Update pricing from incoming
    if incoming.supplier_price_netto > 0:
        result.supplier_price_netto = incoming.supplier_price_netto
    if incoming.supplier_price_brutto > 0:
        result.supplier_price_brutto = incoming.supplier_price_brutto

    return result