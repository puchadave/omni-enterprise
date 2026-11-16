"""Smart pricing engine for Omni-Core Middleware."""

from __future__ import annotations

import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)


class PricingEngine:
    """Calculates final selling price from supplier cost + all fees + margin."""

    def __init__(
        self,
        dropshipping_fee: float = 2.50,
        pick_and_pack_fee: float = 1.50,
        shipping_cost: float = 4.95,
        margin_percent: float = 0.25,
        marketplace_commission_rate: float = 0.15,
    ) -> None:
        self.dropshipping_fee = dropshipping_fee
        self.pick_and_pack_fee = pick_and_pack_fee
        self.shipping_cost = shipping_cost
        self.margin_percent = margin_percent
        self.marketplace_commission_rate = marketplace_commission_rate

    def calculate_price(self, supplier_price: float) -> Dict[str, Any]:
        """Calculate base cost and final selling price."""
        base_cost = supplier_price + self.dropshipping_fee + self.pick_and_pack_fee + self.shipping_cost
        denominator = (1 - self.marketplace_commission_rate) * (1 - self.margin_percent)
        if denominator <= 0:
            raise ValueError("Invalid margin or commission settings.")
        final_price = base_cost / denominator
        return {
            "supplier_price": round(supplier_price, 2),
            "base_cost": round(base_cost, 2),
            "final_price": round(final_price, 2),
            "break_even": round(base_cost / (1 - self.marketplace_commission_rate), 2),
        }
