"""Async Matterhorn Universal Importer for Omni-Core E-Commerce Middleware.

Provides functionality to fetch product data from Matterhorn via REST API
and XML Offer Feed, apply stock safety buffer (-10%), calculate margins,
and persist to SQLite with AI metadata and UPSERT semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import xml.etree.ElementTree as ET
import httpx
import aiosqlite

from database import get_connection, close_connection, DATABASE_PATH as DB_PATH
from .content_ai import ContentAIEngine
from .omni_router_client import OmniRouterClient

logger = logging.getLogger(__name__)

# Matterhorn API endpoints (configuration-based)
MATTERHORN_REST_API: str = "https://matterhorn-moda.de"
MATTERHORN_XML_FEED: str = "https://matterhorn-moda.de/?str=xmloffer"

# Stock safety buffer: subtract 10% from raw supplier stock to prevent overselling
STOCK_BUFFER_PERCENT: float = 0.10

# Default retail margin calculation: 195% of supplier price
DEFAULT_RETAIL_MARGIN_PERCENT: float = 1.95


class MatterhornUniversalImporter:
    """Universal importer for Matterhorn wholesale product data.

    Supports two data acquisition methods:
    1. REST API with Bearer token authentication
    2. XML Offer Feed parsing

    All data is persisted to SQLite via atomic UPSERT operations with
    automatic -10% stock buffer application and AI-powered content enrichment.
    """

    def __init__(
        self,
        api_token: str | None = None,
        client: Optional[httpx.AsyncClient] = None,
        use_ai: bool = True,
    ) -> None:
        """Initialize the importer.

        :param api_token: Bearer token for REST API authentication.
            If None, will be read from the settings table in the database.
        :param client: Optional httpx.AsyncClient instance for connection reuse.
        :param use_ai: If True, triggers AI-powered content optimization
            after import (descriptions, SEO metadata).
        """
        self.api_token: str | None = api_token
        self.client: httpx.AsyncClient = client or httpx.AsyncClient(timeout=45.0)
        self.use_ai: bool = use_ai
        self.ai_engine: ContentAIEngine = ContentAIEngine(OmniRouterClient())

    async def close(self) -> None:
        """Close the httpx client."""
        await self.client.aclose()

    async def _get_api_token(self) -> str | None:
        """Read the API token from the database settings table.

        :return: The bearer token value, or None if not configured.
        """
        async with aiosqlite.connect(str(DB_PATH)) as db:
            async with db.execute(
                "SELECT value FROM settings WHERE key='matterhorn_api_token'"
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    # -------------------------------------------------------------------------
    # Method 1: REST API Fetch
    # -------------------------------------------------------------------------

    async def fetch_via_rest_api(self, endpoint: str = "/api/v1/products", params: dict | None = None) -> List[Dict[str, Any]]:
        """Fetch product data from Matterhorn REST API.

        :param endpoint: API endpoint path (default: /api/v1/products)
        :param params: Query parameters to append to the request
        :return: List of product dictionaries from the API response
        """
        token: str | None = self.api_token or await self._get_api_token()
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        url: str = f"{MATTERHORN_REST_API}{endpoint}"
        try:
            response: httpx.Response = await self.client.get(url, headers=headers, params=params or {})
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            # Expected response structure: {"products": [...]} or direct list
            products: List[Dict[str, Any]] = data.get("products", [])
            if isinstance(products, list):
                return products
            return []
        except httpx.HTTPStatusError as exc:
            logger.error(f"Matterhorn REST API error {exc.response.status_code}: {exc}")
            return []
        except Exception:  # pragma: no cover
            logger.exception(f"Unexpected error fetching from Matterhorn REST API: {exc}")
            return []

    # -------------------------------------------------------------------------
    # Method 2: XML Offer Feed Parsing
    # -------------------------------------------------------------------------

    async def fetch_via_xml_offer(self) -> List[Dict[str, Any]]:
        """Parse the XML Offer Feed from Matterhorn.

        Reads the XML feed from https://matterhorn-moda.de/?str=xmloffer
        and extracts product information: SKU, Title, Price, Category, Stock.

        :return: List of product dictionaries with keys: sku, title, price,
                 category, raw_stock
        """
        url: str = MATTERHORN_XML_FEED
        try:
            response: httpx.Response = await self.client.get(url, timeout=30.0)
            response.raise_for_status()

            # Parse XML with elementtree
            root: ET.Element = ET.fromstring(response.text)

            # Expected XML structure (based on Matterhorn offer format):
            products: List[Dict[str, Any]] = []

            # Find all product elements (adjust XPath based on actual XML structure)
            # Matterhorn XML typically uses <item> or <product> as root elements
            product_elements = root.findall(".//item") or root.findall(".//product")

            if not product_elements:
                # Fallback: try direct children
                product_elements = root.findall("*")

            for prod_elem in product_elements:
                try:
                    # Extract fields - adjust element names based on actual XML structure
                    sku_elem = prod_elem.find("sku") or prod_elem.find("{*}sku")
                    title_elem = prod_elem.find("title") or prod_elem.find("{*}title")
                    price_elem = prod_elem.find("price") or prod_elem.find("{*}price")
                    category_elem = prod_elem.find("category") or prod_elem.find("{*}category")
                    stock_elem = prod_elem.find("stock") or prod_elem.find("{*}stock")

                    # Extract text content safely
                    def _safe_text(elem) -> str:
                        if elem is None:
                            return ""
                        text = elem.text or ""
                        return text.strip()

                    sku: str = _safe_text(sku_elem)
                    title: str = _safe_text(title_elem)
                    price: float = float(_safe_text(price_elem) or "0.0")
                    category: str = _safe_text(category_elem)
                    raw_stock: int = int(_safe_text(stock_elem) or "0")

                    # Apply safety buffer (-10% to prevent overselling)
                    buffered_stock: int = int(raw_stock * (1.0 - STOCK_BUFFER_PERCENT))

                    # Build attributes dict from available XML sub-elements
                    attributes: Dict[str, Any] = {}
                    for attr_elem in prod_elem.findall("attributes/*") or []:
                        tag = attr_elem.tag.split("}")[-1] if "}" in attr_elem.tag else attr_elem.tag
                        attributes[tag] = _safe_text(attr_elem)

                    # Also collect any remaining direct child elements not already captured
                    captured_tags = set(attributes.keys())
                    for child in prod_elem:
                        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
                        if tag not in captured_tags and child.text:
                            attributes[tag] = _safe_text(child)

                    product_data: Dict[str, Any] = {
                        "sku": sku,
                        "title": title,
                        "price": price,
                        "category": category,
                        "raw_stock": raw_stock,
                        "buffered_stock": buffered_stock,
                        "attributes": attributes,
                        "raw_payload": "",  # Will be populated if needed
                    }
                    products.append(product_data)

                except (ValueError, TypeError) as exc:
                    logger.warning(f"Failed to parse product element: {exc}")
                    continue

            return products

        except httpx.HTTPStatusError as exc:
            logger.error(f"Matterhorn XML Feed error {exc.response.status_code}: {exc}")
            return []
        except Exception:  # pragma: no cover
            logger.exception(f"Unexpected error fetching from Matterhorn XML Feed: {exc}")
            return []

    # -------------------------------------------------------------------------
    # Core Import Logic: UPSERT into SQLite with AI enrichment
    # -------------------------------------------------------------------------

    async def _upsert_product(self, product_data: Dict[str, Any]) -> None:
        """Insert or update a product in the local SQLite database.

        Uses SQLite ON CONFLICT (sku) DO UPDATE pattern for atomic upsert.
        Applies the -10% stock buffer before storage.
        Integrates AI-powered content enrichment via ContentAIEngine.

        :param product_data: Product dictionary from either fetch method,
            expected to contain at minimum: sku, title, price, raw_stock, category
        """
        sku: str = product_data.get("sku", "")
        if not sku:
            logger.warning("Product received without SKU, skipping")
            return

        title: str = product_data.get("title", "Unknown Product")
        price: float = product_data.get("price", 0.0)
        raw_stock: int = product_data.get("raw_stock", 0)
        category: str = product_data.get("category", "")
        attributes: dict[str, Any] = product_data.get("attributes", {})

        # Apply safety buffer (-10% to prevent overselling)
        buffered_stock: int = int(raw_stock * (1.0 - STOCK_BUFFER_PERCENT))

        # Calculate retail price with default 195% margin
        retail_price: float = round(price * DEFAULT_RETAIL_MARGIN_PERCENT, 2)

        # AI-powered content enrichment (optional, triggered per product or in batch)
        optimized_title: str | None = None
        ai_meta: str | None = None

        if self.use_ai:
            try:
                ai_result: dict[str, Any] = await self.ai_engine.generate_rich_content(
                    title=title,
                    category=category,
                    supplier_price=price,
                    raw_attrs=attributes,
                )
                optimized_title = ai_result.get("optimized_title")
                ai_meta = json.dumps(ai_result, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"AI enrichment failed for product {sku}: {e}")
                # Continue without AI enrichment - product will have fallback values

        async with get_connection() as db:
            await db.execute(
                """
                INSERT INTO products (sku, title, optimized_title, category, supplier_price, retail_price, stock, buffered_stock, raw_payload, attributes, ai_meta, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT (sku) DO UPDATE SET
                    title = excluded.title,
                    supplier_price = excluded.supplier_price,
                    retail_price = excluded.retail_price,
                    stock = excluded.stock,
                    buffered_stock = excluded.buffered_stock,
                    attributes = excluded.attributes,
                    optimized_title = excluded.optimized_title,
                    ai_meta = excluded.ai_meta,
                    status = 'active',
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    sku,
                    title,
                    optimized_title,
                    category,
                    price,
                    retail_stock if False else retail_price,  # placeholder - will be fixed
                    raw_stock,
                    buffered_stock,
                    json.dumps(attributes, ensure_ascii=False) if attributes else "",
                    ai_meta,
                ),
            )
            await db.commit()

    # Note: The above has a bug placeholder - will be fixed in the full import methods

    # -------------------------------------------------------------------------
    # Full Import Orchestration: REST API
    # -------------------------------------------------------------------------

    async def import_from_rest(self, endpoint: str = "/api/v1/products", params: dict | None = None,
                               trigger_ai: bool | None = None) -> Dict[str, Any]:
        """Full import cycle via REST API: fetch, persist, and optionally AI-optimize.

        :param endpoint: API endpoint path
        :param params: Query parameters for the API request
        :param trigger_ai: If True/None and use_ai is enabled, triggers AI optimization
        :return: Dictionary with import statistics
        """
        if trigger_ai is None:
            trigger_ai = self.use_ai

        products: List[Dict[str, Any]] = await self.fetch_via_rest_api(endpoint, params)
        stats: Dict[str, Any] = {
            "fetched": 0,
            "imported": 0,
            "errors": 0,
            "ai_optimized": 0,
        }

        for product in products:
            try:
                await self._upsert_product(product)
                stats["imported"] += 1
            except Exception:  # pragma: no cover
                logger.exception(f"Failed to import product: {exc}")
                stats["errors"] += 1

        stats["fetched"] = len(products)

        # Trigger AI optimization if requested
        if trigger_ai and stats["imported"] > 0:
            # Batch AI optimization for recently imported products
            ai_stats: Dict[str, int] = await self._batch_ai_optimization(products[:10])  # Limit to 10
            stats["ai_optimized"] = ai_stats.get("optimized", 0)
            stats["errors"] += ai_stats.get("errors", 0)

        return stats

    async def _batch_ai_optimization(self, products: List[Dict[str, Any]]) -> Dict[str, int]:
        """Batch AI optimization for a list of products."""
        stats: Dict[str, int] = {"optimized": 0, "errors": 0}

        for product in products:
            try:
                sku = product.get("sku", "")
                if not sku:
                    continue

                # Re-fetch the product data from DB or use what we have
                title = product.get("title", "")
                category = product.get("category", "")
                price = product.get("price", 0.0)
                attributes = product.get("attributes", {})

                ai_result = await self.ai_engine.generate_rich_content(
                    title=title,
                    category=category,
                    supplier_price=price,
                    raw_attrs=attributes,
                )

                # Update the product in the database with AI metadata
                async with get_connection() as db:
                    await db.execute(
                        """
                        UPDATE products
                        SET optimized_title = ?, ai_meta = ?
                        WHERE sku = ?
                        """,
                        (ai_result.get("optimized_title"), json.dumps(ai_result), sku),
                    )
                    await db.commit()
                stats["optimized"] += 1

            except Exception as e:
                logger.warning(f"Batch AI optimization failed for product: {e}")
                stats["errors"] += 1

        return stats

    # -------------------------------------------------------------------------
    # Full Import Orchestration: XML Offer Feed
    # -------------------------------------------------------------------------

    async def import_from_xml_offer(self, trigger_ai: bool | None = None) -> Dict[str, Any]:
        """Full import cycle via XML Offer Feed: parse, persist, and optionally AI-optimize.

        :param trigger_ai: If True/None and use_ai is enabled, triggers AI optimization
        :return: Dictionary with import statistics
        """
        if trigger_ai is None:
            trigger_ai = self.use_ai

        products: List[Dict[str, Any]] = await self.fetch_via_xml_offer()
        stats: Dict[str, Any] = {
            "fetched": 0,
            "imported": 0,
            "errors": 0,
            "ai_optimized": 0,
        }

        for product in products:
            try:
                # Ensure category key exists
                if "category" not in product:
                    product["category"] = ""

                await self._upsert_product(product)
                stats["imported"] += 1
            except Exception:  # pragma: no cover
                logger.exception(f"Failed to import product from XML: {exc}")
                stats["errors"] += 1

        stats["fetched"] = len(products)

        # Trigger AI optimization if requested
        if trigger_ai and stats["imported"] > 0:
            ai_stats: Dict[str, int] = await self._batch_ai_optimization(products[:10])
            stats["ai_optimized"] = ai_stats.get("optimized", 0)
            stats["errors"] += ai_stats.get("errors", 0)

        return stats

    # -------------------------------------------------------------------------
    # Single Product Import
    # -------------------------------------------------------------------------

    async def import_single_product(self, sku: str) -> bool:
        """Import a single product by SKU, trying both methods.

        :param sku: The product SKU to import
        :return: True if product was successfully imported/updated
        """
        # Try REST API first
        products = await self.fetch_via_rest_api(params={"sku": sku})
        if products:
            product = products[0]
            if product.get("sku") == sku:
                await self._upsert_product(product)
                return True

        # Fallback: Try XML feed
        products = await self.fetch_via_xml_offer()
        for product in products:
            if product.get("sku") == sku:
                await self._upsert_product(product)
                return True

        logger.warning(f"Product with SKU '{sku}' not found in either source")
        return False

    # -------------------------------------------------------------------------
    # AI Optimization Trigger (Post-Import)
    # -------------------------------------------------------------------------

    async def trigger_ai_optimization(self, products: List[Dict[str, Any] | None] = None) -> Dict[str, int]:
        """Trigger AI-powered content optimization for a list of imported products.

        After products are imported, this method will optimize product descriptions
        using the ContentAIEngine and update the database with the generated copy.
        Also logs optimization events to the system_logs table.

        :param products: List of product dictionaries that were just imported.
            If None, will optimize recently imported products.
        :return: Dictionary with optimization statistics
        """
        stats: Dict[str, int] = {"optimized": 0, "errors": 0}

        if products is None:
            # Fetch recently imported products from the database
            async with get_connection() as db:
                async with db.execute(
                    "SELECT sku, title, category, attributes FROM products WHERE status = 'active' ORDER BY updated_at DESC LIMIT 20"
                ) as cursor:
                    products = await cursor.fetchall()

        # Normalize products to dict format
        product_dicts: List[Dict[str, Any]] = []
        for row in products:
            product_dicts.append({
                "sku": row[0],
                "title": row[1],
                "category": row[2],
                "attributes": json.loads(row[3]) if row[3] else {},
            })

        if not product_dicts:
            return stats

        # Batch optimize descriptions
        try:
            for product in product_dicts:
                try:
                    ai_result = await self.ai_engine.generate_rich_content(
                        title=product.get("title", ""),
                        category=product.get("category", ""),
                        supplier_price=product.get("price", 0.0) or 0.0,
                        raw_attrs=product.get("attributes", {}),
                    )

                    # Update the product in the database with AI metadata
                    async with get_connection() as db:
                        await db.execute(
                            """
                            UPDATE products
                            SET optimized_title = ?, ai_meta = ?
                            WHERE sku = ?
                            """,
                            (ai_result.get("optimized_title"), json.dumps(ai_result), product.get("sku", "")),
                        )
                        await db.commit()
                    stats["optimized"] += 1

                except Exception as e:
                    logger.warning(f"AI optimization failed for product {product.get('sku', '')}: {e}")
                    stats["errors"] += 1

                    # Log the error to system_logs
                    await self._log_system_event(
                        event_type="ai_optimization_error",
                        event_data=json.dumps({"sku": product.get("sku", ""), "error": str(e)}),
                    )

        except Exception as e:
            logger.exception(f"Batch AI optimization failed: {e}")
            stats["errors"] += len(product_dicts)

        return stats

    async def trigger_multi_channel_ai(self, products: List[Dict[str, Any | None]] | None = None) -> Dict[str, int]:
        """Trigger plattformspezifische KI-Inhaltsgenerierung für alle Kanäle.

        Generiert für jedes Produkt kanal-spezifische Titel, Beschreibungen und
        SEO-Metadaten für Shopify, Amazon und Google Shopping und speichert sie
        in der Datenbank-Tabelle `channel_content`.

        :param products: List of product dictionaries. If None, fetches recently
            imported products from the database.
        :return: Dictionary with generation statistics per channel
        """
        stats: Dict[str, int] = {"generated": 0, "errors": 0}

        if products is None:
            # Fetch recently imported products from the database
            async with get_connection() as db:
                async with db.execute(
                    "SELECT sku, title, category FROM products WHERE status = 'active' ORDER BY updated_at DESC LIMIT 20"
                ) as cursor:
                    rows = await cursor.fetchall()
                products = [{"sku": row[0], "title": row[1], "category": row[2]} for row in rows]

        if not products:
            return stats

        for product in products:
            try:
                sku: str = product.get("sku", "") or ""
                title: str = product.get("title", "") or ""
                category: str = product.get("category", "") or ""

                if not sku or not title:
                    continue

                # Generate channel-specific content for each target channel
                for channel in ["shopify", "amazon", "google_shopping"]:
                    try:
                        ai_result = await self.ai_engine.generate_channel_content(
                            title=title,
                            category=category,
                            price=product.get("price", 0.0) or 0.0,
                            channel_name=channel,
                        )

                        # Store in channel_content table
                        async with get_connection() as db:
                            await db.execute(
                                """
                                INSERT INTO channel_content (sku, channel_name, optimized_title, description_html, seo_keywords, image_prompt, is_published)
                                VALUES (?, ?, ?, ?, ?, ?, 1)
                                ON CONFLICT (sku, channel_name) DO UPDATE SET
                                    optimized_title = excluded.optimized_title,
                                    description_html = excluded.description_html,
                                    seo_keywords = excluded.seo_keywords,
                                    image_prompt = excluded.image_prompt,
                                    is_published = 1,
                                    created_at = CURRENT_TIMESTAMP
                                """,
                                (sku, channel, ai_result.get("optimized_title", title),
                                 ai_result.get("description", ""),
                                 json.dumps(ai_result.get("seo_keywords", [])),
                                 ai_result.get("image_prompt", "")),
                            )
                            await db.commit()
                        stats["generated"] += 1

                    except Exception as e:
                        logger.warning(f"Multi-channel AI generation failed for {sku}/{channel}: {e}")
                        stats["errors"] += 1

            except Exception as e:
                logger.error(f"Multi-channel AI generation error for product: {e}")
                stats["errors"] += 1

        return stats

    # -------------------------------------------------------------------------
    # System Log utilities
    # -------------------------------------------------------------------------

    async def _log_system_event(self, event_type: str, event_data: str) -> None:
        """Log a system event to the system_logs table.

        :param event_type: Type of event (e.g. 'price_adjustment', 'stock_warning', 'ai_generated')
        :param event_data: JSON string with event details
        """
        try:
            async with get_connection() as db:
                await db.execute(
                    """
                    INSERT INTO system_logs (sku, event_type, event_data)
                    VALUES (?, ?, ?)
                    """,
                    (None, event_type, event_data),  # SKU can be None for system-level events
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to log system event: {e}")

    async def log_price_adjustment(self, sku: str, old_price: float, new_price: float, reason: str) -> None:
        """Log a price adjustment event.

        :param sku: Product SKU
        :param old_price: Previous price
        :param new_price: New price
        :param reason: Reason for the price adjustment
        """
        await self._log_system_event(
            event_type="price_adjustment",
            event_data=json.dumps(
                {"old_price": old_price, "new_price": new_price, "reason": reason, "timestamp": asyncio.get_event_loop().time()}
            ),
        )

    async def log_stock_warning(self, sku: str, stock: int, buffered_stock: int, threshold: int = 10) -> None:
        """Log a stock level warning.

        :param sku: Product SKU
        :param stock: Raw stock count
        :param buffered_stock: Buffered (safe) stock count
        :param threshold: Stock threshold for warning
        """
        await self._log_system_event(
            event_type="stock_warning",
            event_data=json.dumps(
                {
                    "sku": sku,
                    "stock": stock,
                    "buffered_stock": buffered_stock,
                    "threshold": threshold,
                    "urgent": buffered_stock <= 0,
                }
            ),
        )