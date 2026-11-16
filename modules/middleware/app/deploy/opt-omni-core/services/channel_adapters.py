"""Universal Multi-Channel Adapters for Omni-Core ERP & BI Enterprise Edition.

Provides clean export and mapping adapters for all major e-commerce platforms.
Each adapter reads platform-specific KI-Inhalte from the PostgreSQL channel_content table.
"""

from __future__ import annotations

import os

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services.omniroute_client import OmniRouteClient

logger = logging.getLogger("omni.channels")


class ChannelAdapter:
    """Base class for all channel adapters."""

    def __init__(self, client=None, db_session=None) -> None:
        self.client = client or OmniRouteClient(
            os.getenv('OMNI_ROUTE_BASE_URL', 'http://localhost:20128'),
            os.getenv('OMNI_ROUTE_API_KEY', 'test-key'),
        )
        self.db_session = db_session

    async def fetch_channel_content(self, sku: str, channel_name: str) -> Optional[Dict[str, Any]]:
        """Fetch channel-specific content from PostgreSQL channel_content table."""
        from database import get_connection

        if not self.db_session:
            async with get_connection() as db:
                self.db_session = db

        try:
            result = await self.db_session.execute(
                """
                SELECT sku, channel_name, optimized_title, description_html, 
                       seo_keywords, image_prompt, is_published
                FROM channel_content
                WHERE sku = :sku AND channel_name = :channel
                """,
                {"sku": sku, "channel": channel_name},
            )
            row = result.fetchone()
            if row:
                return {
                    "sku": row[0],
                    "channel_name": row[1],
                    "optimized_title": row[2] or "",
                    "description_html": row[3] or "",
                    "seo_keywords": json.loads(row[4]) if row[4] else [],
                    "image_prompt": row[5] or "",
                    "is_published": row[6],
                }
            return None
        except Exception as e:
            logger.error(f"Error fetching channel content for {sku}/{channel_name}: {e}")
            return None


