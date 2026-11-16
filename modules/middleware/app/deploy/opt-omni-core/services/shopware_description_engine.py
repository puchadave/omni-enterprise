"""Shopware Description & Content Engine.

Generates Shopware-specific product descriptions from Matterhorn source data.
Original supplier data is preserved unchanged. Generated descriptions are
stored separately and can be configured via templates and AI enhancement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .canonical_product_mapping import CanonicalProduct

logger = logging.getLogger(__name__)


# ── Description Templates ─────────────────────────────────────────────────

DESCRIPTION_TEMPLATES: dict[str, str] = {
    "premium": """<h2>{product_name}</h2>
<p>{introduction}</p>
<h3>Produktmerkmale</h3>
<ul>{features}</ul>
<h3>Material &amp; Pflege</h3>
<p>{material_info}</p>
<h3>Größe &amp; Passform</h3>
<p>{size_info}</p>
<h3>Farbe</h3>
<p>{color_info}</p>
<h3>Herstellerinformationen</h3>
<p>{brand_info}</p>""",
    "fashion_editorial": """<h1 style="font-weight:300; letter-spacing:1px;">{product_name}</h1>
<p class="lead">{introduction}</p>
<section>
<h4>Details</h4>
<p>{description}</p>
<h4>Material &amp; Pflegehinweise</h4>
<p>{material_info}</p>
<h4>Farbe &amp; Größe</h4>
<p>{color_info} | {size_info}</p>
</section>
<footer>
<p><em>{brand_info}</em></p>
</footer>""",
    "minimalistic": """<h2>{product_name}</h2>
<p>{description}</p>
<ul>
<li>Farbe: {color_info}</li>
<li>Material: {material_info}</li>
<li>Größe: {size_info}</li>
</ul>
<p>{brand_info}</p>""",
    "seo_professional": """<h2>{product_name}</h2>
<p>{seo_introduction}</p>
<h3>Eigenschaften</h3>
<ul>{features}</ul>
<p>{material_info}</p>
<p>{brand_info}</p>""",
    "technical": """<h2>{product_name}</h2>
