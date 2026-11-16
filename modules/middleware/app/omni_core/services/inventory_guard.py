"""Atomic inventory guard for bundle safety and overselling prevention.

Exports two core abstractions:

1. ``InventoryGuard``  - applies a safety buffer to supplier stock counts.
2. Bundle guard logic that enforces the *Atomic Bundle Rule*: a bundle's
   maximum stock is limited by its scarcest component.

All math is deterministic and side-effect free; suitable for use in an
async event-loop context without race conditions so long as the caller
guards database mutations with proper locking or serialization.
"""

from __future__ import annotations

from typing import List, Dict, Any

import logging


logger = logging.getLogger(__name__)


class InventoryGuard:
    """Applies a safety-stock buffer to prevent overselling.

    The safety buffer is a percentage subtracted from the supplier's quoted
    quantity.  For example, a buffer of ``0.10`` (10%) means that if the
    supplier reports 100 units, the guard will only allow 90 to be sold.

    Safety stock calculations are *idempotent* and *deterministic*: given the
    same input they always produce the same output, which guarantees
    consistency in concurrent request handling as long as database writes
    are serialised.
    """

    def __init__(self, safety_stock_buffer: float = 0.10) -> None:
        """Initialise the guard.

        :param safety_stock_buffer: Fraction of stock to subtract (e.g.
            ``0.10`` = 10% buffer).  Must be in ``[0, 1) ``.
        """
        if not 0.0 <= safety_stock_buffer < 1.0:
            raise ValueError(
                f"safety_stock_buffer must be in [0, 1), got {safety_stock_buffer}"
            )
        self.safety_stock_buffer: float = safety_stock_buffer

    def apply_safety_buffer(self, supplier_qty: int) -> int:
        """Calculate the buffered quantity to prevent overselling.

        The formula subtracts the buffer percentage from the supplier's
        quoted quantity and floors the result.

        :param supplier_qty: Raw stock count from the supplier (>= 0).
        :return: Buffered quantity that may safely be sold.
        """
        if supplier_qty < 0:
            logger.warning(f"Negative supplier quantity received: {supplier_qty}")
            return 0
        buffered_qty: int = int(supplier_qty * (1 - self.safety_stock_buffer))
        return max(0, buffered_qty)

    def validate_bundle_stock(
        self,
        bundle_sku: str,
        component_skus: List[str],
        product_stocks: Dict[str, int],
    ) -> Dict[str, Any]:
        """Enforce the Atomic Bundle Guard rule.

        The bundle's sellable stock is limited by the *minimum* component
        stock.  If *any* component is at or below zero, the bundle is
        considered inactive and zero stock is returned.

        This is a pure function – it does not mutate any external state.
        The caller must ensure ``product_stocks`` reflects the latest DB
        state and perform the actual DB update atomically.

        :param bundle_sku: The SKU of the bundle being validated.
        :param component_skus: List of component SKU strings that compose
            the bundle.
        :param product_stocks: Mapping ``sku`` --> current integer stock
            from the products table.
        :return: Dict with two keys::

            - ``valid_stock``: ``int`` – the maximum bundle stock
              (``min`` of component stocks, or ``0`` if any are depleted).
            - ``is_active``: ``bool`` – ``True`` iff every component has
              stock > 0.

        :raises ValueError: If ``component_skus`` is empty.
        """
        if not component_skus:
            msg = "validate_bundle_stock called with empty component_skus list"
            logger.error(msg)
            raise ValueError(msg)

        # Collect component stocks; default to 0 if SKU not in dict
        component_stocks: List[int] = []
        for sku in component_skus:
            stock: int = product_stocks.get(sku, 0)
            component_stocks.append(stock)

            # Early exit: if any component is out of stock, bundle is inactive
            if stock <= 0:
                logger.warning(
                    f"Component {sku} for bundle {bundle_sku} is out of stock. "
                    "Pausing bundle."
                )
                return {"valid_stock": 0, "is_active": False}

        # The bundle's stock is limited by its scarcest component (atomic rule)
        max_bundle_stock: int = min(component_stocks)  # type: ignore[arg-type]

        return {
            "valid_stock": max_bundle_stock,
            "is_active": True,
        }