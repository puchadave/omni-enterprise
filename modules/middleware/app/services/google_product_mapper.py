"""Google Merchant Center Product Mapping & Validation Engine.

Maps canonical product data to Google Merchant Center product format
and validates all required fields before export.

Required Google Shopping fields:
- offerId (unique product identifier)
- title (product title, max 150 chars)
- description (product description)
- link (product URL)
- imageLink (product image URL)
- price (price with currency)
- availability (in stock, out of stock, etc.)
- condition (new, used, refurbished)
- brand
- googleProductCategory
- itemGroupId (for variants)

Optional but recommended:
- gtin
- mpn
- size
- color
- gender
- ageGroup
- shipping
- shippingWeight
- shippingLength/Width/Height
- expirationDate
- custom labels
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Any
from uuid import uuid4

from .canonical_product_mapping import CanonicalProduct

logger = logging.getLogger(__name__)


# ── Google Product Category Mapping ──────────────────────────────────────

GOOGLE_PRODUCT_CATEGORIES: dict[str, str] = {
    "Apparel & Accessories": "1664",
    "Clothing": "1734",
    "Shoes": "2582",
    "Fashion Accessories": "1666",
    "Jewelry": "1678",
    "Watches": "1715",
    "Handbags": "1670",
    "Luggage": "2796",
    "Home & Garden": "1257",
    "Electronics": "5489",
    "Sports": "5875",
    "Beauty": "1263",
    "Toys & Games": "1174",
    "Books": "1172",
    "Automotive": "1260",
    "Food & Groceries": "1736",
    "Health": "1259",
    "Office Supplies": "5873",
    "Pet Supplies": "1258",
    "Hardware": "1175",
    "Crafts": "1168",
}


# ── Availability Mapping ─────────────────────────────────────────────────

AVAILABILITY_MAP: dict[str, str] = {
    "in_stock": "in stock",
    "available": "in stock",
    "out_of_stock": "out of stock",
    "unavailable": "out of stock",
    "preorder": "preorder",
    "backorder": "backorder",
}


# ── Condition Mapping ────────────────────────────────────────────────────

CONDITION_MAP: dict[str, str] = {
    "new": "new",
    "used": "used",
    "refurbished": "refurbished",
}


# ── Product Mapper ───────────────────────────────────────────────────────

class GoogleProductMapper:
    """Maps canonical product data to Google Merchant Center format."""

    def __init__(self, country: str = "DE", language: str = "de",
                 currency: str = "EUR") -> None:
        self.country: str = country
        self.language: str = language
        self.currency: str = currency

    def map_product(self, product: CanonicalProduct,
                    shop_url: str | None = None,
                    ai_title: str | None = None,
                    ai_description: str | None = None,
                    ai_image_url: str | None = None) -> dict[str, Any]:
        """Map a canonical product to Google Merchant Center product format.

        Args:
            product: Canonical product data
            shop_url: Base URL of the shop for product links
            ai_title: AI-optimized title (overrides product title)
            ai_description: AI-optimized description (overrides product description)
            ai_image_url: AI-generated image URL (overrides supplier images)

        Returns:
            Google Merchant Center product dict
        """
        # Generate offerId from SKU
        offer_id = f"{product.sku}-{self.country}"

        # Build product link
        product_link = ""
        if shop_url:
            product_link = f"{shop_url.rstrip('/')}/product/{product.sku}"
        elif product.product_url:
            product_link = product.product_url

        # Determine image URL (prefer AI-generated, then supplier images)
        image_link = ai_image_url or ""
        if not image_link and product.images:
            # Use first supplier image
            first_image = product.images[0]
            if isinstance(first_image, dict):
                image_link = first_image.get("url", "")
            else:
                image_link = str(first_image)

        # Determine title (prefer AI-optimized)
        title = ai_title or product.product_name or "Produkt"
        title = title[:150]  # Google limit

        # Determine description (prefer AI-optimized)
        description = ai_description or product.description or ""
        description = description[:5000]  # Google limit

        # Map category
        google_category = self._map_category(product.category)

        # Build shipping info
        shipping = self._build_shipping(product)

        # Build variant attributes
        variants = self._build_variants(product)

        # Build the product dict
        product_dict: dict[str, Any] = {
            "offerId": offer_id,
            "title": title,
            "description": description,
            "link": product_link,
            "imageLink": image_link,
            "contentLanguage": self.language,
            "feedCountry": self.country,
            "availability": self._map_availability(product.stock_available),
            "condition": "new",
            "price": {
                "value": f"{float(product.supplier_price_netto):.2f}",
                "currency": self.currency,
            },
            "brand": product.brand or "Unbekannt",
            "googleProductCategory": google_category,
            "itemGroupId": product.supplier_product_id or product.sku,
        }

        # Add optional fields
        if product.ean_codes:
            eans = product.ean_codes
            if eans:
                product_dict["gtin"] = eans[0]

        if product.supplier_product_id:
            product_dict["mpn"] = product.supplier_product_id

        # Add variant attributes
        if variants.get("size"):
            product_dict["size"] = variants["size"]
        if variants.get("color"):
            product_dict["color"] = variants["color"]
        if variants.get("gender"):
            product_dict["gender"] = variants["gender"]
        if variants.get("age_group"):
            product_dict["ageGroup"] = variants["age_group"]

        # Add shipping
        if shipping:
            product_dict["shipping"] = shipping

        # Add expiration date (30 days from now)
        expiration = datetime.now(timezone.utc) + timedelta(days=30)
        product_dict["expirationDate"] = expiration.strftime("%Y-%m-%dT%H:%M:%SZ")

        # Add custom labels
        custom_labels = {}
        if product.category:
            custom_labels["customLabel0"] = product.category[:100]
        if product.brand:
            custom_labels["customLabel1"] = product.brand[:100]
        if custom_labels:
            product_dict["customLabels"] = custom_labels

        return product_dict

    # ── Mapping Helpers ─────────────────────────────────────────────────

    def _map_category(self, category: str | None) -> str:
        """Map product category to Google product category ID."""
        if not category:
            return "1664"  # Default: Apparel & Accessories

        cat_lower = category.lower().strip()
        for key, value in GOOGLE_PRODUCT_CATEGORIES.items():
            if key.lower() in cat_lower or cat_lower in key.lower():
                return value

        return "1664"  # Default fallback

    def _map_availability(self, stock: int) -> str:
        """Map stock level to Google availability status."""
        if stock > 0:
            return "in stock"
        return "out of stock"

    def _build_shipping(self, product: CanonicalProduct) -> list[dict[str, Any]] | None:
        """Build shipping information."""
        # Default shipping for Germany
        shipping = {
            "country": self.country,
            "service": "Standardversand",
            "price": {
                "value": "4.95",
                "currency": self.currency,
            },
        }

        # Free shipping threshold
        if product.supplier_price_netto >= 50.0:
            shipping["price"] = {"value": "0.00", "currency": self.currency}

        return [shipping]

    def _build_variants(self, product: CanonicalProduct) -> dict[str, str]:
        """Build variant attributes from product data."""
        variants: dict[str, str] = {}

        if product.color:
            variants["color"] = product.color

        if product.size_table_text:
            variants["size"] = product.size_table_text[:50]

        # Extract size from variants if available
        for variant in product.variants:
            if variant.get("size"):
                variants["size"] = str(variant["size"])[:50]
            if variant.get("color"):
                variants["color"] = str(variant["color"])
            break  # Use first variant

        # Determine gender from category/product name
        name_lower = (product.product_name or "").lower()
        category_lower = (product.category or "").lower()
        if any(term in name_lower or term in category_lower for term in ["damen", "women", "frau"]):
            variants["gender"] = "female"
        elif any(term in name_lower or term in category_lower for term in ["herren", "men", "mann"]):
            variants["gender"] = "male"
        elif any(term in name_lower or term in category_lower for term in ["kind", "child", "baby"]):
            variants["gender"] = "unisex"
            variants["age_group"] = "infant"
        else:
            variants["gender"] = "unisex"
            variants["age_group"] = "adult"

        return variants


# ── Product Validator ────────────────────────────────────────────────────

class GoogleProductValidator:
    """Validates products before Google Merchant Center export."""

    def __init__(self, country: str = "DE", language: str = "de",
                 currency: str = "EUR") -> None:
        self.country: str = country
        self.language: str = language
        self.currency: str = currency

    def validate(self, product: CanonicalProduct,
                 google_product: dict[str, Any],
                 shop_url: str | None = None) -> dict[str, Any]:
        """Validate a Google product before export.

        Returns:
            Dict with 'valid' bool, 'errors' list, 'warnings' list,
            and 'score' (0-100)
        """
        errors: list[str] = []
        warnings: list[str] = []

        # Required fields validation
        if not google_product.get("offerId"):
            errors.append("offerId is required")
        if not google_product.get("title"):
            errors.append("title is required")
        elif len(google_product["title"]) > 150:
            errors.append("title exceeds 150 characters")
        if not google_product.get("description"):
            warnings.append("description is missing")
        if not google_product.get("link"):
            errors.append("link (product URL) is required")
        elif not self._is_valid_url(google_product["link"]):
            errors.append("link is not a valid URL")
        if not google_product.get("imageLink"):
            warnings.append("imageLink is missing - product may be disapproved")
        elif not self._is_valid_url(google_product["imageLink"]):
            errors.append("imageLink is not a valid URL")
        if not google_product.get("price"):
            errors.append("price is required")
        else:
            price = google_product["price"]
            if not price.get("value"):
                errors.append("price.value is required")
            if not price.get("currency"):
                errors.append("price.currency is required")
        if not google_product.get("availability"):
            errors.append("availability is required")
        if not google_product.get("condition"):
            errors.append("condition is required")
        if not google_product.get("brand"):
            warnings.append("brand is missing")
        if not google_product.get("googleProductCategory"):
            warnings.append("googleProductCategory is missing")

        # GTIN/MPN validation (recommended but not required)
        if not google_product.get("gtin"):
            warnings.append("GTIN/EAN is missing - recommended for better matching")
        if not google_product.get("mpn"):
            warnings.append("MPN is missing - recommended for better matching")

        # Variant validation
        if not google_product.get("size"):
            warnings.append("size is missing - recommended for apparel")
        if not google_product.get("color"):
            warnings.append("color is missing - recommended for apparel")

        # Calculate score
        total_checks = 12
        failed_checks = len(errors) + len(warnings)
        score = max(0, int((total_checks - failed_checks) / total_checks * 100))

        valid = len(errors) == 0

        return {
            "valid": valid,
            "errors": errors,
            "warnings": warnings,
            "score": score,
            "country": self.country,
            "language": self.language,
            "currency": self.currency,
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ── Utilities ───────────────────────────────────────────────────────

    @staticmethod
    def _is_valid_url(url: str) -> bool:
        """Basic URL validation."""
        pattern = re.compile(
            r"^https?://"  # http:// or https://
            r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|"  # domain
            r"localhost|"  # localhost
            r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"  # IP
            r"(?::\d+)?"  # optional port
            r"(?:/?|[/?]\S+)$", re.IGNORECASE,
        )
        return bool(pattern.match(url))


# ── Export Record Builder ────────────────────────────────────────────────

def build_export_record(account_id: str, product_sku: str,
                         google_product: dict[str, Any],
                         validation_result: dict[str, Any],
                         offer_id: str | None = None) -> dict[str, Any]:
    """Build a Google product export record for the database.

    Args:
        account_id: Google account ID
        product_sku: Internal product SKU
        google_product: Mapped Google product data
        validation_result: Validation result dict
        offer_id: Optional offer ID override

    Returns:
        Export record dict for database insertion
    """
    export_id = str(uuid4())
    offer = offer_id or google_product.get("offerId", product_sku)

    return {
        "export_id": export_id,
        "account_id": account_id,
        "product_sku": product_sku,
        "offer_id": offer,
        "status": "pending",
        "payload_json": google_product,
        "response_json": {},
        "error_message": "",
        "retry_count": 0,
        "max_retries": 3,
        "validation_score": validation_result.get("score", 0),
        "validation_errors": validation_result.get("errors", []),
        "validation_warnings": validation_result.get("warnings", []),
    }