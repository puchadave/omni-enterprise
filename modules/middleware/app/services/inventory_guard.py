"""Inventory guard: safety stock buffer and atomic bundle stock enforcement."""

from __future__ import annotations

import logging
from typing import Dict, List, Any

logger = logging.getLogger(__name__)


class InventoryGuard:
    """Enforces -10% safety stock buffer and Atomic Bundle Guard rule."""

    def __init__(self, safety_stock_buffer: float = 0.10) -> None:
        self.safety_stock_buffer = safety_stock_buffer

    def apply_safety_buffer(self, supplier_qty: int) -> int:
        """Subtract safety buffer to prevent overselling."""
        buffered_qty = int(supplier_qty * (1 - self.safety_stock_buffer))
        return max(0, buffered_qty)

    def validate_bundle_stock(self, bundle_sku: str, component_skus: List[str], product_stocks: Dict[str, int]) -> Dict[str, Any]:
        """Bundle max stock = min(component stocks). Pause if any component is 0."""
        if not component_skus:
            return {"valid_stock": 0, "is_active": False}
        component_stocks = []
        for sku in component_skus:
            stock = product_stocks.get(sku, 0)
            component_stocks.append(stock)
            if stock <= 0:
                logger.warning("Component %s for bundle %s is out of stock. Pausing bundle.", sku, bundle_sku)
                return {"valid_stock": 0, "is_active": False}
        max_bundle_stock = min(component_stocks)
        return {"valid_stock": max_bundle_stock, "is_active": True}
