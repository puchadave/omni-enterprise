"""Competitor Intelligence - Product Matching Engine.

Implements multi-stage product matching between own products and
competitor products. Matching stages:

STUFE 1: Exakte EAN-Übereinstimmung (Exact EAN match)
STUFE 2: Hersteller-SKU und Modellidentifikation (SKU + Model)
STUFE 3: Marke, Produktname und Attribute (Brand, name, attributes)
STUFE 4: Bildähnlichkeit und visuelle Produkterkennung (Image similarity)
STUFE 5: Semantischer Vergleich der Produktbeschreibungen (Semantic)

Produces a Matching Score from 0-100 with classification:
- 95-100: Identisches Produkt
- 85-94: Sehr hohe Übereinstimmung
- 70-84: Vergleichbares Produkt
- 50-69: Ähnliches Produkt
- Unter 50: Nicht automatisch als Wettbewerbsvergleich verwenden
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .canonical_product_mapping import CanonicalProduct

logger = logging.getLogger(__name__)


# ── Match Classification ──────────────────────────────────────────────────

MATCH_IDENTICAL: str = "identisches_produkt"
MATCH_VERY_HIGH: str = "sehr_hohe_uebereinstimmung"
MATCH_COMPARABLE: str = "vergleichbares_produkt"
MATCH_SIMILAR: str = "ahnliches_produkt"
MATCH_NONE: str = "nicht_automatisch_verwendbar"


def classify_match_score(score: int) -> tuple[str, str]:
    """Classify a match score into a category.

    Returns (category_key, category_label).
    """
    if score >= 95:
        return MATCH_IDENTICAL, "Identisches Produkt"
    if score >= 85:
        return MATCH_VERY_HIGH, "Sehr hohe Übereinstimmung"
    if score >= 70:
        return MATCH_COMPARABLE, "Vergleichbares Produkt"
    if score >= 50:
        return MATCH_SIMILAR, "Ähnliches Produkt"
    return MATCH_NONE, "Nicht automatisch verwendbar"


# ── Match Result ──────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    """Result of matching one own product against one competitor product."""
    own_product_sku: str
    competitor_product_id: str
    match_score: int
    match_stage: str
    match_reason: str
    ai_confirmed: bool = False
    ai_confidence: float | None = None
    stage_details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        category_key, category_label = classify_match_score(self.match_score)
        return {
            "own_product_sku": self.own_product_sku,
            "competitor_product_id": self.competitor_product_id,
            "match_score": self.match_score,
            "match_stage": self.match_stage,
            "match_reason": self.match_reason,
            "match_category": category_key,
            "match_category_label": category_label,
            "ai_confirmed": self.ai_confirmed,
            "ai_confidence": self.ai_confidence,
            "stage_details": self.stage_details,
        }


# ── Product Matching Engine ───────────────────────────────────────────────

class ProductMatchingEngine:
    """Matches own products against competitor products using multiple stages."""

    def __init__(self, ai_client: Any | None = None) -> None:
        self.ai_client: Any | None = ai_client
        self._stage_weights: dict[str, int] = {
            "exact_ean": 100,
            "sku_model": 90,
            "brand_name_attrs": 75,
            "image_similarity": 60,
            "semantic": 50,
            "manual": 100,
        }

    def match(
        self,
        own_product: CanonicalProduct,
        competitor_products: list[dict[str, Any]],
        ai_confirm: bool = False,
    ) -> list[MatchResult]:
        """Match an own product against a list of competitor products.

        Returns sorted list of MatchResult (highest score first).
        """
        results: list[MatchResult] = []

        for comp in competitor_products:
            result = self._match_single(own_product, comp, ai_confirm)
            if result.match_score >= 50:  # Only include meaningful matches
                results.append(result)

        # Sort by score descending
        results.sort(key=lambda r: r.match_score, reverse=True)
        return results

    def _match_single(
        self,
        own_product: CanonicalProduct,
        competitor: dict[str, Any],
        ai_confirm: bool,
    ) -> MatchResult:
        """Match against a single competitor product through all stages."""

        # STUFE 1: Exact EAN match
        stage1 = self._stage_exact_ean(own_product, competitor)
        if stage1["matched"]:
            return MatchResult(
                own_product_sku=own_product.sku,
                competitor_product_id=competitor.get("id", ""),
                match_score=100,
                match_stage="exact_ean",
                match_reason="Exakte EAN-Übereinstimmung",
                stage_details=stage1.get("details", {}),
                ai_confirmed=True,
            )

        # STUFE 2: SKU + Model match
        stage2 = self._stage_sku_model(own_product, competitor)
        if stage2["matched"]:
            return MatchResult(
                own_product_sku=own_product.sku,
                competitor_product_id=competitor.get("id", ""),
                match_score=90,
                match_stage="sku_model",
                match_reason="Hersteller-SKU und Modellübereinstimmung",
                stage_details=stage2.get("details", {}),
                ai_confirmed=True,
            )

        # STUFE 3: Brand + Name + Attributes
        stage3 = self._stage_brand_name_attrs(own_product, competitor)
        score3 = stage3["score"]
        if score3 >= 70:
            return MatchResult(
                own_product_sku=own_product.sku,
                competitor_product_id=competitor.get("id", ""),
                match_score=score3,
                match_stage="brand_name_attrs",
                match_reason=stage3["reason"],
                stage_details=stage3.get("details", {}),
            )

        # STUFE 4: Image similarity (placeholder - would need image processing)
        stage4 = self._stage_image_similarity(own_product, competitor)
        score4 = stage4["score"]
        if score4 >= 70:
            return MatchResult(
                own_product_sku=own_product.sku,
                competitor_product_id=competitor.get("id", ""),
                match_score=score4,
                match_stage="image_similarity",
                match_reason=stage4["reason"],
                stage_details=stage4.get("details", {}),
            )

        # STUFE 5: Semantic comparison
        stage5 = self._stage_semantic(own_product, competitor)
        score5 = stage5["score"]
        if score5 >= 50:
            return MatchResult(
                own_product_sku=own_product.sku,
                competitor_product_id=competitor.get("id", ""),
                match_score=score5,
                match_stage="semantic",
                match_reason=stage5["reason"],
                stage_details=stage5.get("details", {}),
            )

        # No meaningful match
        return MatchResult(
            own_product_sku=own_product.sku,
            competitor_product_id=competitor.get("id", ""),
            match_score=0,
            match_stage="none",
            match_reason="Keine ausreichende Übereinstimmung gefunden",
            stage_details={},
        )

    # ── Stage Implementations ────────────────────────────────────────────

    def _stage_exact_ean(
        self,
        own_product: CanonicalProduct,
        competitor: dict[str, Any],
    ) -> dict[str, Any]:
        """STUFE 1: Exact EAN match."""
        own_eans = set(own_product.ean_codes)
        comp_ean = competitor.get("ean", "")

        if comp_ean and comp_ean in own_eans:
            return {
                "matched": True,
                "score": 100,
                "details": {"matched_ean": comp_ean},
            }
        return {"matched": False, "score": 0, "details": {}}

    def _stage_sku_model(
        self,
        own_product: CanonicalProduct,
        competitor: dict[str, Any],
    ) -> dict[str, Any]:
        """STUFE 2: SKU + Model match."""
        own_sku = own_product.sku.lower()
        own_supplier_id = own_product.supplier_product_id.lower()

        comp_sku = competitor.get("sku", "").lower()
        comp_name = competitor.get("product_name", "").lower()

        matched = False
        reason = ""

        if comp_sku and comp_sku == own_sku:
            matched = True
            reason = "Hersteller-SKU übereinstimmend"
        elif comp_sku and comp_sku == own_supplier_id:
            matched = True
            reason = "Lieferanten-SKU übereinstimmend"
        elif comp_sku and comp_sku in own_product.product_name.lower():
            matched = True
            reason = "SKU im Produktnamen gefunden"

        return {
            "matched": matched,
            "score": 90 if matched else 0,
            "reason": reason,
            "details": {"matched_sku": comp_sku if matched else None},
        }

    def _stage_brand_name_attrs(
        self,
        own_product: CanonicalProduct,
        competitor: dict[str, Any],
    ) -> dict[str, Any]:
        """STUFE 3: Brand + Name + Attributes match."""
        score = 0
        reasons: list[str] = []
        details: dict[str, Any] = {}

        # Brand match
        own_brand = (own_product.brand or "").lower()
        comp_brand = (competitor.get("brand", "") or "").lower()
        if own_brand and comp_brand and own_brand == comp_brand:
            score += 30
            reasons.append(f"Marke übereinstimmend: {own_brand}")
            details["brand_match"] = True

        # Product name similarity
        own_name = (own_product.product_name or "").lower()
        comp_name = (competitor.get("product_name", "") or "").lower()
        name_similarity = self._text_similarity(own_name, comp_name)
        if name_similarity >= 0.8:
            score += 30
            reasons.append(f"Produktname sehr ähnlich ({int(name_similarity * 100)}%)")
            details["name_similarity"] = round(name_similarity, 2)
        elif name_similarity >= 0.6:
            score += 15
            reasons.append(f"Produktname ähnlich ({int(name_similarity * 100)}%)")
            details["name_similarity"] = round(name_similarity, 2)

        # Category match
        own_cat = (own_product.category or "").lower()
        comp_cat = (competitor.get("category", "") or "").lower()
        if own_cat and comp_cat and own_cat == comp_cat:
            score += 10
            reasons.append("Kategorie übereinstimmend")
            details["category_match"] = True

        # Color match
        own_color = (own_product.color or "").lower()
        comp_color = (competitor.get("color", "") or "").lower()
        if own_color and comp_color and own_color == comp_color:
            score += 10
            reasons.append("Farbe übereinstimmend")
            details["color_match"] = True

        # Material match
        own_mat = (own_product.material or "").lower()
        comp_mat = (competitor.get("material", "") or "").lower()
        if own_mat and comp_mat and own_mat == comp_mat:
            score += 10
            reasons.append("Material übereinstimmend")
            details["material_match"] = True

        return {
            "matched": score >= 70,
            "score": min(score, 100),
            "reason": "; ".join(reasons) if reasons else "Keine Übereinstimmung",
            "details": details,
        }

    def _stage_image_similarity(
        self,
        own_product: CanonicalProduct,
        competitor: dict[str, Any],
    ) -> dict[str, Any]:
        """STUFE 4: Image similarity (placeholder).

        Actual image similarity would require image processing libraries
        (e.g., PIL, OpenCV, or ML models). This is a structural placeholder.
        """
        # Placeholder: would compare image hashes or use ML embeddings
        own_images = own_product.images or []
        comp_images = competitor.get("images", [])

        if own_images and comp_images:
            # Basic check: same number of images suggests similar product
            if len(own_images) == len(comp_images):
                return {"matched": True, "score": 60, "reason": "Anzahl Produktbilder identisch", "details": {}}

        return {"matched": False, "score": 0, "details": {}}

    def _stage_semantic(
        self,
        own_product: CanonicalProduct,
        competitor: dict[str, Any],
    ) -> dict[str, Any]:
        """STUFE 5: Semantic comparison of descriptions."""
        own_desc = (own_product.raw_description or own_product.description or "").lower()
        comp_desc = (competitor.get("description", "") or "").lower()

        if not own_desc or not comp_desc:
            return {"matched": False, "score": 0, "details": {}}

        similarity = self._text_similarity(own_desc, comp_desc)

        if similarity >= 0.5:
            return {
                "matched": True,
                "score": int(similarity * 50),
                "reason": f"Semantische Beschreibungähnlichkeit ({int(similarity * 100)}%)",
                "details": {"semantic_similarity": round(similarity, 2)},
            }

        return {"matched": False, "score": 0, "details": {}}

    # ── Utilities ─────────────────────────────────────────────────────────

    @staticmethod
    def _text_similarity(text1: str, text2: str) -> float:
        """Compute simple text similarity using word overlap (Jaccard-like)."""
        if not text1 or not text2:
            return 0.0

        words1 = set(re.findall(r"\b\w{3,}\b", text1.lower()))
        words2 = set(re.findall(r"\b\w{3,}\b", text2.lower()))

        if not words1 or not words2:
            return 0.0

        intersection = words1 & words2
        union = words1 | words2

        return len(intersection) / len(union) if union else 0.0

    async def confirm_with_ai(
        self,
        match_result: MatchResult,
        own_product: CanonicalProduct,
        competitor_product: dict[str, Any],
    ) -> MatchResult:
        """Confirm a match using AI analysis.

        Only used for matches with score 50-94 that need manual verification.
        """
        if not self.ai_client:
            return match_result

        if match_result.match_score >= 95:
            match_result.ai_confirmed = True
            match_result.ai_confidence = 1.0
            return match_result

        prompt = (
            f"Analyze if these two products are the same or comparable:\n\n"
            f"Product A (our): {own_product.product_name}\n"
            f"Brand A: {own_product.brand}\n"
            f"Category A: {own_product.category}\n"
            f"EAN A: {own_product.ean_codes}\n"
            f"Description A: {own_product.raw_description or own_product.description}\n\n"
            f"Product B (competitor): {competitor_product.get('product_name', '')}\n"
            f"Brand B: {competitor_product.get('brand', '')}\n"
            f"Category B: {competitor_product.get('category', '')}\n"
            f"EAN B: {competitor_product.get('ean', '')}\n"
            f"Description B: {competitor_product.get('description', '')}\n\n"
            f"Current match score: {match_result.match_score}/100\n"
            f"Respond with JSON: {{\"confirmed\": true/false, \"confidence\": 0.0-1.0, \"reason\": \"explanation\"}}"
        )

        try:
            if hasattr(self.ai_client, "generate_completion"):
                response = self.ai_client.generate_completion(
                    prompt=prompt, model="gemini-2.5-flash", temperature=0.3, max_tokens=300
                )
                import json as _json
                ai_result = _json.loads(response.get("generated_text", "{}"))
                match_result.ai_confirmed = ai_result.get("confirmed", False)
                match_result.ai_confidence = ai_result.get("confidence", 0.0)
                if ai_result.get("reason"):
                    match_result.match_reason += f" (KI: {ai_result['reason']})"
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("AI match confirmation failed: %s", exc)

        return match_result