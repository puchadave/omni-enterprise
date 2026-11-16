"""Payment state synchronization worker for Omni-Core Middleware.

Verifies periodical the payment status at Payment Gateways and updates
the database accordingly. Follows the established pattern of the
``background_worker`` and the ``start_trend_scheduler``.

Design principles:
- Idempotent: repeated execution does not unexpectedly change state.
- Error-resilient: network errors are logged and do not crash the worker.
- Non-blocking: uses asyncio.sleep for interval control.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import aiosqlite

from database import get_connection, close_connection, DB_PATH

logger = logging.getLogger(__name__)

# Interval for periodic check (in seconds)
PAYMENT_SYNC_INTERVAL: int = 30  # every 30 seconds

# Mapping of gateway response codes to internal payment_status values
GATEWAY_PAYMENT_MAP: dict[str, str] = {
    "paid": "paid",
    "success": "paid",
    "completed": "paid",
    "failed": "failed",
    "error": "failed",
    "cancelled": "refunded",
}


class PaymentStateSync:
    """Synchronizes the payment status of all orders with the gateway."""

    def __init__(self, interval_seconds: int = PAYMENT_SYNC_INTERVAL) -> None:
        self.interval_seconds: int = interval_seconds
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start background task (via FastAPI lifespan)."""
        self._task = asyncio.create_task(self._run_loop())
        logger.info("PaymentStateSync worker started (interval=%ss)", self.interval_seconds)

    async def stop(self) -> None:
        """Graceful shutdown of background task."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            logger.info("PaymentStateSync worker stopped")

    async def _run_loop(self) -> None:
        """Main loop: sleep in interval and run sync."""
        try:
            while True:
                await asyncio.sleep(self.interval_seconds)
                await self._sync_all_orders()
        except asyncio.CancelledError:
            logger.info("PaymentStateSync loop cancelled")
            raise
        except Exception as exc:  # pragma: no cover
            logger.exception(f"PaymentStateSync unexpected error: {exc}")

    async def _sync_all_orders(self) -> None:
        """Fetch all orders with payment_status != 'paid' and check them."""
        async with get_connection() as db:
            async with db.execute(
                "SELECT order_id, sku FROM orders WHERE payment_status != 'paid'"
            ) as cursor:
                orders = await cursor.fetchall()

        for order_id, sku in orders:
            await self._sync_order_payment(order_id, sku)

    async def _sync_order_payment(self, order_id: str, sku: str) -> None:
        """Check the payment status of a single order via the gateway.

        Idempotent: if the order already has ``payment_status = 'paid'``,
        nothing is changed.
        """
        async with get_connection() as db:
            async with db.execute(
                "SELECT payment_status, fulfillment_locked FROM orders WHERE order_id = ?",
                (order_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            logger.warning(f"Order {order_id} not found during payment sync")
            return

        current_status, locked = row

        if current_status == "paid":
            return  # Already paid, nothing to do

        # --- Placeholder: Real gateway API call ---
        # Replace this block with the actual API call to your Payment Provider.
        # Example:
        # async with httpx.AsyncClient(timeout=10.0) as client:
        #     response = await client.get(f"{GATEWAY_BASE}/orders/{order_id}/payment")
        #     gateway_data = response.json()
        #     gateway_status = gateway_data.get("status", "pending")
        #
        # mapped = GATEWAY_PAYMENT_MAP.get(gateway_status)
        # if mapped is None:
        #     logger.error(f"Unknown gateway status '{gateway_status}' for order {order_id}")
        #     return
        #
        # await _update_order_payment_status_db(db, order_id, mapped)
        # logger.info(f"Payment sync order {order_id}: {current_status} -> {mapped}")
        # return

        # --- Simulation: Every 7th order is marked as 'paid' ---
        h = int(hashlib.md5(f"{order_id}{sku}".encode()).hexdigest()[:8], 16)
        if h % 7 == 0:
            new_status = "paid"
        else:
            new_status = "pending"

        async with get_connection() as db:
            await db.execute(
                "UPDATE orders SET payment_status = ?, fulfillment_locked = ?, updated_at = (STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW')) WHERE order_id = ?",
                (new_status, new_status == "paid", order_id),
            )
            await db.commit()

        if new_status != current_status:
            logger.info(f"Payment sync order {order_id}: {current_status} -> {new_status}")
        else:
            logger.debug(f"Payment sync order {order_id} unchanged: {current_status}")

    async def _update_order_payment_status_db(
        self, db: aiosqlite.Connection, order_id: str, new_status: str
    ) -> None:
        """Update the payment status in the database atomically."""
        await db.execute(
            "UPDATE orders SET payment_status = ?, fulfillment_locked = ?, updated_at = (STRFTIME('%Y-%m-%d %H:%M:%f', 'NOW')) WHERE order_id = ?",
            (new_status, new_status == "paid", order_id),
        )
        await db.commit()


# Singleton instance for the application
payment_sync: PaymentStateSync = PaymentStateSync()


async def start_payment_sync_worker() -> None:
    """Start the payment sync worker (call in FastAPI lifespan)."""
    await payment_sync.start()


async def stop_payment_sync_worker() -> None:
    """Stop the payment sync worker (call in FastAPI lifespan)."""
    await payment_sync.stop()