class ShopifyAdapter(ChannelAdapter):
    """Shopify Adapter - GraphQL/REST API Export & CSV generation."""

    CHANNEL_NAME = "shopify"

    async def export_product_feed(self, skus: List[str] | None = None) -> Dict[str, Any]:
        """Export product feed for Shopify integration."""
        from database import get_connection

        async with get_connection() as db:
            if skus:
                placeholders = ", ".join([f" :sku{i}" for i in range(len(skus))])
                query = f"""
                    SELECT sku, title, optimized_title, description_html, 
                           seo_keywords, image_prompt, is_published
                    FROM channel_content
                    WHERE channel_name = 'shopify'
                    AND sku IN ({placeholders})
                """
                params = {f"sku{i}": sku for i, sku in enumerate(skus)}
            else:
                query = """
                    SELECT sku, title, optimized_title, description_html, 
                           seo_keywords, image_prompt, is_published
                    FROM channel_content
                    WHERE channel_name = 'shopify'
                """
                params = {}

            result = await db.execute(query, params)
            rows = result.fetchall()

            products = []
            for row in rows:
                products.append({
                    "id": row[0],
                    "title": row[1] or row[2] or row[0],
                    "body_html": row[3] or f"<h1>{row[1] or row[0]}</h1>",
                    "tags": json.loads(row[4]) if row[4] else [],
                    "image": row[5],
                    "published": row[6],
                })

            return {
                "success": True,
                "channel": self.CHANNEL_NAME,
                "total_products": len(products),
                "products": products,
                "export_format": "shopify_rest_v2024_01",
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }

    async def generate_shopify_graphql_mutation(
        self, sku: str, channel_content: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate Shopify GraphQL mutation for product update."""
        optimized_title = channel_content.get("optimized_title") or channel_content.get("title", sku)
        description = channel_content.get("description_html") or ""
        seo_keywords = json.dumps(channel_content.get("seo_keywords", [])) or ""

        mutation = f"""
        mutation productUpdate {{
            productUpdate(input: {{
                product: {{
                    id: gid("product", "{sku}")
                    title: "{optimized_title}"
                    bodyHTML: "{description}"
                    tags: {seo_keywords}
                }}
            }}) {{ product {{ id title }} userErrors {{ field message }} }}
        }}
        """

        return {
            "success": True,
            "sku": sku,
            "mutation": mutation.strip(),
            "format": "graphql_product_update",
        }


class AmazonAdapter(ChannelAdapter):
    """Amazon Adapter - Inventory & Price Feed XML generation."""

    CHANNEL_NAME = "amazon"

    async def export_inventory_feed(self, skus: List[str] | None = None) -> Dict[str, Any]:
        """Export Amazon inventory feed XML."""
        from database import get_connection

        async with get_connection() as db:
            if skus:
                placeholders = ",".join([f"'{sku}'" for sku in skus])
                query = f"""
                    SELECT sku, optimized_title, description_html, seo_keywords, image_prompt, is_published
                    FROM channel_content
                    WHERE channel_name = 'amazon'
                    AND sku IN ({placeholders})
                """
            else:
                query = """
                    SELECT sku, optimized_title, description_html, seo_keywords, image_prompt, is_published
                    FROM channel_content
                    WHERE channel_name = 'amazon'
                """

            result = await db.execute(query)
            rows = result.fetchall()

            xml_lines = ['<?xml version="1.0"?>']
            xml_lines.append('<AmazonFeed xmlns="https://feeds.amazon.com/feeds/2005-10-31">')

            for row in rows:
                xml_lines.append("  <Product>")
                xml_lines.append(f"    <SKU>{row[0]}</SKU>")
                xml_lines.append(f"    <Title>{row[1] or row[0]}</Title>")
                xml_lines.append(f"    <Description>{row[2] or ''}</Description>")
                xml_lines.append(f"    <Keywords>{json.dumps(row[4]) or ''}</Keywords>")
                xml_lines.append(f"    <ImageURL>{row[5] or ''}</ImageURL>")
                xml_lines.append(f"    <IsActive>{'true' if row[6] else 'false'}</IsActive>")
                xml_lines.append("  </Product>")

            xml_lines.append("</AmazonFeed>")

            return {
                "success": True,
                "channel": self.CHANNEL_NAME,
                "feed_format": "amazon_inventory_2005_10_31",
                "total_products": len(rows),
                "xml": "\n".join(xml_lines),
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }

    async def export_price_feed(self, skus: List[str] | None = None) -> Dict[str, Any]:
        """Export Amazon price feed with competitive pricing."""
        from database import get_connection

        async with get_connection() as db:
            if skus:
                placeholders = ",".join([f"'{sku}'" for sku in skus])
                query = f"""
                    SELECT sku, retail_price, supplier_price, buffered_stock
                    FROM products
                    WHERE sku IN ({placeholders})
                """
            else:
                query = """
                    SELECT sku, retail_price, supplier_price, buffered_stock
                    FROM products
                """

            result = await db.execute(query)
            rows = result.fetchall()

            xml_lines = ['<?xml version="1.0"?>']
            xml_lines.append('<PriceFeed xmlns="https://feeds.amazon.com/feeds/2005-10-31">')

            for row in rows:
                margin_pct = (
                    ((float(row[1]) - float(row[2])) / float(row[1])) * 100
                    if float(row[1]) > 0
                    else 0
                )
                xml_lines.append("  <Product>")
                xml_lines.append(f"    <SKU>{row[0]}</SKU>")
                xml_lines.append(f"    <Price>{float(row[1]):.2f}</Price>")
                xml_lines.append(f"    <MarginPercent>{margin_pct:.2f}</MarginPercent>")
                xml_lines.append(f"    <Stock>{int(row[3])}</Stock>")
                xml_lines.append("  </Product>")

            xml_lines.append("</PriceFeed>")

            return {
                "success": True,
                "channel": self.CHANNEL_NAME,
                "feed_format": "amazon_price_feed",
                "total_products": len(rows),
                "xml": "\n".join(xml_lines),
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }


class GoogleShoppingAdapter(ChannelAdapter):
    """Google Shopping Adapter - Merchant Center XML generation."""

    CHANNEL_NAME = "google_shopping"

    async def export_merchant_center_feed(self, skus: List[str] | None = None) -> Dict[str, Any]:
        """Export Google Merchant Center compliant XML feed."""
        from database import get_connection

        async with get_connection() as db:
            if skus:
                placeholders = ",".join([f"'{sku}'" for sku in skus])
                query = f"""
                    SELECT c.sku, c.optimized_title, c.description_html, c.seo_keywords, c.image_prompt, 
                           p.retail_price, p.supplier_price, c.is_published
                    FROM channel_content c
                    INNER JOIN products p USING (sku)
                    WHERE c.channel_name = 'google_shopping'
                    AND sku IN ({placeholders})
                """
            else:
                query = """
                    SELECT c.sku, c.optimized_title, c.description_html, c.seo_keywords, c.image_prompt,
                           p.retail_price, p.supplier_price, c.is_published
                    FROM channel_content c
                    INNER JOIN products p USING (sku)
                    WHERE c.channel_name = 'google_shopping'
                """

            result = await db.execute(query)
            rows = result.fetchall()

            xml_lines = ['<?xml version="1.0" encoding="UTF-8"?>']
            xml_lines.append('<g:feed xmlns:g="http://base.google.com/ns/1.0">')

            for row in rows:
                xml_lines.append("  <g:item>")

                title = row[1] or row[0] or "Product"
                desc = row[2] or title
                price = float(row[5]) if row[5] else 0.0

                xml_lines.append(f"    <g:title>{title}</g:title>")
                xml_lines.append(f"    <g:link>https://omni-core.local/product/{row[0]}</g:link>")
                xml_lines.append(f"    <g:description>{desc}</g:description>")
                xml_lines.append(f"    <g:price>{price:.2f} EUR</g:price>")
                xml_lines.append(f"    <g:availability>{'in stock' if row[7] else 'out of stock'}</g:availability>")
                xml_lines.append("    <g:condition>new</g:condition>")
                xml_lines.append(f"    <g:brand>{row[4] or 'Omni-Core'}</g:brand>")
                xml_lines.append(f"    <g:gtin>{row[3][1] if row[3] and len(row[3]) > 1 else 'Does not apply'}</g:gtin>")
                xml_lines.append(f"    <g:mpn>{row[0]}</g:mpn>")
                xml_lines.append("    <g:shipping_weight>1.000 kg</g:shipping_weight>")

                xml_lines.append("  </g:item>")

            xml_lines.append("</g:feed>")

            return {
                "success": True,
                "channel": self.CHANNEL_NAME,
                "feed_format": "google_shopping_merchant_center",
                "total_products": len(rows),
                "xml": "\n".join(xml_lines),
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }


class EbayAdapter(ChannelAdapter):
    """eBay Adapter - Inventory Management Mapping and Feed generation."""

    CHANNEL_NAME = "ebay"

    async def export_inventory_feed(self, skus: List[str] | None = None) -> Dict[str, Any]:
        """Export eBay inventory status feed."""
        from database import get_connection

        async with get_connection() as db:
            if skus:
                placeholders = ",".join([f"'{sku}'" for sku in skus])
                query = f"""
                    SELECT sku, optimized_title, description_html, seo_keywords, image_prompt, is_published
                    FROM channel_content
                    WHERE channel_name = 'ebay'
                    AND sku IN ({placeholders})
                """
            else:
                query = """
                    SELECT sku, optimized_title, description_html, seo_keywords, image_prompt, is_published
                    FROM channel_content
                    WHERE channel_name = 'ebay'
                """

            result = await db.execute(query)
            rows = result.fetchall()

            items = []
            for row in rows:
                items.append({
                    "sku": row[0],
                    "title": row[1] or row[0],
                    "quantity": int(row[5]) if row[5] else 0,
                    "price": float(row[1]) if len(row) > 1 and row[1] else 0.0,
                    "condition": "New" if row[6] else "Used",
                    "description": row[2] or "",
                    "image_url": row[5] if len(row) > 5 else "",
                })

            return {
                "success": True,
                "channel": self.CHANNEL_NAME,
                "total_items": len(items),
                "items": items,
                "feed_format": "ebay_inventory",
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }


class ZalandoAdapter(ChannelAdapter):
    """Zalando/About You Adapter - mode-spezifische Feed-Transformation."""

    CHANNEL_NAME = "zalando"

    async def export_zalando_feed(self, skus: List[str] | None = None) -> Dict[str, Any]:
        """Export Zalando-specific fashion feed with attribute mapping."""
        from database import get_connection

        async with get_connection() as db:
            if skus:
                placeholders = ",".join([f"'{sku}'" for sku in skus])
                query = f"""
                    SELECT c.sku, c.optimized_title, c.description_html, c.seo_keywords, c.image_prompt,
                           c.is_published, p.category, p.retail_price, p.supplier_price
                    FROM channel_content c
                    INNER JOIN products p USING (sku)
                    WHERE c.channel_name = 'zalando'
                    AND sku IN ({placeholders})
                """
            else:
                query = """
                    SELECT c.sku, c.optimized_title, c.description_html, c.seo_keywords, c.image_prompt,
                           c.is_published, p.category, p.retail_price, p.supplier_price
                    FROM channel_content c
                    INNER JOIN products p USING (sku)
                    WHERE c.channel_name = 'zalando'
                """

            result = await db.execute(query)
            rows = result.fetchall()

            items = []
            for row in rows:
                attributes = {}
                if row[3] and len(row[3]) > 0:
                    kw_lower = [k.lower() for k in row[3]]
                    if any("sommer" in k or "summer" in k for k in kw_lower):
                        attributes["season"] = "SS2025"
                    if any("party" in k or "abend" in k or "elegant" in k for k in kw_lower):
                        attributes["occasion"] = "formal"
                    if any("casual" in k or "täglich" in k or "daily" in k for k in kw_lower):
                        attributes["style"] = "casual"

                items.append({
                    "sku": row[0],
                    "title": row[1] or row[0] or "Product",
                    "price": float(row[6]) if row[6] else 0.0,
                    "currency": "EUR",
                    "description": row[2] or "",
                    "image_url": row[4] or "",
                    "is_active": row[5],
                    "attributes": attributes,
                    "gender": self._determine_gender(row[7] or "") if row[7] else "unisex",
                    "age_group": "adult",
                    "material": self._extract_material(row[3] or []) or "unknown",
                })

            return {
                "success": True,
                "channel": self.CHANNEL_NAME,
                "total_items": len(items),
                "items": items,
                "feed_format": "zalando_fashion_feed",
                "exported_at": datetime.now(timezone.utc).isoformat(),
            }

    @staticmethod
    def _determine_gender(category: str) -> str:
        """Determine gender based on product category."""
        cat_lower = category.lower() if category else ""
        if any(word in cat_lower for word in ["herren", "man", "male"]):
            return "men"
        elif any(word in cat_lower for word in ["damen", "woman", "female"]):
            return "women"
        return "unisex"

    @staticmethod
    def _extract_material(keywords: List[str]) -> str | None:
        """Extract material information from keywords."""
        if not keywords:
            return None
        material_map = {
            "baumwolle": "cotton",
            "polyester": "polyester",
            "seide": "silk",
            "leder": "leather",
            "denim": "denim",
        }
        for kw in keywords:
            kw_lower = kw.lower() if kw else ""
            if kw_lower in material_map:
                return material_map[kw_lower]
        return None


# Convenience factory functions
def get_shopify_adapter(client=None, db_session=None):
    return ShopifyAdapter(client=client, db_session=db_session)

def get_amazon_adapter(client=None, db_session=None):
    return AmazonAdapter(client=client, db_session=db_session)

def get_google_shopping_adapter(client=None, db_session=None):
    return GoogleShoppingAdapter(client=client, db_session=db_session)

def get_ebay_adapter(client=None, db_session=None):
    return EbayAdapter(client=client, db_session=db_session)

def get_zalando_adapter(client=None, db_session=None):
    return ZalandoAdapter(client=client, db_session=db_session)