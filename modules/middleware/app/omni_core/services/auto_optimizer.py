"""Auto-Optimizer Agent for Omni-Core E-Commerce Middleware.

A Background-Routine that periodically or post-import runs to:
  1. Analyze data quality of imported products
  2. Self-Healing: Capture unknown XML/JSON nodes and migrate them to attributes JSON
  3. Generate autonomous performance suggestions and write them to system_logs
  4. Provide a feedback loop for the ContentAIEngine and channel overrides
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import aiosqlite

from database import get_connection, DATABASE_PATH as DB_PATH
from .content_ai import ContentAIEngine
from .omni_router_client import OmniRouterClient

logger = logging.getLogger("omni.auto_optimizer")


class AutoOptimizerAgent:
    """
    Autonomous optimization agent for the Omni-Core Middleware.

    Runs as a background task to continuously improve data quality,
    capture missing attributes from supplier feeds, and generate
    performance suggestions stored in the system_logs table.
    """

    def __init__(self, content_ai_engine: ContentAIEngine | None = None):
        """
        Initialize the AutoOptimizerAgent.

        :param content_ai_engine: Optional ContentAIEngine instance.
            If None, a default one will be created.
        """
        self.content_ai: ContentAIEngine = content_ai_engine or ContentAIEngine()
        self.omni_router: OmniRouterClient = self.content_ai.omni_router
        self._running: bool = False

    async def start(self) -> None:
        """Start the background optimization loop."""
        self._running = True
        logger.info("AutoOptimizerAgent started")

    async def stop(self) -> None:
        """Stop the background optimization loop."""
        self._running = False
        logger.info("AutoOptimizerAgent stopped")

    # -------------------------------------------------------------------------
    # Main Optimization Loop
    # -------------------------------------------------------------------------

    async def run_cycle(self, force: bool = False) -> Dict[str, int]:
        """Run one optimization cycle.

        Analyzes data quality, performs self-healing attribute mapping,
        and generates performance suggestions.

        :param force: If True, runs even if a cycle is already in progress
        :return: Dictionary with cycle statistics
        """
        if not self._running and not force:
            logger.info("AutoOptimizerAgent not running, skipping cycle")
            return {"analyzed": 0, "healed": 0, "suggested": 0, "errors": 0}

        stats: Dict[str, int] = {"analyzed": 0, "healed": 0, "suggested": 0, "errors": 0}

        try:
            # Step 1: Fetch all active products from the database
            products = await self._fetch_products_needing_attention()
            stats["analyzed"] = len(products)

            if not products:
                return stats

            # Step 2: Self-Healing - Map unknown attributes from raw_payload
            stats["healed"] = await self._self_healing_attribute_mapping(products)

            # Step 3: Generate performance suggestions
            stats["suggested"] = await self._generate_performance_suggestions(products)

        except Exception as e:
            logger.exception(f"Auto-optimization cycle failed: {e}")
            stats["errors"] += 1

        return stats

    # -------------------------------------------------------------------------
    # Step 1: Fetch products needing attention (missing attributes, low stock, etc.)
    # -------------------------------------------------------------------------

    async def _fetch_products_needing_attention(self) -> List[Dict[str, Any]]:
        """Fetch products that have missing attributes or low stock warnings."""
        products: List[Dict[str, Any]] = []

        async with get_connection() as db:
            # Products with empty or NULL attributes JSON
            async with db.execute(
                """
                SELECT sku, title, category, attributes, raw_payload
                FROM products
                WHERE status = 'active'
                  (attributes IS NULL OR TRIM(attributes) = '' OR json_extract(attributes, '$') IS NULL)
                ORDER BY updated_at ASC
                LIMIT 50
                """
            ) as cursor:
                rows = await cursor.fetchall()

                for row in rows:
                    products.append(
                        {
                            "sku": row[0],
                            "title": row[1],
                            "category": row[2],
                            "attributes": json.loads(row[3]) if row[3] else {},
                            "raw_payload": row[4],
                        }
                    )

            # Also add products with low buffered stock
            async with db.execute(
                """
                SELECT sku, title, category, attributes, buffered_stock, stock
                FROM products
                WHERE status = 'active' AND buffered_stock < 20
                ORDER BY buffered_stock ASC
                LIMIT 50
                """
            ) as cursor:
                rows = await cursor.fetchall()

                for row in rows:
                    sku = row[0]
                    # Avoid duplicates
                    if any(p["sku"] == sku for p in products):
                        continue
                    products.append(
                        {
                            "sku": sku,
                            "title": row[1],
                            "category": row[2],
                            "attributes": json.loads(row[3]) if row[3] else {},
                            "raw_payload": None,
                            "buffered_stock": row[4],
                            "stock": row[5],
                        }
                    )

        return products

    # -------------------------------------------------------------------------
    # Step 2: Self-Healing Attribute Mapping
    # -------------------------------------------------------------------------

    async def _self_healing_attribute_mapping(self, products: List[Dict[str, Any]]) -> int:
        """Map unknown attributes from raw_payload or supplier format into the attributes JSON.

        This is the 'self-healing' component: when a product has been imported
        but the XML/JSON supplier format had nodes that weren't captured, we
        attempt to extract and categorize them.

        :param products: List of products with potentially missing attributes
        :return: Number of products healed (attributes updated)
        """
        healed_count: int = 0

        for product in products:
            try:
                sku: str = product.get("sku", "")
                raw_payload: str | None = product.get("raw_payload")
                current_attrs: dict[str, Any] = product.get("attributes", {})

                # If we have a raw payload (XML dump), try to extract additional attributes
                if raw_payload and isinstance(raw_payload, str):
                    # Try to parse as JSON if it looks like JSON
                    if raw_payload.strip().startswith("{") or raw_payload.strip().startswith("["):
                        try:
                            payload_data: dict[str, Any] = json.loads(raw_payload)
                            # Merge any new keys into attributes
                            for key, value in payload_data.items():
                                if key not in current_attrs and value:
                                    current_attrs[key] = value
                        except (json.JSONDecodeError, TypeError):
                            # Not JSON payload - could be XML, try keyword extraction
                            pass
                    else:
                        # Text payload - try to extract structured data via AI
                        try:
                            ai_extracted = await self._extract_attributes_via_ai(
                                product.get("title", ""), raw_payload
                            )
                            for key, value in ai_extracted.items():
                                if key not in current_attrs:
                                    current_attrs[key] = value
                        except Exception:
                            pass

                # If attributes were updated, save back to database
                if current_attrs != product.get("attributes", {}):
                    async with get_connection() as db:
                        await db.execute(
                            """
                            UPDATE products
                            SET attributes = ?
                            WHERE sku = ?
                            """,
                            (json.dumps(current_attrs, ensure_ascii=False), sku),
                        )
                        await db.commit()
                    healed_count += 1
                    logger.info(f"Healed attributes for product {sku}: {list(current_attrs.keys())}")

            except Exception as e:
                logger.warning(f"Self-healing failed for product {sku}: {e}")
                healed_count += 0  # Don't count as healed on error

        return healed_count

    async def _extract_attributes_via_ai(
        self, title: str, raw_payload: str
    ) -> dict[str, Any]:
        """Use the ContentAIEngine to extract structured attributes from raw text/payload."""
        system_prompt: str = (
            "You are an E-Commerce data extraction expert. "
            "Extract product attributes (color, size, material, brand, etc.) from the given text "
            "and output valid JSON with attribute names as keys and their values as values. "
            "Only output the JSON object, no surrounding text."
        )

        user_prompt: str = f"Produkt-Titel: '{title}'\\nRohdaten: {raw_payload[:2000]}"

        try:
            response: dict[str, Any] = await self.content_ai.omni_router.get_structured_response(
                system_prompt, user_prompt, response_format={"type": "json_object"}
            )
            if isinstance(response, dict):
                return response
            return {}
        except Exception as e:
            logger.warning(f"AI attribute extraction failed: {e}")
            return {}

    # -------------------------------------------------------------------------
    # Step 3: Generate Performance Suggestions
    # -------------------------------------------------------------------------

    async def _generate_performance_suggestions(self, products: List[Dict[str, Any]]) -> int:
        """Generate autonomous performance and pricing suggestions.

        For each product, analyze the current data and generate suggestions
        for price adjustments, restocking, or AI content improvements.
        Suggestions are written to the system_logs table.

        :param products: List of products to analyze
        :return: Number of suggestions generated
        """
        suggested_count: int = 0

        for product in products:
            try:
                sku: str = product.get("sku", "")
                title: str = product.get("title", "")
                category: str = product.get("category", "")
                supplier_price: float = product.get("price", 0.0) or 0.0
                stock: int = product.get("stock", 0) or 0
                buffered_stock: int = product.get("buffered_stock", 0) or 0
                current_attrs: dict[str, Any] = product.get("attributes", {})

                # Check 1: Price adjustment suggestion
                # If retail price seems too low compared to typical margin, suggest adjustment
                expected_retail: float = round(supplier_price * 1.95, 2)
                current_attrs_json: str = json.dumps(current_attrs)

                if expected_retail > 0:
                    price_diff_pct: float = round(
                        (expected_retail - expected_retail) / expected_retail * 100, 2
                    )  # placeholder - would need current retail price from DB
                    # Since we don't store current retail price separately, we'll skip this
                    # or use a simplified check

                # Check 2: Stock level warning
                if buffered_stock <= 0:
                    # Out of stock - generate urgent restock suggestion
                    await self._log_suggestion(
                        sku=sku,
                        title=title,
                        suggestion_type="stock_urgent",
                        suggestion_text=f"Produkt ist aus dem Lager genommen. Puffer-bestock: 0. Sofortige Nachbestellung empfohlen.",
                        priority="high",
                    )
                    suggested_count += 1
                elif buffered_stock < 10:
                    await self._log_suggestion(
                        sku=sku,
                        title=title,
                        suggestion_type="stock_warning",
                        suggestion_text=f"Niedriger Lagerbestand. Puffer-bestock: {buffered_stock}. Nachbestellung innerhalb von 1-2 Wochen empfohlen.",
                        priority="medium",
                    )
                    suggested_count += 1

                # Check 3: AI content gap analysis
                # If optimized_title or ai_meta is missing or empty, suggest AI re-generation
                optimized_title: str | None = product.get("optimized_title")
                ai_meta_json: str | None = product.get("ai_meta")

                if not optimized_title or optimized_title == title:
                    # Title not AI-optimized yet
                    await self._log_suggestion(
                        sku=sku,
                        title=title,
                        suggestion_type="ai_content_gap",
                        suggestion_text=f"Produktbeschreibung wurde noch nicht von KI optimiert. AI-Optimierung empfohlen für SEO-Verbesserung.",
                        priority="low",
                    )
                    suggested_count += 1

                # Check 4: Attribute completeness
                # If attributes has fewer than 3 keys, suggest attribute enrichment
                attr_count: int = len(current_attrs)
                if attr_count < 3:
                    await self._log_suggestion(
                        sku=sku,
                        title=title,
                        suggestion_type="attribute_enrichment",
                        suggestion_text=f"Attribute sind unvollständig ({attr_count} von mind. 3 erwartet). Rohdaten-Check und -Mapping empfohlen.",
                        priority="medium",
                    )
                    suggested_count += 1

            except Exception as e:
                logger.warning(f"Suggestion generation failed for product {sku}: {e}")
                suggested_count += 0

        return suggested_count

    async def _log_suggestion(
        self,
        sku: str,
        title: str,
        suggestion_type: str,
        suggestion_text: str,
        priority: str = "medium",
    ) -> None:
        """Log a performance suggestion to the system_logs table."""
        try:
            await self._log_system_event(
                event_type=suggestion_type,
                event_data=json.dumps(
                    {
                        "sku": sku,
                        "title": title[:200] if title else "",
                        "suggestion_text": suggestion_text[:500],
                        "priority": priority,
                        "generated_at": asyncio.get_event_loop().time(),
                    }
                ),
            )
        except Exception as e:
            logger.error(f"Failed to log suggestion for {sku}: {e}")

    # -------------------------------------------------------------------------
    # System Log utility (shared)
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
                    (sku, event_type, event_data),
                )
                await db.commit()
        except Exception as e:
            logger.error(f"Failed to log system event '{event_type}': {e}")