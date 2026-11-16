"""Content AI Engine for Omni-Core E-Commerce Middleware.

Provides AI-powered content generation and analysis using the Omni-Route Gateway.
This service transforms raw product data into high-quality E-Commerce copy,
structured SEO metadata, and performs intelligent pricing/trim analysis.

The engine is designed to work with the -10% stock buffer logic,
channel overrides, and integrates seamlessly with the Omni-Core Middleware pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from .omni_router_client import OmniRouterClient

logger = logging.getLogger("omni.content_ai")

# Default prompts for content generation
DEFAULT_PROMPTS: dict[str, str] = {
    "product_description": (
        "You are an expert E-Commerce AI copywriter specializing in fashion retail. "
        "Generate a compelling, conversion-focused product description. "
        "Always output valid JSON with the following structure: "
        "{'optimized_title': '...', 'description': '...', 'bullet_points': [...], "
        "'seo_keywords': [...], 'meta_title': '...', 'meta_description': '...'}"
    ),
    "trend_analysis": (
        "You are a fashion trend analyst with deep knowledge of the European market. "
        "Analyze the following products and identify trends, seasonality, and pricing strategy. "
        "Consider current market conditions, competitor pricing, and seasonal patterns. "
        "Output valid JSON: {'trend_score': 0-10, 'suggested_price': float, "
        "'margin_recommendation': 'competitive|normal|high_recovery', "
        "'restock_recommendation': 'urgent|moderate|normal'}"
    ),
}


class ContentAIEngine:
    """
    AI-powered content generation and analysis engine for E-Commerce products.

    Uses the Omni-Route Gateway to transform raw product data into
    high-quality marketing copy, structured SEO metadata, and performs
    intelligent pricing analysis with stock buffer considerations.

    The engine integrates with the -10% stock buffer logic and
    supports channel-specific overrides via the channel_overrides table.
    """

    def __init__(self, omni_router_client: OmniRouterClient | None = None):
        """
        Initialize the ContentAIEngine.

        :param omni_router_client: An initialized OmniRouterClient instance.
            If None, a default client will be created.
        """
        self.omni_router: OmniRouterClient = omni_router_client or OmniRouterClient()

    # -------------------------------------------------------------------------
    # Rich Product Content Generation
    # -------------------------------------------------------------------------

    async def generate_rich_content(
        self,
        title: str,
        category: str,
        supplier_price: float,
        raw_attrs: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Generate SEO-optimized, sales-driven product copy from raw product data.

        Creates:
        - optimized_title: SEO title (max 70 chars)
        - description: HTML-formatted product description with benefit bullets
        - bullet_points: 3-5 key benefit highlights
        - seo_keywords: 5-8 relevant search terms
        - meta_title: Meta title tag (max 70 chars)
        - meta_description: Meta description (150-160 chars)

        :param title: Original product title from supplier
        :param category: Product category (used for context-sensitive generation)
        :param supplier_price: Supplier price from Matterhorn/CSV
        :param raw_attrs: Dictionary of additional attributes (color, size, material, etc.)
        :return: Dictionary with rich content components
        """
        system_prompt: str = (
            "You are an expert E-Commerce AI copywriter specializing in fashion retail. "
            "Generate conversion-focused, SEO-optimized product copy. "
            "Always output valid JSON with the exact keys: "
            "'optimized_title', 'description', 'bullet_points', 'seo_keywords', "
            "'meta_title', 'meta_description'."
        )

        # Build a comprehensive user prompt with all product data
        attrs_str: str = json.dumps(raw_attrs, ensure_ascii=False, indent=2)
        user_prompt: str = (
            f"Produkt-Daten:\n"
            f"- Titel: {title}\n"
            f"- Kategorie: {category}\n"
            f"- Lieferanten-Preis: {supplier_price} EUR\n"
            f"- Zusatz-Attribute: {attrs_str}\n\n"
            "Erzeuge folgenden JSON-Output (kein umschließendes ```json```):\n"
            "{"
            f"'optimized_title': '<verkaufsstarker Titel max 70 Zeichen>', "
            f"'description': '<HTML-formatierte Beschreibung mit Vorteils-Paragraphen>', "
            f"'bullet_points': ['<Kernvorteil 1>', '<Kernvorteil 2>', '<Kernvorteil 3>']"
            f", 'seo_keywords': ['<Suchbegriff 1>', '<Suchbegriff 2>', '<Suchbegriff 3>', '<Suchbegriff 4>', '<Suchbegriff 5>']"
            f", 'meta_title': '<Meta-Titel max 70 Zeichen>', "
            f"'meta_description': '<Meta-Beschreibung 150-160 Zeichen>'"
            "}"
        )

        try:
            response: dict[str, Any] = await self.omni_router.get_structured_response(
                system_prompt, user_prompt, response_format={"type": "json_object"}
            )

            # Validate that we have the expected keys
            expected_keys = {
                "optimized_title",
                "description",
                "bullet_points",
                "seo_keywords",
                "meta_title",
                "meta_description",
            }
            if response and isinstance(response, dict) and expected_keys.issubset(response.keys()):
                return {
                    "optimized_title": response.get("optimized_title", title[:70]),
                    "description": response.get("description", f"<p>{title} aus der Kategorie {category}</p>"),
                    "bullet_points": response.get("bullet_points", [
                        "Hochwertiges Produkt",
                        "Langlebig & Robust",
                        "Exklusives Design",
                    ]),
                    "seo_keywords": response.get("seo_keywords", [category, "Mode", "Online-Shop"]),
                    "meta_title": response.get("meta_title", title[:70]),
                    "meta_description": response.get(
                        "meta_description",
                        f"{title} - Entdecken Sie {category} online im Shop",
                    )[:160],
                }

            # Fallback if response structure is unexpected
            logger.warning("AI response missing expected keys, using fallback")
            return self._fallback_content(title, category)

        except Exception as e:
            logger.warning(f"Rich content generation failed: {e}")
            return self._fallback_content(title, category)

    @staticmethod
    def _fallback_content(title: str, category: str) -> dict[str, Any]:
        """Fallback content generation when AI is unavailable."""
        return {
            "optimized_title": title[:70],
            "description": f"<p>{title} aus der Kategorie {category}. Hochwertige Verarbeitung und ansprechendes Design.</p>",
            "bullet_points": [
                "Hochwertiges Produkt",
                "Langlebig & Robust",
                "Exklusives Design",
            ],
            "seo_keywords": [category, "Mode", "Online-Shop"],
            "meta_title": title[:70],
            "meta_description": f"{title} - Entdecken Sie {category} online im Shop"[:160],
        }

    async def generate_channel_content(
        self,
        title: str,
        category: str,
        price: float,
        channel_name: str,
    ) -> dict[str, Any]:
        """
        Generate plattformspezifische KI-Inhalte für einen bestimmten Vertriebskanal.

        Jeder Kanal erhält einen angepassten Prompt:
        - 'shopify': Emotional, story-lastig, fokiert auf Vorteile
        - 'amazon': Faktenbasiert, keyword-optimiert, produkt-detailliert
        - 'google_shopping': Präzise, Merchant Center konform, klar strukturiert

        :param title: Original product title
        :param category: Product category
        :param price: Supplier price
        :param channel_name: Target channel ('shopify', 'amazon', 'google_shopping')
        :return: Dictionary with channel-optimized content
        """
        # Kanal-spezifische Prompt-Anpassung
        if channel_name == "shopify":
            channel_prompt = "Erzeuge emotionalen, story-lastigen Verkaufstext mit Fokus auf Lebensstil und Vorteile. "
        elif channel_name == "amazon":
            channel_prompt = "Erzeuge faktenbasierten, keyword-optimierten Text mit detaillierten Produktmerkmalen und Spezifikationen. "
        elif channel_name == "google_shopping":
            channel_prompt = "Erzeuge präzisen, Merchant Center konformen Text mit klarer Struktur und exakten Preisangaben. "
        else:
            channel_prompt = ""

        system_prompt = (
            f"Du bist ein E-Commerce AI-Experte für den {channel_name.upper()} Kanal. "
            "Antworte stets mit gültigem JSON gemäß dem Schema:"
            "{'optimized_title': '...', 'description': '...', 'seo_keywords': [...], "
            "'image_prompt': '...'}."
        )

        user_prompt = f"""
        Produkt-Daten:
        - Titel: {title}
        - Kategorie: {category}
        - EK-Preis: {price} EUR

        Optimiere den Text für den {channel_name.upper()} Vertriebskanal.
        """

        try:
            response: dict[str, Any] = await self.omni_router.get_structured_response(
                system_prompt, user_prompt, response_format={"type": "json_object"}
            )

            if isinstance(response, dict) and "optimized_title" in response:
                return {
                    "optimized_title": response.get("optimized_title", title),
                    "description": response.get(
                        "description", f"<p>{title} aus der Kategorie {category}</p>"
                    ),
                    "seo_keywords": response.get(
                        "seo_keywords", [category, "Mode", "Online-Shop"]
                    ),
                    "image_prompt": response.get(
                        "image_prompt",
                        f"Professionelles Produktfoto von {title} auf Weißem Hintergrund",
                    ),
                }

            return self._fallback_content(title, category)

        except Exception as e:
            logger.warning(f"Channel content generation failed for {channel_name}: {e}")
            return self._fallback_content(title, category)

    # -------------------------------------------------------------------------
    # Trend & Pricing Analysis with Stock Buffer
    # -------------------------------------------------------------------------

    async def analyze_trends_and_pricing(
        self,
        products_batch: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """
        Analyze a batch of imported products and suggest pricing/trend insights.

        Considers:
        - The -10% stock buffer logic (products with low buffered stock get higher margin suggestions)
        - Category-based trend analysis
        - Cost-plus pricing with margin calculation

        :param products_batch: List of product dicts from the import pipeline.
            Each product should have: 'supplier_price', 'stock', 'category', 'sku'
        :return: List of product dicts with added 'trend_score', 'suggested_price',
            'margin_recommendation', and 'restock_recommendation' fields.
        """
        if not products_batch:
            return []

        results: list[dict[str, Any]] = []

        for product in products_batch:
            try:
                product_result: dict[str, Any] = {
                    "sku": product.get("sku", "unknown"),
                    "category": product.get("category", "unknown"),
                    "supplier_price": product.get("supplier_price", 0.0),
                    "stock": product.get("stock", 0),
                    "trend_score": 0.0,
                    "suggested_price": 0.0,
                    "margin_recommendation": "normal",
                    "restock_recommendation": "normal",
                }

                # Apply the -10% stock buffer logic
                raw_stock: int = product.get("stock", 0)
                buffered_stock: int = int(raw_stock * 0.90) if raw_stock > 0 else 0

                # Calculate margin recommendations based on stock level
                supplier_price: float = product.get("supplier_price", 0.0)

                # Base retail price calculation (195% margin)
                retail_price: float = round(supplier_price * 1.95, 2)
                product_result["suggested_price"] = retail_price

                # Margin adjustment based on stock level
                if buffered_stock <= 0:
                    # Out of stock - high margin, focus on revenue recovery
                    product_result["margin_recommendation"] = "high_recovery"
                    margin_adjustment: float = 0.35  # 35% margin for slow-moving stock
                elif buffered_stock < 10:
                    # Low stock - moderate margin
                    product_result["margin_recommendation"] = "normal"
                    margin_adjustment: float = 0.25
                else:
                    # Good stock level - competitive margin
                    product_result["margin_recommendation"] = "competitive"
                    margin_adjustment: float = 0.18

                # Refined suggested price with margin adjustment
                if margin_adjustment > 0:
                    adjusted_price: float = round(
                        supplier_price / (1.0 - margin_adjustment), 2
                    )
                else:
                    adjusted_price = retail_price
                product_result["suggested_price"] = adjusted_price

                # Trend score based on stock level and price competitiveness
                if buffered_stock > 50:
                    product_result["trend_score"] = 7.0  # High trend potential (overstock)
                elif buffered_stock > 10:
                    product_result["trend_score"] = 5.0  # Moderate trend potential
                else:
                    product_result["trend_score"] = 2.0  # Low stock, niche product

                # Restock recommendation
                if buffered_stock <= 0:
                    product_result["restock_recommendation"] = "urgent"
                elif buffered_stock < 10:
                    product_result["restock_recommendation"] = "moderate"
                else:
                    product_result["restock_recommendation"] = "normal"

                results.append(product_result)

            except Exception as e:
                logger.warning(f"Trend/pricing analysis failed for product: {e}")
                results.append(
                    {
                        "sku": product.get("sku", "unknown"),
                        "category": product.get("category", "unknown"),
                        "trend_score": 0.0,
                        "suggested_price": product.get("supplier_price", 0.0),
                        "margin_recommendation": "normal",
                        "restock_recommendation": "normal",
                    }
                )

        return results

    # -------------------------------------------------------------------------
    # Channel-Specific Override Generation
    # -------------------------------------------------------------------------

    async def generate_channel_override(
        self,
        sku: str,
        channel_name: str,
        base_title: str,
        base_description: str,
        base_price: float,
        attributes: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Generate platform-specific content overrides for a given sales channel.

        :param sku: Product SKU
        :param channel_name: Target channel ('shopify', 'amazon', 'google_shopping')
        :param base_title: Base product title
        :param base_description: Base product description
        :param base_price: Base product price
        :param attributes: Product attributes dictionary
        :return: Dictionary with channel-specific overrides
        """
        system_prompt: str = (
            f"You are an E-Commerce platform specialist for {channel_name}. "
            f"Generate platform-optimized product copy. "
            f"Always output valid JSON with keys: "
            f"'custom_title', 'custom_description', 'custom_price', 'is_published'."
        )

        attrs_str: str = json.dumps(attributes or {}, ensure_ascii=False)
        user_prompt: str = (
            f"Produkt-Daten:\n"
            f"- SKU: {sku}\n"
            f"- Kanal: {channel_name}\n"
            f"- Basis-Titel: {base_title}\n"
            f"- Basis-Beschreibung: {base_description}\n"
            f"- EK-Preis: {base_price} EUR\n"
            f"- Attribute: {attrs_str}\n\n"
            "Erzeuge JSON-Output:"
            f"{{\"custom_title\": \"...\", \"custom_description\": \"...\", "
            f"'custom_price\": <preis>, \"is_published\": true}}"
        )

        try:
            response: dict[str, Any] = await self.omni_router.get_structured_response(
                system_prompt, user_prompt, response_format={"type": "json_object"}
            )

            if isinstance(response, dict):
                return {
                    "custom_title": response.get("custom_title", base_title),
                    "custom_description": response.get(
                        "custom_description", base_description
                    ),
                    "custom_price": response.get("custom_price", base_price),
                    "is_published": response.get("is_published", True),
                }

            return {
                "custom_title": base_title,
                "custom_description": base_description,
                "custom_price": base_price,
                "is_published": True,
            }

        except Exception as e:
            logger.warning(f"Channel override generation failed for {sku}/{channel_name}: {e}")
            return {
                "custom_title": base_title,
                "custom_description": base_description,
                "custom_price": base_price,
                "is_published": True,
            }