<table>
<tr><th>Produktname</th><td>{product_name}</td></tr>
<tr><th>Marke</th><td>{brand_info}</td></tr>
<tr><th>Farbe</th><td>{color_info}</td></tr>
<tr><th>Material</th><td>{material_info}</td></tr>
<tr><th>Größe</th><td>{size_info}</td></tr>
<tr><th>Kategorie</th><td>{category_info}</td></tr>
</table>
<p>{description}</p>""",
}


# ── Description Component ─────────────────────────────────────────────────

class DescriptionComponent:
    """A single component of a generated product description."""

    def __init__(
        self,
        name: str,
        label: str,
        value: str | None = None,
        is_required: bool = False,
        is_ai_generated: bool = False,
    ) -> None:
        self.name: str = name
        self.label: str = label
        self.value: str = value or ""
        self.is_required: bool = is_required
        self.is_ai_generated: bool = is_ai_generated

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "value": self.value,
            "is_required": self.is_required,
            "is_ai_generated": self.is_ai_generated,
        }


# ── Description Engine ────────────────────────────────────────────────────

class DescriptionEngine:
    """Generates Shopware product descriptions from Matterhorn data."""

    def __init__(
        self,
        template_name: str = "premium",
        ai_enabled: bool = False,
        ai_client: Any | None = None,
    ) -> None:
        self.template_name: str = template_name
        self.ai_enabled: bool = ai_enabled
        self.ai_client: Any | None = ai_client

    def generate_description(
        self,
        canonical_product: CanonicalProduct,
        template_name: str | None = None,
        components: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Generate a complete product description.

        Args:
            canonical_product: The canonical product with all source data
            template_name: Override the default template
            components: Override specific description components

        Returns:
            Dict with html, components, and metadata
        """
        template_name = template_name or self.template_name
        template = DESCRIPTION_TEMPLATES.get(template_name, DESCRIPTION_TEMPLATES["premium"])

        # Build component values from product data
        comp_values = self._build_components(canonical_product, components or {})

        # Render template
        html = self._render_template(template, comp_values)

        # Generate source hash for change detection
        source_hash = hashlib.sha256(
            json.dumps(comp_values, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]

        return {
            "html": html,
            "components": {k: v for k, v in comp_values.items()},
            "source_hash": source_hash,
            "template": template_name,
            "ai_enhanced": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def enhance_with_ai(
        self,
        description_data: dict[str, Any],
        canonical_product: CanonicalProduct,
        model: str = "gemini-2.5-flash",
        creativity: float = 0.7,
        max_length: int = 2000,
        tone: str = "professional",
        language: str = "de",
        seo_intensity: str = "medium",
        translation_mode: bool = False,
    ) -> dict[str, Any]:
        """Enhance description using AI via OmniRoute gateway.

        Args:
            description_data: Previously generated description data
            canonical_product: Product data for context
            model: AI model to use
            creativity: Temperature 0.0-1.0
            max_length: Maximum output length
            tone: Writing tone
            language: Target language
            seo_intensity: SEO optimization level
            translation_mode: Whether to translate existing content

        Returns:
            Enhanced description data with AI-generated HTML
        """
        if not self.ai_client:
            logger.warning("AI client not configured, returning original description")
            return description_data

        prompt = self._build_ai_prompt(
            canonical_product, description_data, model, creativity, max_length,
            tone, language, seo_intensity, translation_mode,
        )

        try:
            # Call OmniRoute or configured AI gateway
            ai_response = self._call_ai_gateway(prompt, model, creativity, max_length)
            html = ai_response.get("generated_text", description_data.get("html", ""))
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("AI enhancement failed: %s", exc)
            html = description_data.get("html", "")

        result = dict(description_data)
        result["html"] = html
        result["ai_enhanced"] = True
        result["ai_model"] = model
        result["ai_creativity"] = creativity
        result["ai_tone"] = tone
        result["ai_language"] = language
        result["ai_seo_intensity"] = seo_intensity
        result["ai_translation_mode"] = translation_mode
        result["generated_at"] = datetime.now(timezone.utc).isoformat()

        return result

    # ── Component Building ───────────────────────────────────────────────

    def _build_components(
        self,
        product: CanonicalProduct,
        overrides: dict[str, str],
    ) -> dict[str, str]:
        """Build all description component values from product data."""
        features = self._build_feature_list(product)

        return {
            "product_name": overrides.get("product_name", product.product_name or "Produkt"),
            "introduction": overrides.get(
                "introduction",
                f"Entdecken Sie {product.product_name} – ein hochwertiges Produkt von {product.brand or 'unserer Marke'}.",
            ),
            "features": overrides.get("features", features),
            "material_info": overrides.get(
                "material_info",
                product.material or "Materialinformationen nicht verfügbar.",
            ),
            "size_info": overrides.get(
                "size_info",
                f"Verfügbare Größen: {', '.join(v.get('size', 'N/A') for v in product.variants) if product.variants else 'Standardgröße'}",
            ),
            "color_info": overrides.get(
                "color_info",
                product.color or "Farbe nicht angegeben",
            ),
            "brand_info": overrides.get(
                "brand_info",
                f"Hergestellt von {product.brand or 'unbekannter Hersteller'}.",
            ),
            "category_info": overrides.get(
                "category_info",
                product.category or "Allgemein",
            ),
            "description": overrides.get(
                "description",
                product.raw_description or product.description or "Keine Beschreibung verfügbar.",
            ),
            "seo_introduction": overrides.get(
                "seo_introduction",
                f"{product.product_name} von {product.brand or ''} – Jetzt im Online-Shop erhältlich.",
            ),
            "care_instructions": overrides.get(
                "care_instructions",
                "Pflegehinweise beachten, siehe Etikett.",
            ),
        }

    def _build_feature_list(self, product: CanonicalProduct) -> str:
        """Build HTML feature list from product attributes."""
        items = []
        if product.brand:
            items.append(f"<li>Marke: {product.brand}</li>")
        if product.color:
            items.append(f"<li>Farbe: {product.color}</li>")
        if product.material:
            items.append(f"<li>Material: {product.material}</li>")
        if product.category:
            items.append(f"<li>Kategorie: {product.category}</li>")
        if product.processing_time:
            items.append(f"<li>Lieferzeit: {product.processing_time} Tage</li>")
        if product.ean_codes:
            items.append(f"<li>EAN: {product.ean_codes[0]}</li>")
        for variant in product.variants[:5]:
            size = variant.get("size", "")
            color = variant.get("color", "")
            if size or color:
                items.append(f"<li>Variante: {size} {color}".strip() + "</li>")

        return "\n".join(items) if items else "<li>Keine spezifischen Merkmale verfügbar.</li>"

    # ── Template Rendering ───────────────────────────────────────────────

    def _render_template(self, template: str, components: dict[str, str]) -> str:
        """Render a description template with component values."""
        result = template
        for key, value in components.items():
            placeholder = "{" + key + "}"
            result = result.replace(placeholder, value or "")

        # Replace any remaining unreplaced placeholders with empty string
        result = re.sub(r"\{[^}]*\}", "", result)

        return result.strip()

    # ── AI Gateway ───────────────────────────────────────────────────────

    def _build_ai_prompt(
        self,
        product: CanonicalProduct,
        description_data: dict[str, Any],
        model: str,
        creativity: float,
        max_length: int,
        tone: str,
        language: str,
        seo_intensity: str,
        translation_mode: bool,
    ) -> str:
        """Build AI prompt for description enhancement."""
        if translation_mode:
            return (
                f"Translate the following product description to {language}:\n\n"
                f"{description_data.get('html', '')}\n\n"
                f"Product: {product.product_name}\nBrand: {product.brand}\n"
                f"Keep HTML formatting. Max length: {max_length} characters."
            )

        return (
            f"Enhance the following product description for an e-commerce shop.\n\n"
            f"Product: {product.product_name}\n"
            f"Brand: {product.brand}\n"
            f"Category: {product.category}\n"
            f"Color: {product.color}\n"
            f"Material: {product.material}\n"
            f"Current description: {description_data.get('html', '')}\n\n"
            f"Requirements:\n"
            f"- Tone: {tone}\n"
            f"- Language: {language}\n"
            f"- SEO intensity: {seo_intensity}\n"
            f"- Creativity: {creativity}\n"
            f"- Max length: {max_length} characters\n"
            f"- Keep HTML formatting\n"
            f"- Do not invent product features not present in source data\n"
            f"- Generate compelling, sales-oriented copy\n"
        )

    def _call_ai_gateway(
        self,
        prompt: str,
        model: str,
        creativity: float,
        max_length: int,
    ) -> dict[str, Any]:
        """Call the AI gateway (OmniRoute or configured provider)."""
        if not self.ai_client:
            raise RuntimeError("AI client not configured")

        # Try OmniRoute-style API
        if hasattr(self.ai_client, "generate_completion"):
            return self.ai_client.generate_completion(
                prompt=prompt,
                model=model,
                temperature=creativity,
                max_tokens=max_length // 4,  # Rough token estimate
            )

        # Fallback: try direct completion
        if hasattr(self.ai_client, "complete"):
            return self.ai_client.complete(prompt=prompt, model=model)

        raise RuntimeError("AI client does not support required methods")


# ── SEO Center ────────────────────────────────────────────────────────────

class SEOCenter:
    """SEO optimization for Shopware products."""

    def __init__(self, ai_client: Any | None = None) -> None:
        self.ai_client: Any | None = ai_client

    def generate_seo_data(
        self,
        canonical_product: CanonicalProduct,
        description: str | None = None,
    ) -> dict[str, Any]:
        """Generate SEO metadata for a product.

        Args:
            canonical_product: Product data
            description: Optional product description for keyword extraction

        Returns:
            Dict with seo_title, meta_description, url_slug, keywords, alt_texts
        """
        name = canonical_product.product_name or "Produkt"
        brand = canonical_product.brand or ""
        category = canonical_product.category or ""

        # Generate SEO title (max 70 chars)
        seo_title = f"{name} von {brand}" if brand else name
        if len(seo_title) > 70:
            seo_title = seo_title[:67] + "..."

        # Generate meta description (max 160 chars)
        meta_desc = description or canonical_product.raw_description or ""
        if len(meta_desc) > 160:
            meta_desc = meta_desc[:157] + "..."

        # Generate URL slug
        slug = self._generate_slug(name)

        # Extract keywords
        keywords = self._extract_keywords(canonical_product, description)

        # Generate image alt texts
        alt_texts = []
        for i, img in enumerate(canonical_product.images[:5]):
            alt_texts.append({
                "position": i + 1,
                "alt": f"{name} {brand} Produktbild {i + 1}".strip(),
                "title": name,
            })

        return {
            "seo_title": seo_title,
            "meta_description": meta_desc,
            "url_slug": slug,
            "keywords": keywords,
            "image_alt_texts": alt_texts,
            "canonical_product_sku": canonical_product.sku,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def enhance_seo_with_ai(
        self,
        seo_data: dict[str, Any],
        canonical_product: CanonicalProduct,
        model: str = "gemini-2.5-flash",
        seo_intensity: str = "medium",
        language: str = "de",
    ) -> dict[str, Any]:
        """Enhance SEO data using AI."""
        if not self.ai_client:
            return seo_data

        prompt = (
            f"Optimize the following SEO data for a product in {language}.\n"
            f"Product: {canonical_product.product_name}\n"
            f"Brand: {canonical_product.brand}\n"
            f"Category: {canonical_product.category}\n"
            f"Current SEO title: {seo_data.get('seo_title', '')}\n"
            f"Current meta description: {seo_data.get('meta_description', '')}\n"
            f"Current keywords: {', '.join(seo_data.get('keywords', []))}\n\n"
            f"Requirements:\n"
            f"- SEO intensity: {seo_intensity}\n"
            f"- No keyword stuffing\n"
            f"- Natural language\n"
            f"- Unique slug\n"
            f"- Max 70 chars for title, 160 for description\n"
            f"- Return JSON with: seo_title, meta_description, url_slug, keywords (array)"
        )

        try:
            if hasattr(self.ai_client, "generate_completion"):
                response = self.ai_client.generate_completion(
                    prompt=prompt, model=model, temperature=0.5, max_tokens=500
                )
                # Try to parse JSON response
                import json as _json
                enhanced = _json.loads(response.get("generated_text", "{}"))
                seo_data.update(enhanced)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("SEO AI enhancement failed: %s", exc)

        seo_data["ai_enhanced"] = True
        seo_data["ai_model"] = model
        seo_data["ai_seo_intensity"] = seo_intensity
        return seo_data

    # ── Utilities ─────────────────────────────────────────────────────────

    @staticmethod
    def _generate_slug(name: str) -> str:
        """Generate a URL-friendly slug from product name."""
        slug = name.lower()
        slug = re.sub(r"[äöüß]", lambda m: {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}[m.group()], slug)
        slug = re.sub(r"[^\w\s-]", "", slug)
        slug = re.sub(r"[\s_]+", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug[:200]

    @staticmethod
    def _extract_keywords(
        product: CanonicalProduct,
        description: str | None,
    ) -> list[str]:
        """Extract relevant keywords from product data."""
        keywords = []

        if product.brand:
            keywords.append(product.brand)
        if product.product_name:
            keywords.append(product.product_name)
        if product.category:
            keywords.append(product.category)
        if product.color:
            keywords.append(product.color)
        if product.material:
            keywords.append(product.material)
        if product.ean_codes:
            keywords.append(product.ean_codes[0])

        # Extract from description
        if description:
            words = re.findall(r"\b[a-zA-ZäöüÄÖÜß]{4,}\b", description.lower())
            common = {"und", "oder", "der", "die", "das", "mit", "für", "von", "zu", "in", "auf", "aus"}
            for w in words[:20]:
                if w not in common and w not in keywords:
                    keywords.append(w)

        # Deduplicate and limit
        seen = set()
        unique_keywords = []
        for k in keywords:
            k_lower = k.lower()
            if k_lower not in seen:
                seen.add(k_lower)
                unique_keywords.append(k)

        return unique_keywords[:10]