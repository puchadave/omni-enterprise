"""Matterhorn B2B REST API Client.

Client for the Matterhorn Wholesale B2B API (Base URL: https://matterhorn-moda.de/B2BAPI/).
Implements authentication, pagination, retry logic, rate limit handling,
and all required endpoints per the official API specification.

API Key authentication: Authorization: Bearer <API_KEY>
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import httpx

from .matterhorn_connection_manager import (
    MatterhornConnectionManager,
    MatterhornAPIError,
    MatterhornAuthenticationError,
    MatterhornNotFoundError,
    MatterhornRateLimitError,
    MAX_PAGE_SIZE,
    DEFAULT_BASE_URL,
)

logger = logging.getLogger(__name__)

# ── API Endpoints ─────────────────────────────────────────────────────────

# Base paths relative to Matterhorn B2BAPI base URL
API_PATHS: dict[str, str] = {
    "items": "/ITEMS/",
    "item": "/ITEMS/{id}",
    "brands": "/DICTIONARIES/BRANDS",
    "categories": "/DICTIONARIES/CATEGORIES",
    "delivery": "/DICTIONARIES/DELIVERY",
    "delivery_country": "/DICTIONARIES/DELIVERY/{country}",
    "orders": "/ACCOUNT/ORDERS/",
    "order": "/ACCOUNT/ORDERS/{id}",
}

# ── Pagination helpers ────────────────────────────────────────────────────

def _build_pagination_params(
    page: int = 1,
    limit: int | None = None,
    max_limit: int = MAX_PAGE_SIZE,
) -> dict[str, int]:
    """Build pagination query parameters for API requests."""
    limit = limit or DEFAULT_PAGE_SIZE
    limit = min(limit, max_limit)
    return {"page": page, "limit": limit}


# ── Product API ───────────────────────────────────────────────────────────

class ProductAPI:
    """Handler for Matterhorn product API endpoints."""

    def __init__(self, connection: MatterhornConnectionManager) -> None:
        self.connection = connection

    async def list_items(
        self,
        page: int = 1,
        limit: int | None = None,
        brand_id: int | None = None,
        category_id: int | None = None,
        new_collection: bool | None = None,
        last_update: str | None = None,
    ) -> dict[str, Any]:
        """List products with filtering and pagination.

        Supported filters per Matterhorn spec:
        - page: Page number (1-based)
        - limit: Items per page (max 1000)
        - brand_id: Filter by brand ID
        - category_id: Filter by category ID
        - new_collection: Boolean filter for new collection items
        - last_update: ISO datetime filter, only items updated after this time
        """
        params: dict[str, Any] = _build_pagination_params(page, limit)

        if brand_id is not None:
            params["brand_id"] = brand_id
        if category_id is not None:
            params["category_id"] = category_id
        if new_collection is not None:
            params["new_collection"] = "1" if new_collection else "0"
        if last_update is not None:
            # Ensure proper ISO 8601 format
            if isinstance(last_update, datetime):
                last_update = last_update.astimezone(timezone.utc).isoformat()
            params["last_update"] = last_update

        response = await self.connection._request(
            "GET", API_PATHS["items"], params=params
        )
        return response.json()

    async def retrieve_item(self, product_id: int | str) -> dict[str, Any]:
        """Retrieve a single product by its Matterhorn ID."""
        response = await self.connection._request(
            "GET", API_PATHS["item"].format(id=product_id)
        )
        return response.json()


# ── Dictionary API ────────────────────────────────────────────────────────

class DictionaryAPI:
    """Handler for Matterhorn dictionary/brand/category endpoints."""

    def __init__(self, connection: MatterhornConnectionManager) -> None:
        self.connection = connection

    async def list_brands(self) -> list[dict[str, Any]]:
        """Retrieve all brands from the Matterhorn dictionary."""
        response = await self.connection._request("GET", API_PATHS["brands"])
        return response.json()

    async def list_categories(self) -> list[dict[str, Any]]:
        """Retrieve all categories from the Matterhorn dictionary."""
        response = await self.connection._request("GET", API_PATHS["categories"])
        return response.json()

    async def list_delivery(
        self, country: str | None = None
    ) -> list[dict[str, Any]]:
        """Retrieve delivery/duty information.

        Args:
            country: ISO country code (e.g. "DE", "AT", "CH") or None for all
        """
        params: dict[str, Any] = {}
        if country:
            params["country"] = country
        response = await self.connection._request(
            "GET", API_PATHS["delivery"], params=params
        )
        return response.json()

    async def get_delivery_country(self, country: str) -> dict[str, Any]:
        """Retrieve delivery information for a specific country."""
        response = await self.connection._request(
            "GET", API_PATHS["delivery_country"].format(country=country)
        )
        return response.json()


# ── Order API ─────────────────────────────────────────────────────────────

class OrderAPI:
    """Handler for Matterhorn order API endpoints."""

    def __init__(self, connection: MatterhornConnectionManager) -> None:
        self.connection = connection

    async def create_order(
        self,
        items: list[dict[str, Any]],
        delivery_to: dict[str, str],
        currency: str = "EUR",
        delivery_method_id: int | None = None,
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
            items: List of order items with 'variant_uid' and 'quantity'
            delivery_to: Delivery address with all required fields
            currency: Currency code (default: EUR)
            delivery_method_id: Optional delivery method ID from dictionary

        Returns:
            Order creation response from Matterhorn API
        """
        # Validate required delivery fields
        required_fields: list[str] = [
            "first_name",
            "second_name",
            "country",
            "street",
            "house_number",
            "zip",
            "city",
        ]
        missing: list[str] = [
            f for f in required_fields if f not in delivery_to or not delivery_to[f]
        ]
        if missing:
            raise ValueError(f"Missing required delivery fields: {missing}")

        payload: dict[str, Any] = {
            "items": items,
            "delivery_to": delivery_to,
            "currency": currency,
        }
        if delivery_method_id is not None:
            payload["delivery_method_id"] = delivery_method_id

        response = await self.connection._request(
            "PUT", API_PATHS["orders"], json_body=payload
        )
        return response.json()

    async def retrieve_order(self, order_id: str) -> dict[str, Any]:
        """Retrieve an order by its Matterhorn order ID."""
        response = await self.connection._request(
            "GET", API_PATHS["order"].format(id=order_id)
        )
        return response.json()


# ── Full API Client ───────────────────────────────────────────────────────

class MatterhornAPIClient:
    """Full-featured client for the Matterhorn B2B API.

    Provides access to all API domains through dedicated handlers.
    """

    def __init__(self, connection: MatterhornConnectionManager) -> None:
        self.connection = connection
        self.products = ProductAPI(connection)
        self.dictionaries = DictionaryAPI(connection)
        self.orders = OrderAPI(connection)

    # ── Convenience factory ──────────────────────────────────────────────

    @staticmethod
    async def create(api_key: str | None = None, base_url: str = DEFAULT_BASE_URL) -> MatterhornAPIClient:
        """Create a fully configured MatterhornAPIClient."""
        connection = MatterhornConnectionManager(api_key=api_key, base_url=base_url)
        await connection.__aenter__()
        return MatterhornAPIClient(connection)

    async def close(self) -> None:
        await self.connection.close()


# ── Factory function ──────────────────────────────────────────────────────

async def create_matterhorn_client(
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> MatterhornAPIClient:
    """Factory function to create a complete Matterhorn API client."""
    return await MatterhornAPIClient.create(api_key, base_url)