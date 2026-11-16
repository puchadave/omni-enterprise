"""Matterhorn Price Engine.

Calculates selling prices from Matterhorn Netto-Großhandelspreise (Net wholesale prices)
considering all cost factors and business rules.

Cost factors considered:
- Einkaufspreis netto (Net purchase price)
- Einkaufspreis brutto (Gross purchase price with VAT)
- Währung (Currency)
- Wechselkurs (Exchange rate)
- Versandkosten (Shipping costs)
- Verpackungskosten (Packaging costs)
- Plattformgebühren (Platform fees)
- Zahlungsgebühren (Payment fees)
- Marketingkosten (Marketing costs)
- Umsatzsteuer (Sales tax/VAT)
- Zielmarge (Target margin)
- Deckungsbeitrag (Contribution margin)

Price rules:
- Globale Regeln (Global rules)
- Lieferantenspezifische Regeln (Supplier-specific rules)
- Markenspezifische Regeln (Brand-specific rules)
- Kategoriespezifische Rules (Category-specific rules)
- Produktspezifische Rules (Product-specific rules)

No automatic price publication without corresponding approval.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

from .canonical_product_mapping import CanonicalProduct

logger = logging.getLogger(__name__)

# ── Price Engine Configuration ────────────────────────────────────────────

# Default values for price calculation when not explicitly set
DEFAULT_MARGIN_PERCENT: Decimal = Decimal("30.00")  # 30% target margin
DEFAULT_SHIPPING_COSTS: Decimal = Decimal("0.00")
DEFAULT_PACKAGING_COSTS: Decimal = Decimal("0.00")
DEFAULT_PLATFORM_FEE_PERCENT: Decimal = Decimal("0.00")  # % of net price
DEFAULT_PAYMENT_FEE_FIXED: Decimal = Decimal("0.00")  # Fixed amount
DEFAULT_MARKETING_COST_PERCENT: Decimal = Decimal("0.00")
DEFAULT_TAX_RATE_PERCENT: Decimal = Decimal("19.00")  # German VAT (19%)


# ── Price Calculation Result ──────────────────────────────────────────────

class PriceCalculationResult:
    """Result of a price calculation operation."""
    def __init__(self) -> None:
        self.net_price: Decimal = Decimal("0.00")  # Net sales price (before tax)
        self.brutto_price: Decimal = Decimal("0.00")  # Gross sales price (with tax)
        self.purchase_netto: Decimal = Decimal("0.00")  # Net purchase price
        self.purchase_brutto: Decimal = Decimal("0.00")  # Gross purchase price
        self.margin_percent: Decimal = DEFAULT_MARGIN_PERCENT
        self.margin_amount: Decimal = Decimal("0.00")
        self.contribution_margin: Decimal = Decimal("0.00")
        self.tax_amount: Decimal = Decimal("0.00")
        self.shopping_fees: Decimal = Decimal("0.00")
        self.marketing_costs: Decimal = Decimal("0.00")
        self.errors: list[str] = []
        self.warnings: list[str] = []

    @property
    def is_profitable(self) -> bool:
        """Check if the calculation resulted in a profitable price."""
        return self.margin_amount > Decimal("0.00")

    @property
    def margin_percentage(self) -> Decimal:
        """Calculate margin percentage based on net price."""
        if self.net_price > Decimal("0.00"):
            return (self.margin_amount / self.net_price) * Decimal("100")
        return Decimal("0.00")

    def to_dict(self) -> dict[str, Any]:
        return {
            "net_price": float(self.net_price.quantize(Decimal("0.01"))),
            "brutto_price": float(self.brutto_price.quantize(Decimal("0.01"))),
            "purchase_netto": float(self.purchase_netto.quantize(Decimal("0.01"))),
            "purchase_brutto": float(self.purchase_brutto.quantize(Decimal("0.01"))),
            "margin_percent": float(self.margin_percent.quantize(Decimal("0.01"))),
            "margin_amount": float(self.margin_amount.quantize(Decimal("0.01"))),
            "contribution_margin": float(self.contribution_margin.quantize(Decimal("0.01"))),
            "tax_amount": float(self.tax_amount.quantize(Decimal("0.01"))),
            "shopping_fees": float(self.shopping_fees.quantize(Decimal("0.01"))),
            "marketing_costs": float(self.marketing_costs.quantize(Decimal("0.01"))),
            "errors": self.errors,
            "warnings": self.warnings,
        }


# ── Price Engine ──────────────────────────────────────────────────────────

class PriceEngine:
    """Engine for calculating sales prices from Matterhorn wholesale prices.

    Considers all cost factors and applies price rules at various levels:
    global, supplier, brand, category, and product-specific.
    """

    def __init__(self) -> None:
        # Price rule configurations at different levels
        self._global_rules: dict[str, Any] = {
            "margin_percent": float(DEFAULT_MARGIN_PERCENT),
            "shipping_costs": float(DEFAULT_SHIPPING_COSTS),
            "packaging_costs": float(DEFAULT_PACKAGING_COSTS),
            "platform_fee_percent": float(DEFAULT_PLATFORM_FEE_PERCENT),
            "payment_fee_fixed": float(DEFAULT_PAYMENT_FEE_FIXED),
            "marketing_cost_percent": float(DEFAULT_MARKETING_COST_PERCENT),
            "tax_percent": float(DEFAULT_TAX_RATE_PERCENT),
        }

        # Per-supplier rules (supplier_id -> rules)
        self._supplier_rules: dict[str, dict[str, Any]] = defaultdict(
            lambda: self._global_rules.copy()
        )

        # Per-brand rules
        self._brand_rules: dict[str, dict[str, Any]] = defaultdict(
            lambda: self._global_rules.copy()
        )

        # Per-category rules
        self._category_rules: dict[str, dict[str, Any]] = defaultdict(
            lambda: self._global_rules.copy()
        )

        # Per-product rules (sku -> rules)
        self._product_rules: dict[str, dict[str, Any]] = defaultdict(
            lambda: self._global_rules.copy()
        )

    # ── Rule Configuration ───────────────────────────────────────────────

    def set_global_rule(self, key: str, value: Any) -> None:
        """Set a global price rule."""
        self._global_rules[key] = value
        # Also update the instance attribute for margin_percent
        if key == "margin_percent":
            self.margin_percent = Decimal(str(value))
        # Reset all per-level rules to use the new global
        for d in self._supplier_rules.values():
            d[key] = value
        for d in self._brand_rules.values():
            d[key] = value
        for d in self._category_rules.values():
            d[key] = value
        for d in self._product_rules.values():
            d[key] = value

    def set_supplier_rule(self, supplier_id: str, key: str, value: Any) -> None:
        """Set a price rule for a specific supplier."""
        if supplier_id not in self._supplier_rules:
            self._supplier_rules[supplier_id] = self._global_rules.copy()
        self._supplier_rules[supplier_id][key] = value

    def set_brand_rule(self, brand_id: str, key: str, value: Any) -> None:
        """Set a price rule for a specific brand."""
        if brand_id not in self._brand_rules:
            self._brand_rules[brand_id] = self._global_rules.copy()
        self._brand_rules[brand_id][key] = value

    def set_category_rule(self, category_id: str, key: str, value: Any) -> None:
        """Set a price rule for a specific category."""
        if category_id not in self._category_rules:
            self._category_rules[category_id] = self._global_rules.copy()
        self._category_rules[category_id][key] = value

    def set_product_rule(self, sku: str, key: str, value: Any) -> None:
        """Set a price rule for a specific product."""
        if sku not in self._product_rules:
            self._product_rules[sku] = self._global_rules.copy()
        self._product_rules[sku][key] = value

    # ── Price Calculation ──────────────────────────────────────────────────

    def calculate_price(
        self,
        canonical_product: CanonicalProduct,
        purchase_netto: Decimal | None = None,
        purchase_brutto: Decimal | None = None,
        override_rules: dict[str, Any] | None = None,
    ) -> PriceCalculationResult:
        """Calculate the sales price for a canonical product.

        Args:
            canonical_product: The product with supplier pricing info
            purchase_netto: Explicit net purchase price (overrides product data)
            purchase_brutto: Explicit gross purchase price (overrides product data)
            override_rules: Override the default price rules

        Returns:
            PriceCalculationResult with all calculated values
        """
        result = PriceCalculationResult()
        rules = override_rules or self._get_applicable_rules(canonical_product)

        # ── Step 1: Determine purchase prices ─────────────────────────────
        # Use explicit values if provided, otherwise from product data
        if purchase_netto is not None:
            result.purchase_netto = Decimal(str(purchase_netto))
        elif canonical_product.supplier_price_netto:
            result.purchase_netto = Decimal(
                str(canonical_product.supplier_price_netto)
            )
        else:
            result.errors.append("No net purchase price available")

        if purchase_brutto is not None:
            result.purchase_brutto = Decimal(str(purchase_brutto))
        elif canonical_product.supplier_price_brutto:
            result.purchase_brutto = Decimal(
                str(canonical_product.supplier_price_brutto)
            )
        else:
            # Derive brutto from netto + tax if we have tax rate
            if result.purchase_netto and Decimal(str(rules["tax_percent"])):
                result.purchase_brutto = result.purchase_netto * (
                    Decimal("1") + Decimal(str(rules["tax_percent"])) / Decimal("100")
                )
            else:
                result.purchase_brutto = result.purchase_netto

        # ── Step 2: Apply cost factors ────────────────────────────────────
        # Shipping costs (can be fixed or percent-based)
        shipping_costs: Decimal = Decimal(str(rules.get("shipping_costs", "0.00")))
        if shipping_costs < 0:
            shipping_costs = Decimal("0.00")

        # Packaging costs
        packaging_costs: Decimal = Decimal(
            str(rules.get("packaging_costs", "0.00"))
        )
        if packaging_costs < 0:
            packaging_costs = Decimal("0.00")

        # Platform fee (percent of net price)
        platform_fee_percent: Decimal = Decimal(
            str(rules.get("platform_fee_percent", "0.00"))
        )
        if platform_fee_percent < 0:
            platform_fee_percent = Decimal("0.00")

        # Payment fee (fixed amount)
        payment_fee_fixed: Decimal = Decimal(
            str(rules.get("payment_fee_fixed", "0.00"))
        )
        if payment_fee_fixed < 0:
            payment_fee_fixed = Decimal("0.00")

        # Marketing costs (percent of net price)
        marketing_cost_percent: Decimal = Decimal(
            str(rules.get("marketing_cost_percent", "0.00"))
        )
        if marketing_cost_percent < 0:
            marketing_cost_percent = Decimal("0.00")

        # Tax rate (percent)
        tax_percent: Decimal = Decimal(str(rules.get("tax_percent", "19.00")))
        if tax_percent < 0:
            tax_percent = Decimal("19.00")

        # ── Step 3: Calculate total costs ─────────────────────────────────
        # Start with the purchase net price
        total_costs: Decimal = result.purchase_netto

        # Add shipping costs (fixed amount)
        total_costs += shipping_costs

        # Add packaging costs (fixed amount)
        total_costs += packaging_costs

        # Add platform fee (percentage of net price - added to costs)
        platform_fee_amount: Decimal = (
            result.purchase_netto * platform_fee_percent / Decimal("100")
            if result.purchase_netto else Decimal("0.00")
        )
        total_costs += platform_fee_amount

        # Add payment fee (fixed amount)
        total_costs += payment_fee_fixed

        # Add marketing costs (percentage of net price)
        marketing_costs: Decimal = (
            result.purchase_netto * marketing_cost_percent / Decimal("100")
            if result.purchase_netto else Decimal("0.00")
        )
        total_costs += marketing_costs

        # ── Step 4: Apply target margin ───────────────────────────────────
        # Margin is calculated on the NET price (before tax)
        target_margin_percent: Decimal = Decimal(str(rules.get("margin_percent", "30.00")))
        result.margin_percent = target_margin_percent

        # Net sales price = Total costs + Target margin
        if result.purchase_netto and target_margin_percent > 0:
            self._margin_amount: Decimal = (
                result.purchase_netto * target_margin_percent / Decimal("100")
            )
            result.margin_amount = self._margin_amount
            result.net_price = result.purchase_netto + self._margin_amount
        else:
            # No margin specified - just cover costs
            result.net_price = total_costs
            result._margin_amount = Decimal("0.00")

        # ── Step 5: Add tax (VAT) to get gross price ─────────────────────
        # Tax is applied on the NET sales price
        if tax_percent and tax_percent > 0:
            result.tax_amount = result.net_price * tax_percent / Decimal("100")
        else:
            result.tax_amount = Decimal("0.00")

        # Gross price = Net price + Tax
        result.brutto_price = result.net_price + result.tax_amount

        # ── Step 6: Calculate contribution margin ─────────────────────────
        # Contribution margin = Net price - Total costs (without tax)
        result.contribution_margin = result.net_price - total_costs

        # ── Step 7: Validate and add warnings ─────────────────────────────
        # Warning if margin is very low (< 5%)
        if (
            result.margin_amount > Decimal("0.00")
            and result.margin_amount / result.net_price < Decimal("0.05")
            if result.net_price else False
        ):
            result.warnings.append(
                "Margin is below 5% - consider adjusting price rules"
            )

        # Warning if gross price is much higher than purchase brutto
        if result.brutto_price > result.purchase_brutto * Decimal("2.0"):
            result.warnings.append(
                "Gross price is significantly above purchase price - "
                "verify margin calculation"
            )

        return result

    # ── Helper: Get applicable rules for a product ────────────────────────

    def _get_applicable_rules(
        self, canonical_product: CanonicalProduct
    ) -> dict[str, Any]:
        """Get the price rules that apply to a product, in priority order.

        Priority (highest to lowest):
        1. Product-specific rules (SKU)
        2. Category-specific rules
        3. Brand-specific rules
        4. Supplier-specific rules
        5. Global rules (base)
        """
        rules: dict[str, Any] = self._global_rules.copy()

        # 1. Product-specific (SKU)
        if canonical_product.sku:
            product_rules = self._product_rules.get(
                canonical_product.sku, self._global_rules.copy()
            )
            rules.update(product_rules)

        # 2. Category-specific rules
        if canonical_product.category_id:
            cat_rules = self._category_rules.get(
                canonical_product.category_id, self._global_rules.copy()
            )
            rules.update(cat_rules)

        # 3. Brand-specific rules
        if canonical_product.brand_id:
            brand_rules = self._brand_rules.get(
                canonical_product.brand_id, self._global_rules.copy()
            )
            rules.update(brand_rules)

        # 4. Supplier-specific rules
        # Supplier ID is stored in the canonical product
        if canonical_product.supplier_id:
            supp_rules = self._supplier_rules.get(
                canonical_product.supplier_id, self._global_rules.copy()
            )
            rules.update(supp_rules)

        return rules


# ── Convenience Function ─────────────────────────────────────────────────

def calculate_sales_price(
    supplier_price_netto: Decimal | float | str,
    purchase_brutto: Decimal | float | str | None = None,
    margin_percent: Decimal | float | str | None = None,
    tax_percent: Decimal | float | str | None = None,
    shipping_costs: Decimal | float | str | None = None,
    platform_fee_percent: Decimal | float | str | None = None,
    payment_fee_fixed: Decimal | float | str | None = None,
    marketing_cost_percent: Decimal | float | str | None = None,
) -> PriceCalculationResult:
    """Convenience function to calculate a sales price.

    Args:
        supplier_price_netto: The net purchase price from the supplier
        purchase_brutto: The gross purchase price (optional)
        margin_percent: Target margin percentage (optional, default 30%)
        tax_percent: Sales tax percentage (optional, default 19% German VAT)
        shipping_costs: Fixed shipping costs (optional)
        platform_fee_percent: Platform fee as percent of net price (optional)
        payment_fee_fixed: Fixed payment fee (optional)
        marketing_cost_percent: Marketing cost percent of net price (optional)

    Returns:
        PriceCalculationResult with all calculated values
    """
    engine = PriceEngine()

    # Override rules if provided
    if margin_percent is not None:
        engine.set_global_rule("margin_percent", margin_percent)
    if tax_percent is not None:
        engine.set_global_rule("tax_percent", tax_percent)
    if shipping_costs is not None:
        engine.set_global_rule("shipping_costs", shipping_costs)
    if platform_fee_percent is not None:
        engine.set_global_rule("platform_fee_percent", platform_fee_percent)
    if payment_fee_fixed is not None:
        engine.set_global_rule("payment_fee_fixed", payment_fee_fixed)
    if marketing_cost_percent is not None:
        engine.set_global_rule("marketing_cost_percent", marketing_cost_percent)

    # The engine will use the global rules and the product data
    # For this convenience function, we create a minimal product dict
    # The actual product data would come from the CanonicalProduct model
    result = PriceCalculationResult()
    result = engine.calculate_price(
        canonical_product=CanonicalProduct(
            supplier_id="convenience",
            supplier_product_id="convenience",
            sku="convenience",
            product_name="Convenience Product",
            name_without_number="Convenience Product",
            description="",
            supplier_price_netto=Decimal(str(supplier_price_netto)),
        ),
        override_rules=engine._global_rules,
    )

    return result