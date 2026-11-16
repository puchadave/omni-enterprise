"""Pricing engine for Omni-Core Middleware.

Calculates the full cost breakdown and final selling price for a product
given its supplier price and the configured fee/commission rates.

Logic is pure (no I/O, no mutable state) and mathematically verified:

- ``Base Cost``  = Supplier Price + Dropshipping Fee + Pick & Pack + Shipping
- ``Final Price`` = Base Cost / ((1 - commission_rate) * (1 - margin_percent))

The break-even price (the price *before* marketplace commission is applied)
is also returned for reporting purposes.
"""

from __future__ import annotations

from typing import Dict, Any


class PricingEngine:
    """Calculate retail pricing with margin and commission safety.

    Attributes
    ----------
    dropshipping_fee : float
        Fixed fee added on top of the supplier price.
    pick_and_pack_fee : float
        Per-order picking and packing cost.
    shipping_cost : float
        Fixed shipping charge applied to every order.
    margin_percent : float
        Desired profit margin (e.g. ``0.25`` = 25%).
    marketplace_commission_rate : float
        Percentage taken by the marketplace from the *final* selling price
        (e.g. ``0.15`` = 15%).
    """

    def __init__(
        self,
        dropshipping_fee: float = 2.50,
        pick_and_pack_fee: float = 1.50,
        shipping_cost: float = 4.95,
        margin_percent: float = 0.25,
        marketplace_commission_rate: float = 0.15,
    ) -> None:
        """Initialise the pricing engine with custom or default rates.

        :param dropshipping_fee: Fixed dropshipping surcharge.
        :param pick_and_pack_fee: Picking and packing labour cost.
        :param shipping_cost: Fixed shipping charge.
        :param margin_percent: Target net margin after commission (0.0-
            1.0).  Default 25 %.
        :param marketplace_commission_rate: Marketplace commission as a
            fraction of the final price (0.0 - 1.0).  Default 15 %.
        """
        self.dropshipping_fee: float = dropshipping_fee
        self.pick_and_pack_fee: float = pick_and_pack_fee
        self.shipping_cost: float = shipping_cost
        self.margin_percent: float = margin_percent
        self.marketplace_commission_rate: float = marketplace_commission_rate

    def calculate_price(self, supplier_price: float) -> Dict[str, Any]:
        """Compute the full cost and final selling price for a product.

        The calculation follows the standard dropship pricing formula:

        1. ``Base Cost`` = supplier_price + dropshipping_fee + pick_and_pack_fee
           + shipping_cost
        2. ``Final Price`` = Base Cost / ((1 - commission) * (1 - margin))
           This guarantees that ::

               final_price * (1 - commission) * (1 - margin) == base_cost

        3. ``Break-even`` = Base Cost / (1 - commission)
            – the price needed to *just* cover the costs, before margin.

        :param supplier_price: The amount paid to the supplier for one unit
            (>= 0).
        :return: Dict with the following keys::

            - ``supplier_price``: The original supplier cost, rounded to 2
              decimal places.
            - ``base_cost``: Total cost base (supplier + all fees + shipping),
              rounded to 2 decimal places.
            - ``final_price``: The recommended retail price, rounded to 2
              decimal places, at which the desired margin is achieved after
              the marketplace commission is deducted.
            - ``break_even``: The minimum price to cover base cost before
              margin is applied, rounded to 2 decimal places.

        :raises ValueError: If ``supplier_price`` is negative, or if the
            combined margin + commission is >= 1 (which would cause a
            division-by-zero or negative final price).
        """
        if supplier_price < 0:
            msg = f"supplier_price must be non-negative, got {supplier_price}"
            raise ValueError(msg)

        denominator: float = (1 - self.marketplace_commission_rate) * (
            1 - self.margin_percent
        )

        if denominator <= 0:
            msg = (
                f"Invalid pricing configuration: "
                f"marketplace_commission_rate={self.marketplace_commission_rate} "
                f"+ margin_percent={self.margin_percent} >= 1.0, "
                "would result in non-positive final price."
            )
            raise ValueError(msg)

        base_cost: float = (
            supplier_price
            + self.dropshipping_fee
            + self.pick_and_pack_fee
            + self.shipping_cost
        )

        final_price: float = base_cost / denominator

        return {
            "supplier_price": round(supplier_price, 2),
            "base_cost": round(base_cost, 2),
            "final_price": round(final_price, 2),
            "break_even": round(base_cost / (1 - self.marketplace_commission_rate), 2),
        }