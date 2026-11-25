from typing import List, Optional

from .enrichment_contracts import ProductSource, DescriptionSeoResult
from .error_codes import ErrorCode

class DescriptionEnrichmentService:
    def __init__(self, locale: str):
        self.locale = locale

    def enrich(self, product: ProductSource) -> DescriptionSeoResult:
        # Generate title
        title = product.name[:70]  # Limit to 70 characters

        # Generate meta description
        meta_description = product.description[:160]  # Limit to 160 characters

        # Generate slug
        slug = self._normalize_slug(product.name[:200])  # Limit to 200 characters

        # Generate alt texts for images
        alt_texts = [f"{product.name} image {i + 1}" for i in range(len(product.images))]

        return DescriptionSeoResult(
            title=title,
            meta_description=meta_description,
            slug=slug,
            alt_texts=alt_texts,
        )

    def _normalize_slug(self, text: str) -> str:
        # Normalize slug by replacing umlauts and special characters
        text = text.lower()
        text = text.replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        text = text.replace("ß", "ss")
        text = "".join(c for c in text if c.isalnum() or c == " ")
        text = text.replace(" ", "-")
        return text
