"""Marketing & Content Engine for Omni-Core ERP & BI Enterprise Edition.

Provides automated content generation and marketing campaign creation:
- Social media campaign generation (Meta/Instagram, Google Ads)
- Email newsletter generation with segmentation
- Platform-specific ad copy generation via Omni-Route Gateway
- Content optimization and A/B testing support
"""

from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from services.omniroute_client import OmniRouteClient

logger = logging.getLogger("omni.marketing")


class MarketingEngine:
    """Intelligent marketing engine for automated campaign generation."""

    def __init__(self, client: OmniRouteClient | None = None) -> None:
        self.client = client or OmniRouteClient(
            os.getenv('OMNI_ROUTE_BASE_URL', 'http://localhost:20128'),
            os.getenv('OMNI_ROUTE_API_KEY', 'test-key'),
        )
        # Campaign configuration
        self.ad_formats = {
            "meta": {"aspect_ratio": "1:1", "max_text": "125"},
            "google_search": {"max_chars": 30},
            "google_display": {"max_chars": 90},
            "email": {"preheader_chars": 50},
        }

    async def generate_meta_ad_copy(
        self, product_sku: str, product_data: Dict[str, Any], campaign_goal: str = "conversion"
    ) -> Dict[str, Any]:
        """Generate Meta/Instagram ad copy for a product."""

        # Build product context for AI
        product_title = product_data.get("title", "Product")
        product_category = product_data.get("category", "Uncategorized")
        product_price = product_data.get("retail_price", 0)

        # Prepare prompt for Omni-Route Gateway
        prompt = f"""Du bist ein erfahrener Meta/Instagram Advertising Experte. 
Erstelle ansprechenden Werbetext für ein Produkt.

Produkt-Details:
- Titel: {product_title}
- Kategorie: {product_category}
- Preis: {product_price} EUR
- Ziel: {campaign_goal}

Erstelle:
1. Eine fesselnde Headline (max 40 Zeichen)
2. Ein überzeugendes Haupttext (max 125 Zeichen)
3. 3 Call-to-Action Buttons
4. 3 Bild-Ideas für das Carousel/Video

Formatiere die Ausgabe als JSON mit den Schlüsseln: headline, main_text, cta_buttons, image_ideas
"""

        try:
            response = await self.client.complete_chat([
                {"role": "system", "content": "Du bist ein Meta Advertising Experte mit Expertise für E-Commerce Konversionen. Antworte immer im angeforderten JSON-Format."},
                {"role": "user", "content": prompt}
            ])

            # Try to parse JSON from response
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "{}")
            # Extract JSON if wrapped in text
            import re
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                return {
                    "success": True,
                    "headline": parsed.get("headline", f"Entdecke {product_title}"),
                    "main_text": parsed.get("main_text", "Erleben Sie Qualität zum besten Preis"),
                    "cta_buttons": parsed.get("cta_buttons", ["Jetzt kaufen", "Mehr erfahren", "Angebot anfordern"]),
                    "image_ideas": parsed.get("image_ideas", [f"Produktfoto von {product_title}"]),
                    "platform": "meta",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
            else:
                return {"success": False, "error": "Invalid JSON response from AI"}

        except Exception as e:
            logger.error(f"Error generating Meta ad copy: {e}")
            return {
                "success": False,
                "error": str(e),
                "fallback": {
                    "headline": f"Entdecke {product_title}",
                    "main_text": f"Qualitatives Produkt zum Preis von {product_price} EUR",
                    "cta_buttons": ["Jetzt kaufen", "Mehr erfahren"],
                    "image_ideas": [f"Produktfoto {product_title}"],
                    "platform": "meta",
                    "fallback_used": True,
                },
            }

    async def generate_google_ads_copy(
        self, product_sku: str, product_data: Dict[str, Any], campaign_goal: str = "conversion"
    ) -> Dict[str, Any]:
        """Generate Google Ads search copy for a product."""

        product_title = product_data.get("title", "Product")
        product_price = product_data.get("retail_price", 0)
        product_category = product_data.get("category", "Uncategorized")

        # Build Google Ads specific prompt
        prompt = f"""Du bist ein Google Ads Spezialist. Erstelle Suchanzeigen-Texte für dieses Produkt.

Produkt-Details:
- Titel: {product_title}
- Kategorie: {product_category}
- Preis: {product_price} EUR
- Ziel: {campaign_goal}

Erstelle gemäß Google Ads Best Practices:
1. 3 Headlines (je max 30 Zeichen)
2. 2 Description Lines (je max 90 Zeichen)
3. Path/Domain Display URL (format: domain.com/product)
4. 2 Ad Extensions ideas (Sitelinks or Callouts)

Formatiere als JSON mit: headlines (Array), descriptions (Array), display_url, extensions
"""

        try:
            response = await self.client.complete_chat([
                {"role": "system", "content": "Du bist ein Google Ads Spezialist. Antworte immer im JSON-Format gemäß Google Ads Spezifikation."},
                {"role": "user", "content": prompt}
            ])

            content = response.get("choices", [{}])[0].get("message", {}).get("content", "{}")
            import re
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                return {
                    "success": True,
                    "headlines": parsed.get("headlines", [f"{product_title} kaufen", f"Top Qualität {product_title}", f"{product_price} EUR"]),
                    "descriptions": parsed.get("descriptions", [f"Top-bewertetes Produkt in {product_category}", "Jetzt bestellen und sparen"]),
                    "display_url": parsed.get("display_url", f"domain.com/{product_sku.lower()}"),
                    "extensions": parsed.get("extensions", ["Kostenlose Lieferung", "10% Neukundenrabatt"]),
                    "platform": "google_ads",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
            else:
                return {"success": False, "error": "Invalid JSON response from AI"}

        except Exception as e:
            logger.error(f"Error generating Google Ads copy: {e}")
            return {
                "success": False,
                "error": str(e),
                "fallback": {
                    "headlines": [f"{product_title} kaufen", f"Top Qualität {product_title}", f"{product_price} EUR"],
                    "descriptions": ["Top-bewertetes Produkt", "Jetzt bestellen und sparen"],
                    "display_url": f"domain.com/{product_sku.lower()}",
                    "extensions": ["Kostenlose Lieferung", "10% Neukundenrabatt"],
                    "platform": "google_ads",
                    "fallback_used": True,
                },
            }

    async def generate_email_newsletter(
        self, product_sku: str, product_data: Dict[str, Any], segment_criteria: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate segmented email newsletter content."""

        product_title = product_data.get("title", "Product")
        product_price = product_data.get("retail_price", 0)
        product_category = product_data.get("category", "Uncategorized")
        discount_pct = segment_criteria.get("discount_pct", "kein Rabatt")

        prompt = f"""Du bist ein E-Mail Marketing Experte. Erstelle einen personalisierten Newsletter-Text.

Produkt: {product_title}
Kategorie: {product_category}
Preis: {product_price} EUR
Rabatt/Angebot: {discount_pct}

Erstelle:
1. Betreffzeile (max 50 Zeichen, attention-grabbing)
2. Preheader (max 80 Zeichen, Vorschau-Text)
3. Eröffnungssatz (persönlich und ansprechend)
4. Produktvorstellung mit Nutzen-Fokus
5. Call-to-Action Button Text
6. Abmeldung-Hinweis (dezent)

Formatiere als JSON mit: subject_line, preheader, opening, product_offer, cta_text, disclaimer
"""

        try:
            response = await self.client.complete_chat([
                {"role": "system", "content": "Du bist ein E-Mail Marketing Experte. Antworte immer im JSON-Format für Newsletter-Kampagnen."},
                {"role": "user", "content": prompt}
            ])

            content = response.get("choices", [{}])[0].get("message", {}).get("content", "{}")
            import re
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                return {
                    "success": True,
                    "subject_line": parsed.get("subject_line", f"Neuheit: {product_title} - Jetzt entdecken"),
                    "preheader": parsed.get("preheader", f"Sonderangebot von {discount_pct} bis {product_price} EUR"),
                    "opening": parsed.get("opening", "Hallo Lieber Kunde, wir haben ein besonderes Angebot für Sie:"),
                    "product_offer": parsed.get("product_offer", f"Entdecken Sie {product_title} - ein hochwertiges Produkt aus der Kategorie {product_category}"),
                    "cta_text": parsed.get("cta_text", "Jetzt zum Angebot gehen"),
                    "disclaimer": parsed.get("disclaimer", "Ihr Warenkorb wird für 24 Stunden reserviert"),
                    "platform": "email",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
            else:
                return {"success": False, "error": "Invalid JSON response from AI"}

        except Exception as e:
            logger.error(f"Error generating email newsletter: {e}")
            return {
                "success": False,
                "error": str(e),
                "fallback": {
                    "subject_line": f"Neuheit: {product_title} - Jetzt entdecken",
                    "preheader": "Sonderangebot entdecken",
                    "opening": "Hallo Lieber Kunde, wir haben ein besonderes Angebot für Sie:",
                    "product_offer": f"Entdecken Sie {product_title} - hochwertiges Produkt",
                    "cta_text": "Jetzt zum Angebot gehen",
                    "disclaimer": "Ihr Warenkorb wird für 24 Stunden reserviert",
                    "platform": "email",
                    "fallback_used": True,
                },
            }

    async def generate_social_media_campaign(
        self, product_sku: str, product_data: Dict[str, Any], platforms: List[str] = None
    ) -> Dict[str, Any]:
        """Generate complete social media campaign across multiple platforms."""

        if platforms is None:
            platforms = ["meta", "google_ads", "email"]

        product_title = product_data.get("title", "Product")
        product_price = product_data.get("retail_price", 0)
        product_category = product_data.get("category", "Uncategorized")

        campaign = {
            "product_sku": product_sku,
            "product_title": product_title,
            "product_category": product_category,
            "product_price": product_price,
            "campaigns": {},
            "generated_at": datetime.now(timezone.utc).isoformat()
,
        }

        # Generate for each requested platform
        for platform in platforms:
            if platform == "meta":
                campaign["campaigns"]["meta"] = await self.generate_meta_ad_copy(
                    product_sku, product_data, "conversion"
                )
            elif platform == "google_ads":
                campaign["campaigns"]["google_ads"] = await self.generate_google_ads_copy(
                    product_sku, product_data, "conversion"
                )
            elif platform == "email":
                # Segment criteria based on price category
                if product_price and product_price > 0:
                    if product_price > 100:
                        discount_pct = "15%"
                    elif product_price > 50:
                        discount_pct = "10%"
                    else:
                        discount_pct = "5%"
                else:
                    discount_pct = "kein Rabatt"
                campaign["campaigns"]["email"] = await self.generate_email_newsletter(
                    product_sku, product_data, {"discount_pct": discount_pct}
                )

        return campaign


# Convenience function
def get_marketing_engine() -> MarketingEngine:
    """Factory function for MarketingEngine instance."""
    return MarketingEngine()