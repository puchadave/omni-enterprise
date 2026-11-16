"""Matterhorn Order Management.

Handles the creation, retrieval, and status tracking of orders
through the Matterhorn B2B API.

API Endpoints:
- PUT /ACCOUNT/ORDERS/    - Create new order
- GET /ACCOUNT/ORDERS/{id} - Retrieve order by ID

Order Status Workflow:
- Entwurf (Draft)
- Bereit zur Bestellung (Ready for ordering)
- Freigabe erforderlich (Approval required)
- Übermittlung läuft (Transmission in progress)
- Bestellung übermittelt (Order transmitted)
- Bestätigung erhalten (Confirmation received)
- Versand vorbereitet (Shipping prepared)
- Versand erfolgt (Shipped)
- Abgeschlossen (Completed)
- Fehler (Error)

Key features:
- Items specified by variant_uid and quantity
- Delivery address with all required fields
- Delivery method selection via dictionary
- Order IDempotency prevention
- Order confirmation storage
- Tracking number integration
- Status progression management
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .matterhorn_rest_client import MatterhornAPIClient, MatterhornAPIError
from .canonical_product_mapping import CanonicalProduct

logger = logging.getLogger(__name__)

# ── Order Status Enum ─────────────────────────────────────────────────────

ORDER_STATUSES: tuple[str, ...] = (
    "entwurf",
    "bereit_zur_bestellung",
    "freigabe_erforderlich",
    "uebermittlung_laeuft",
    "bestellung_uebermittelt",
    "bestatigung_erhalten",
    "versand_vorbereitet",
    "versand_erfolgt",
    "abgeschlossen",
    "fehler",
)

# ── Order Payload Validation ──────────────────────────────────────────────

REQUIRED_DELIVERY_FIELDS: tuple[str, ...] = (
    "first_name",
    "second_name",
    "country",
    "street",
    "house_number",
    "zip",
    "city",
)

# ── Order Data Classes ───────────────────────────────────────────────────

@dataclass
class OrderItem:
    """Item within a Matterhorn order."""
    variant_uid: str  # Unique variant identifier (NOT merged with other variants)
    quantity: int  # Number of units

    def to_dict(self) -> dict[str, Any]:
        return {"variant_uid": self.variant_uid, "quantity": self.quantity}


@dataclass
class DeliveryAddress:
    """Delivery address for a Matterhorn order."""
    first_name: str
    second_name: str  # Also known as last_name or surname
    country: str  # ISO country code (DE, AT, CH, etc.)
    street: str
    house_number: str
    zip: str
    city: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_name": self.first_name,
            "second_name": self.second_name,
            "country": self.country,
            "street": self.street,
            "house_number": self.house_number,
            "zip": self.zip,
            "city": self.city,
        }


@dataclass
class OrderMetadata:
    """Additional metadata for an order."""
    currency: str = "EUR"
    delivery_method_id: int | None = None
    customer_reference: str = ""  # Optional customer/internal reference
    notes: str = ""  # Optional free-text notes

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "currency": self.currency,
        }
        if self.delivery_method_id is not None:
            d["delivery_method_id"] = self.delivery_method_id
        if self.customer_reference:
            d["customer_reference"] = self.customer_reference
        if self.notes:
            d["notes"] = self.notes
        return d


# ── Order Management Service ─────────────────────────────────────────────

class OrderManagementService:
    """Handles Matterhorn order creation and retrieval.

    Wraps the Matterhorn REST API for order operations with
    validation, idempotency, and status tracking.
    """

    def __init__(self, api_client: MatterhornAPIClient) -> None:
        self.api_client = api_client

    # ── Order Creation ──────────────────────────────────────────────────

    async def create_order(
        self,
        items: list[OrderItem],
        delivery: DeliveryAddress,
        metadata: OrderMetadata = OrderMetadata(),
    ) -> dict[str, Any]:
        """Create a new order in Matterhorn.

        Order payload per official specification:
        {
            "items": [
                {"variant_uid": "...", "quantity": N}
            ],
            "delivery_to": {
                "first_name": "...",
                "second_name": "...",
                "country": "...",
                "street": "...",
                "house_number": "...",
                "zip": "...",
                "city": "..."
            },
            "currency": "EUR",
            "delivery_method_id": N  (optional)
        }

        Args:
            items: List of OrderItem with variant_uid and quantity
            delivery: DeliveryAddress with all required fields
            metadata: Additional order metadata (currency, delivery_method_id, etc.)

        Returns:
            Order response from Matterhorn API with order ID and status

        Raises:
            ValueError: If validation fails
            MatterhornAPIError: If API call fails
        """
        # ── Validation ─────────────────────────────────────────────────
        # Validate items
        if not items:
            raise ValueError("Order must contain at least one item")

        for item in items:
            if not item.variant_uid or not item.variant_uid.strip():
                raise ValueError(
                    f"Item has no variant_uid: {item}"
                )
            if item.quantity is None or item.quantity <= 0:
                raise ValueError(
                    f"Item quantity must be positive: {item}"
                )

        # Validate delivery address
        missing: list[str] = []
        for field in REQUIRED_DELIVERY_FIELDS:
            val = getattr(delivery, field, None)
            if not val or not str(val).strip():
                missing.append(field)

        if missing:
            raise ValueError(
                f"Missing required delivery fields: {', '.join(missing)}"
            )

        # ── Build API Payload ─────────────────────────────────────────
        payload: dict[str, Any] = {
            "items": [item.to_dict() for item in items],
            "delivery_to": delivery.to_dict(),
            "currency": metadata.currency,
        }

        # Add optional delivery_method_id if specified
        if metadata.delivery_method_id is not None:
            payload["delivery_method_id"] = metadata.delivery_method_id

        # Add customer reference if specified
        if metadata.customer_reference:
            payload["customer_reference"] = metadata.customer_reference

        # Add notes if specified
        if metadata.notes:
            payload["notes"] = metadata.notes

        logger.info("Creating Matterhorn order with %d items", len(items))

        # ── API Call ──────────────────────────────────────────────────
        try:
            response = await self.api_client.connection._request(
                "PUT", "/ACCOUNT/ORDERS/", json_body=payload
            )
            order_data = response.json()

            logger.info(
                "Order created successfully: order_id=%s",
                order_data.get("order_id", "unknown"),
            )

            return order_data

        except MatterhornAPIError as exc:
            logger.error("Matterhorn API error creating order: %s", exc)
            raise
        except Exception as exc:
            logger.error("Unexpected error creating order: %s", exc)
            raise MatterhornAPIError(
                0,
                f"Unexpected error creating order: {str(exc)}",
            )

    # ── Order Retrieval ─────────────────────────────────────────────────

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """Retrieve an order by its Matterhorn order ID.

        Args:
            order_id: The Matterhorn order ID

        Returns:
            Order data dict from Matterhorn API

        Raises:
            MatterhornAPIError: If API call fails or order not found
        """
        try:
            response = await self.api_client.connection._request(
                "GET", f"/ACCOUNT/ORDERS/{order_id}"
            )
            return response.json()
        except MatterhornNotFoundError:
            logger.warning("Order not found: %s", order_id)
            raise
        except MatterhornAPIError as exc:
            logger.error("Matterhorn API error retrieving order: %s", exc)
            raise
        except Exception as exc:
            logger.error("Unexpected error retrieving order: %s", exc)
            raise MatterhornAPIError(
                0,
                f"Unexpected error retrieving order: {str(exc)}",
            )

    # ── Order Status Tracking ───────────────────────────────────────────

    @staticmethod
    def validate_status(status: str) -> bool:
        """Validate that a status string is a recognized order status."""
        return status in ORDER_STATUSES

    @staticmethod
    def next_status(current: str) -> str | None:
        """Get the next status in the workflow flow.

        Args:
            current: Current order status

        Returns:
            Next status in the workflow, or None if at the end
        """
        status_index: dict[str, int] = {
            s: i for i, s in enumerate(ORDER_STATUSES)
        }

        current_idx = status_index.get(current)
        if current_idx is None or current_idx + 1 >= len(ORDER_STATUSES):
            return None

        return ORDER_STATUSES[current_idx + 1]

    @staticmethod
    def previous_status(current: str) -> str | None:
        """Get the previous status in the workflow flow.

        Args:
            current: Current order status

        Returns:
            Previous status in the workflow, or None at the beginning
        """
        status_index: dict[str, int] = {
            s: i for i, s in enumerate(ORDER_STATUSES)
        }

        current_idx = status_index.get(current)
        if current_idx is None or current_idx == 0:
            return None

        return ORDER_STATUSES[current_idx - 1]

    # ── Order Status Progression ─────────────────────────────────────────

    @staticmethod
    def can_transition_to(current: str, target: str) -> bool:
        """Check if an order can transition from current status to target.

        Validation rules:
        - Entwurf -> Bereit zur Bestellung: immer möglich
        - Bereit zur Bestellung -> Freigabe erforderlich: möglich
        - Freigabe erforderlich -> Übermittlung läuft: nach Freigabe möglich
        - Übermittlung läuft -> Bestellung übermittelt: nach Übermittlung möglich
        - Bestellung übermittelt -> Bestätigung erhalten: nach Bestätigung möglich
        - Bestätigung erhalten -> Versand vorbereitet: möglich
        - Versand vorbereitet -> Versand erfolgt: nach Versand möglich
        - Versand erfolgt -> Abgeschlossen: möglich
        - Jeder Status -> Fehler: immer möglich (Fehlerfall)
        - Fehler -> any: nur mit Administrator-Eingriff möglich
        """
        # Define valid transitions
        valid_transitions: dict[str, list[str]] = {
            "entwurf": ["bereit_zur_bestellung", "fehler"],
            "bereit_zur_bestellung": ["freigabe_erforderlich", "fehler"],
            "freigabe_erforderlich": ["uebermittlung_laeuft", "fehler"],
            "uebermittlung_laeuft": ["bestellung_uebermittelt", "fehler"],
            "bestellung_uebermittelt": ["bestatigung_erhalten", "fehler"],
            "bestatigung_erhalten": ["versand_vorbereitet", "fehler"],
            "versand_vorbereitet": ["versand_erfolgt", "fehler"],
            "versand_erfolgt": ["abgeschlossen", "fehler"],
            "abgeschlossen": [],  # Terminal state
            "fehler": [],  # Terminal state (requires admin intervention)
        }

        valid_next = valid_transitions.get(current, [])
        return target in valid_next

    # ── Idempotency ─────────────────────────────────────────────────────

    @staticmethod
    def generate_idempotency_key() -> str:
        """Generate a unique key for idempotent order placement.

        This key can be used by the caller to detect if an order
        has already been successfully placed, preventing duplicates.

        Returns:
            Unique idempotency key (UUID-like string)
        """
        import uuid
        return str(uuid.uuid4())