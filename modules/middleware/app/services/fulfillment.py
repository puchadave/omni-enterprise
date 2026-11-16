"""Order fulfillment service.

The service reserves stock before outbound dispatch. If a supplier API endpoint
is not configured, the order remains in ``reserved`` state rather than inventing
a tracking number or claiming a shipment was sent.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

import httpx

from database import get_setting, update_order_status

logger = logging.getLogger(__name__)


class FulfillmentService:
    """Dispatch already-reserved orders to a configured supplier endpoint."""

    def __init__(self, default_base_url: str = "https://matterhorn-moda.de") -> None:
        self.default_base_url = default_base_url.rstrip("/")

    async def dispatch_reserved_order(self, order: Mapping[str, Any]) -> dict[str, str]:
        """Dispatch a reserved order and persist supplier-confirmed tracking data.

        Without a configured token, no network call is attempted and the order is
        left in ``reserved`` state for operator action. This is safer than a fake
        fulfillment acknowledgement.
        """
        order_id = str(order["order_id"])
        token = await get_setting("matterhorn_api_token")
        base_url = (await get_setting("matterhorn_order_api_url") or self.default_base_url).rstrip("/")
        if not token:
            message = "Matterhorn API token is not configured; order remains reserved."
            await update_order_status(order_id, "reserved", error=message)
            return {"order_id": order_id, "status": "reserved", "message": message}

        payload = {
            "order_id": order_id,
            "sku": str(order["sku"]),
            "quantity": int(order["quantity"]),
            "marketplace": str(order["marketplace"]),
            "customer_reference": str(order.get("customer_reference", "")),
        }
        await update_order_status(order_id, "dispatching")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
                response = await client.post(
                    f"{base_url}/api/v1/orders",
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                response_data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Supplier dispatch failed for %s: %s", order_id, exc)
            await update_order_status(order_id, "failed", error="Supplier dispatch request failed.")
            return {"order_id": order_id, "status": "failed", "message": "Supplier dispatch request failed."}

        tracking_number = str(response_data.get("tracking_number", ""))
        tracking_url = str(response_data.get("tracking_url", ""))
        await update_order_status(
            order_id,
            "dispatched",
            tracking_number=tracking_number,
            tracking_url=tracking_url,
        )
        logger.info("Order %s dispatched successfully.", order_id)
        return {
            "order_id": order_id,
            "status": "dispatched",
            "message": "Supplier dispatch confirmed.",
        }
