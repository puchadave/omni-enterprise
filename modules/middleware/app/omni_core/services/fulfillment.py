"""Fulfillment service for Omni-Core Middleware.

Processes incoming orders through three stages:

1. **Inventory validation** - checks safety stock and bundle rules.
2. **Supplier dispatch** - sends the order to the supplier via Matterhorn
   (or whichever dispatch endpoint is configured).
3. **Tracking return** - returns the tracking number and final status.

All I/O is asynchronous; the service is designed to be instantiated once
and called from FastAPI route handlers or background workers.
"""

from __future__ import annotations

import logging
import httpx
from typing import Dict, Any, Optional

from omni_core.services.inventory_guard import InventoryGuard

logger = logging.getLogger(__name__)


class FulfillmentService:
    """Orchestrates order fulfillment: validate → dispatch → track."""

    def __init__(
        self,
        matterhorn_base_url: str = "https://matterhorn.example.com",
        safety_stock_buffer: float = 0.10,
    ) -> None:
        """Initialise the fulfillment service.

        :param matterhorn_base_url: Base URL of the Matterhorn dispatch
            service (e.g. ``https://matterhorn.example.com``).
        :param safety_stock_buffer: Passed to ``InventoryGuard``; default
            10% buffer.
        """
        self.base_url: str = matterhorn_base_url.rstrip("/")
        self.inventory_guard: InventoryGuard = InventoryGuard(
            safety_stock_buffer=safety_stock_buffer
        )

    async def process_order(
        self,
        order: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Process an inbound order through the full fulfillment pipeline.

        Pipeline stages:

        1. **Inventory check** - validates the order quantity against
           current stock using the ``InventoryGuard`` safety buffer.
           If the order cannot be fulfilled, an appropriate error is
           returned immediately.
        2. **Supplier dispatch** - POSTs the order to the Matterhorn
            dispatch endpoint as form-data (``application/x-www-form-urlencoded``).
            The Matterhorn service is expected to accept ``order_id``,
            ``sku``, ``quantity``, and ``customer_email`` fields and
            return a ``202 Accepted`` with a tracking number.
        3. **Tracking response** - assembles the final response dict
            with the tracking number and human-readable status message.

        The method is fully async and uses ``httpx.AsyncClient`` with a
        15-second timeout for the dispatch call.

        :param order: Dict containing at minimum the keys::

            - ``order_id``: Unique order identifier (str).
            - ``sku``: Product SKU (str).
            - ``quantity``: Number of units ordered (int).
            - ``customer_email``: Customer e-mail address (str).
            - ``marketplace``: Marketplace identifier (str), e.g.
              ``"shopware"``, ``"ebay"``.

        :return: Dict with the following keys::

            - ``order_id``: The original order ID.
            - ``status``: One of ``"dispatched"``, ``"inventory_error"``,
              ``"supplier_error"``.
            - ``tracking_number``: Optional tracking string returned by
              Matterhorn (``None`` if dispatch failed).
            - ``message``: Human-readable status message.

        :raises httpx.HTTPError: If the Matterhorn dispatch request
            returns a non-2xx status code (propagated as-is).
        """
        # ------------------------------------------------------------------
        # Stage 1: Inventory validation
        # ------------------------------------------------------------------
        order_id: str = order.get("order_id", "unknown")
        sku: str = order.get("sku", "")
        quantity: int = order.get("quantity", 1)
        marketplace: str = order.get("marketplace", "unknown")

        # Apply safety buffer to the available stock
        available_stock: int = await self._get_sku_stock(sku)
        buffered_stock: int = self.inventory_guard.apply_safety_buffer(
            available_stock
        )

        if quantity > buffered_stock:
            logger.warning(
                f"Order {order_id}: requested {quantity} units, "
                f"but only {buffered_stock} buffered available for SKU {sku}"
            )
            return {
                "order_id": order_id,
                "status": "inventory_error",
                "tracking_number": None,
                "message": (
                    f"Insufficient inventory for SKU {sku}. "
                    f"Requested {quantity}, available buffered {buffered_stock}."
                ),
            }

        # ------------------------------------------------------------------
        # Stage 2: Supplier dispatch
        # ------------------------------------------------------------------
        tracking_number: Optional[str] = None
        status: str = "dispatched"
        message: str = ""

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                dispatch_url = f"{self.base_url}/dispatch"
                form_data = {
                    "order_id": order_id,
                    "sku": sku,
                    "quantity": str(quantity),
                    "marketplace": marketplace,
                    "customer_email": order.get("customer_email", ""),
                }
                response = await client.post(
                    dispatch_url,
                    data=form_data,
                )
                response.raise_for_status()
                dispatch_data: Dict[str, Any] = response.json()
                tracking_number = dispatch_data.get("tracking_number")
                status = dispatch_data.get("status", "dispatched")
                message = dispatch_data.get(
                    "message",
                    f"Order dispatched to Matterhorn. Tracking: {tracking_number}",
                )
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"Fulfillment dispatch failed for {order_id}: "
                f"{exc.response.status_code} {exc.response.text}"
            )
            status = "supplier_error"
            message = (
                f"Supplier dispatch failed ({exc.response.status_code}): "
                f"{exc.response.text[:200]}"
            )
        except httpx.HTTPError as exc:
            logger.exception(f"Fulfillment dispatch network error for {order_id}: {exc}")
            status = "supplier_error"
            message = f"Supplier dispatch network error: {exc}"

        # ------------------------------------------------------------------
        # Stage 3: Return tracking response
        # ------------------------------------------------------------------
        return {
            "order_id": order_id,
            "status": status,
            "tracking_number": tracking_number,
            "message": message,
        }

    async def _get_sku_stock(self, sku: str) -> int:
        """Fetch current stock for a given SKU from the database.

        Uses the shared database layer when available (PostgreSQL/SQLite WAL),
        with a direct SQLite fallback for standalone use.

        :param sku: Product SKU to look up.
        :return: Integer stock count (0 if unknown/unavailable).
        """
        if not sku:
            return 0
        try:
            from database import fetch_product  # type: ignore

            item = await fetch_product(sku)
            if item:
                return int(item.get("stock") or 0)
        except Exception:
            logger.debug("Shared DB lookup failed for sku=%s; trying SQLite fallback", sku, exc_info=True)

        try:
            import os
            import sqlite3

            db_path = os.environ.get("DATABASE_PATH", "middleware.db")
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.execute("SELECT stock FROM products WHERE sku = ?", (sku,))
                row = cur.fetchone()
                return int(row[0]) if row and row[0] is not None else 0
            finally:
                conn.close()
        except Exception:
            logger.exception("Failed to fetch stock for sku=%s", sku)
            return 0