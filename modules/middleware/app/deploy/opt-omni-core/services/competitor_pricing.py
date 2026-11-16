"""Competitor Intelligence - Dynamic Pricing Engine.

Calculates price recommendations based on market data, competitor prices,
and business rules. Supports multiple pricing strategies:

A. PREMIUM POSITIONING - Price above market average
B. MARKET AVERAGE - Orient at average market price
C. COMPETITIVE PRICING - Just below a defined competitor price
D. MARGIN OPTIMIZATION - Maximize contribution margin
E. CLEARANCE - Sell-off strategy

ABSOLUTE RULE: Selling price must never fall below the configured
economic minimum threshold. Automatic price changes are disabled by default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Pricing Strategies ────────────────────────────────────────────────────

STRATEGY_PREMIUM: str = "premium_positioning"
STRATEGY_MARKET_AVERAGE: str = "market_average"
STRATEGY_COMPETITIVE: str = "competitive_pricing"
STRATEGY_MARGIN: str = "margin_optimization"
STRATEGY_CLEARANCE: str = "clearance"


# ── Pricing Result ────────────────────────────────────────────────────────

@dataclass
class PricingResult:
    """Result of a price calculation."""
    own_product_sku: str
    current_price: Decimal
    recommended_price: Decimal
    market_average: Decimal
    market_min: Decimal
    expected_margin: Decimal
    contribution_margin: Decimal
    price_deviation: Decimal
    strategy: str
    justification: str
    data_quality: str
    risk_indicator: str
    is_approved: bool = False
    applied_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "own_product_sku": self.own_product_sku,
            "current_price": str(self.current_price),
            "recommended_price": str(self.recommended_price),
            "market_average": str(self.market_average),
            "market_min": str(self.market_min),
            "expected_margin": str(self.expected_margin),
            "contribution_margin": str(self.contribution_margin),
            "price_deviation": str(self.price_deviation),
            "strategy": self.strategy,
            "justification": self.justification,
            "data_quality": self.data_quality,
            "risk_indicator": self.risk_indicator,
            "is_approved": self.is_approved,
            "applied_at": self.applied_at,
        }


# ── Pricing Rule ──────────────────────────────────────────────────────────

@dataclass
class PricingRule:
    """A pricing rule for a specific entity scope."""
    rule_id: str
    name: str
    rule_type: str  # global, brand, category, product, sales_channel
    target_entity_id: str
    min_margin: Decimal = Decimal("0.0")
    target_margin: Decimal | None = None
    markup_percent: Decimal | None = None
    min_selling_price: Decimal | None = None
    max_selling_price: Decimal | None = None
    strategy: str = STRATEGY_MARKET_AVERAGE
    is_active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "rule_type": self.rule_type,
            "target_entity_id": self.target_entity_id,
            "min_margin": str(self.min_margin),
            "target_margin": str(self.target_margin) if self.target_margin else None,
            "markup_percent": str(self.markup_percent) if self.markup_percent else None,
            "min_selling_price": str(self.min_selling_price) if self.min_selling_price else None,
            "max_selling_price": str(self.max_selling_price) if self.max_selling_price else None,
            "strategy": self.strategy,
            "is_active": self.is_active,
        }


# ── Dynamic Pricing Engine ────────────────────────────────────────────────

class DynamicPricingEngine:
    """Calculates price recommendations based on market and competitor data."""

    def __init__(self, rules: list[PricingRule] | None = None) -> None:
        self.rules: list[PricingRule] = rules or []
        self._rules_by_type: dict[str, list[PricingRule]] = {}
        self._index_rules()

    def _index_rules(self) -> None:
        """Index rules by type for fast lookup."""
        self._rules_by_type = {}
        for rule in self.rules:
            if rule.is_active:
                self._rules_by_type.setdefault(rule.rule_type, []).append(rule)

    def add_rule(self, rule: PricingRule) -> None:
        """Add a pricing rule."""
        self.rules.append(rule)
        self._index_rules()

    def remove_rule(self, rule_id: str) -> None:
        """Remove a pricing rule by ID."""
        self.rules = [r for r in self.rules if r.rule_id != rule_id]
        self._index_rules()

    def get_applicable_rule(
        self,
        product_sku: str,
        brand: str | None = None,
        category: str | None = None,
        sales_channel: str | None = None,
    ) -> PricingRule | None:
        """Get the most specific applicable pricing rule for a product.

        Priority: product > brand > category > sales_channel > global
        """
        # Check product-specific rule
        for rule in self._rules_by_type.get("product", []):
            if rule.target_entity_id == product_sku:
                return rule

        # Check brand rule
        if brand:
            for rule in self._rules_by_type.get("brand", []):
                if rule.target_entity_id == brand:
                    return rule

        # Check category rule
        if category:
            for rule in self._rules_by_type.get("category", []):
                if rule.target_entity_id == category:
                    return rule

        # Check sales channel rule
        if sales_channel:
            for rule in self._rules_by_type.get("sales_channel", []):
                if rule.target_entity_id == sales_channel:
                    return rule

        # Check global rule
        for rule in self._rules_by_type.get("global", []):
            return rule

        return None

    # ── Price Calculation ───────────────────────────────────────────────

    def calculate_price(
        self,
        own_product_sku: str,
        supplier_price_netto: Decimal,
        current_selling_price: Decimal,
        market_prices: list[Decimal],
        brand: str | None = None,
        category: str | None = None,
        sales_channel: str | None = None,
        tax_rate_percent: Decimal = Decimal("19.0"),
        shipping_cost: Decimal = Decimal("0.0"),
        packaging_cost: Decimal = Decimal("0.0"),
        platform_fee_percent: Decimal = Decimal("0.0"),
        currency: str = "EUR",
    ) -> PricingResult:
        """Calculate a price recommendation.

        Args:
            own_product_sku: The product's SKU
            supplier_price_netto: Supplier purchase price (netto)
            current_selling_price: Current selling price
            market_prices: List of competitor market prices
            brand: Product brand (for rule lookup)
            category: Product category (for rule lookup)
            sales_channel: Sales channel (for rule lookup)
            tax_rate_percent: VAT rate (default 19%)
            shipping_cost: Shipping cost per unit
            packaging_cost: Packaging cost per unit
            platform_fee_percent: Marketplace fee percent
            currency: Currency code

        Returns:
            PricingResult with all calculated values
        """
        # Get applicable rule
        rule = self.get_applicable_rule(own_product_sku, brand, category, sales_channel)
        strategy = rule.strategy if rule else STRATEGY_MARKET_AVERAGE
        min_margin = rule.min_margin if rule else Decimal("0.15")

        # Calculate market statistics
        if market_prices:
            market_average = sum(market_prices) / Decimal(len(market_prices))
            market_min = min(market_prices)
        else:
            market_average = current_selling_price
            market_min = current_selling_price

        # Calculate cost base (total cost per unit)
        cost_base = supplier_price_netto + shipping_cost + packaging_cost
        cost_with_tax = cost_base * (1 + tax_rate_percent / Decimal("100"))

        # Add platform fees
        platform_fee = cost_with_tax * (platform_fee_percent / Decimal("100"))
        total_cost = cost_with_tax + platform_fee

        # Calculate recommended price based on strategy
        recommended = self._apply_strategy(
            strategy=strategy,
            market_average=market_average,
            market_min=market_min,
            current_price=current_selling_price,
            total_cost=total_cost,
            min_margin=min_margin,
            target_margin=rule.target_margin if rule else None,
            markup_percent=rule.markup_percent if rule else None,
        )

        # Apply absolute minimum price rule
        min_price = rule.min_selling_price if rule and rule.min_selling_price else None
        if min_price is None:
            # Economic minimum: cost + minimum margin
            min_price = total_cost * (1 + min_margin)

        if recommended < min_price:
            recommended = min_price
            risk = "high"
            justification = (
                f"Preis auf wirtschaftliche Mindestgrenze angehoben "
                f"(Mindestmarge: {min_margin * 100:.0f}%)"
            )
        else:
            risk = "low"
            justification = self._build_justification(
                strategy, recommended, market_average, current_selling_price
            )

        # Calculate margins
        margin_amount = recommended - total_cost
        expected_margin = margin_amount / recommended if recommended > 0 else Decimal("0")
        contribution_margin = margin_amount / recommended if recommended > 0 else Decimal("0")

        # Price deviation from market
        price_deviation = (recommended - market_average) / market_average if market_average > 0 else Decimal("0")

        # Data quality assessment
        data_quality = self._assess_data_quality(len(market_prices), strategy)

        return PricingResult(
            own_product_sku=own_product_sku,
            current_price=current_selling_price,
            recommended_price=recommended.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            market_average=market_average.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            market_min=market_min.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            expected_margin=expected_margin.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            contribution_margin=contribution_margin.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            price_deviation=price_deviation.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            strategy=strategy,
            justification=justification,
            data_quality=data_quality,
            risk_indicator=risk,
        )

    def _apply_strategy(
        self,
        strategy: str,
        market_average: Decimal,
        market_min: Decimal,
        current_price: Decimal,
        total_cost: Decimal,
        min_margin: Decimal,
        target_margin: Decimal | None,
        markup_percent: Decimal | None,
    ) -> Decimal:
        """Apply a specific pricing strategy."""

        if strategy == STRATEGY_PREMIUM:
            # Price above market average (5-15% premium)
            premium_factor = Decimal("1.10")
            return market_average * premium_factor

        if strategy == STRATEGY_MARKET_AVERAGE:
            # Orient at average market price
            return market_average

        if strategy == STRATEGY_COMPETITIVE:
            # Just below the cheapest competitor (95% of min)
            return market_min * Decimal("0.95")

        if strategy == STRATEGY_MARGIN:
            # Maximize contribution margin with target margin
            if target_margin:
                return total_cost / (1 - target_margin)
            # Default: 30% margin
            return total_cost / Decimal("0.70")

        if strategy == STRATEGY_CLEARANCE:
            # Sell-off: at or slightly above cost
            return total_cost * Decimal("1.05")

        # Default fallback
        return market_average

    # ── Utilities ─────────────────────────────────────────────────────────

    def _build_justification(
        self,
        strategy: str,
        recommended: Decimal,
        market_average: Decimal,
        current_price: Decimal,
    ) -> str:
        """Build human-readable justification for the price recommendation."""
        if strategy == STRATEGY_PREMIUM:
            deviation = ((recommended - market_average) / market_average * 100) if market_average > 0 else Decimal("0")
            return (
                f"Premium-Positionierung: {deviation:.0f}% über dem Marktdurchschnitt. "
                f"Empfohlener VK: {recommended:.2f} EUR."
            )
        if strategy == STRATEGY_MARKET_AVERAGE:
            return f"Marktdurchschnitts-Preis: {recommended:.2f} EUR orientiert am Markt."
        if strategy == STRATEGY_COMPETITIVE:
            deviation = ((market_average - recommended) / market_average * 100) if market_average > 0 else Decimal("0")
            return (
                f"Wettbewerbspreis: {deviation:.0f}% unter dem Marktdurchschnitt. "
                f"Empfohlener VK: {recommended:.2f} EUR."
            )
        if strategy == STRATEGY_MARGIN:
            return f"Marge-Optimierung: Zielmarge berücksichtigt bei VK {recommended:.2f} EUR."
        if strategy == STRATEGY_CLEARANCE:
            return f"Abverkaufsstrategie: Schneller Abverkauf bei VK {recommended:.2f} EUR."

        return f"Preisempfehlung: {recommended:.2f} EUR basierend auf {strategy}."

    def _assess_data_quality(self, competitor_count: int, strategy: str) -> str:
        """Assess the quality of market data for the recommendation."""
        if competitor_count == 0:
            return "niedrig"  # No competitor data
        if competitor_count < 3:
            return "mittel"  # Few competitors
        if strategy in (STRATEGY_PREMIUM, STRATEGY_COMPETITIVE) and competitor_count < 5:
            return "mittel"  # Need more data for competitive strategies
        return "hoch"  # Good data quality

    # ── Bulk Calculation ─────────────────────────────────────────────────

    def calculate_bulk(
        self,
        products: list[dict[str, Any]],
    ) -> list[PricingResult]:
        """Calculate prices for multiple products at once.

        Each product dict should contain:
        - sku, supplier_price_netto, current_selling_price
        - market_prices (list of Decimal)
        - Optional: brand, category, sales_channel, tax_rate_percent, etc.
        """
        results = []
        for p in products:
            result = self.calculate_price(
                own_product_sku=p["sku"],
                supplier_price_netto=Decimal(str(p.get("supplier_price_netto", 0))),
                current_selling_price=Decimal(str(p.get("current_selling_price", 0))),
                market_prices=[Decimal(str(mp)) for mp in p.get("market_prices", [])],
                brand=p.get("brand"),
                category=p.get("category"),
                sales_channel=p.get("sales_channel"),
                tax_rate_percent=Decimal(str(p.get("tax_rate_percent", 19.0))),
                shipping_cost=Decimal(str(p.get("shipping_cost", 0))),
                packaging_cost=Decimal(str(p.get("packaging_cost", 0))),
                platform_fee_percent=Decimal(str(p.get("platform_fee_percent", 0))),
                currency=p.get("currency", "EUR"),
            )
            results.append(result)
        return results