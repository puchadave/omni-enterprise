"""Matterhorn B2B API Connection Manager.

Central connection handler for the Matterhorn Wholesale B2B API.
Provides authenticated access, request logging, retry logic, and pagination.
All endpoints use Bearer token authentication.

Base URL: https://matterhorn-moda.de/B2BAPI/
Authentication: Authorization: Bearer <API_KEY>
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Defaults ────────────────────────────────────────────────────────────────

DEFAULT_BASE_URL: str = "https://matterhorn-moda.de/B2BAPI/"
DEFAULT_TIMEOUT: float = 30.0
DEFAULT_RETRY_COUNT: int = 3
DEFAULT_RETRY_DELAY: float = 1.0
DEFAULT_PAGE_SIZE: int = 100  # Maximum 1000 per phase spec, but default 100
MAX_PAGE_SIZE: int = 1_000


# ── Exceptions ──────────────────────────────────────────────────────────────

class MatterhornAPIError(RuntimeError):
    """Raised when the Matterhorn API returns a non-2xx status code."""
    def __init__(self, status_code: int, message: str, response_text: str | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.response_text = response_text
        super().__init__(f"Matterhorn API {status_code}: {message}")


class MatterhornAuthenticationError(MatterhornAPIError):
    """Raised when authentication fails (401/403)."""


class MatterhornNotFoundError(MatterhornAPIError):
    """Raised when a resource is not found (404)."""


class MatterhornRateLimitError(MatterhornAPIError):
    """Raised when rate limit is hit (429)."""


# ── Connection Manager ──────────────────────────────────────────────────────

class MatterhornConnectionManager:
    """Central connection manager for the Matterhorn B2B API.

    Handles:
    - Authenticated HTTP requests with Bearer token
    - Retry logic with exponential backoff
    - Pagination support
    - Rate limit handling
    - Request/response logging (sans secrets)
    - Connection health checking
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry_count: int = DEFAULT_RETRY_COUNT,
        retry_delay: float = DEFAULT_RETRY_DELAY,
    ) -> None:
        self.base_url: str = base_url.rstrip("/")
        self.api_key: str | None = api_key
        self.timeout: float = timeout
        self.retry_count: int = retry_count
        self.retry_delay: float = retry_delay
        self._client: httpx.AsyncClient | None = None
        self._last_health_check: float = 0
        self._request_count: int = 0
        self._error_count: int = 0
        self._success_count: int = 0
        self._api_version: str | None = None
        self._endpoint_counts: dict[str, int] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def _ensure_client(self) -> None:
        """Ensure the HTTP client is initialized."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            )

    async def __aenter__(self) -> MatterhornConnectionManager:
        await self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── Health ─────────────────────────────────────────────────────────────

    async def check_health(self) -> dict[str, Any]:
        """Check API availability and return basic info."""
        await self._ensure_client()
        start = time.monotonic()
        try:
            response = await self._client.get(f"{self.base_url}api-help", timeout=self.timeout)
            elapsed = time.monotonic() - start
            self._request_count += 1
            self._endpoint_counts["api-help"] = self._endpoint_counts.get("api-help", 0) + 1

            if response.status_code == 200:
                data = response.json()
                self._success_count += 1
                self._api_version = data.get("api_version", self._api_version or "unknown")
                return {
                    "status": "healthy",
                    "api_version": self._api_version,
                    "response_time_ms": int(elapsed * 1000),
                    "endpoints": data.get("endpoints", []),
                }
            elif response.status_code == 401:
                self._error_count += 1
                return {"status": "auth_error", "response_time_ms": int(elapsed * 1000)}
            else:
                self._error_count += 1
                return {
                    "status": f"http_{response.status_code}",
                    "response_time_ms": int(elapsed * 1000),
                }
        except httpx.TimeoutException as exc:
            self._error_count += 1
            return {"status": "timeout", "error": str(exc)}
        except httpx.ConnectError as exc:
            self._error_count += 1
            return {"status": "connection_error", "error": str(exc)}
        except Exception as exc:  # pylint: disable=broad-except
            self._error_count += 1
            return {"status": "error", "error": str(exc)}

    # ── Request helpers ────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute a single HTTP request with retry logic."""
        url = f"{self.base_url}{endpoint}"
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        last_exception: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                self._request_count += 1
                response = await self._client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )

                # Rate limit handling
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else self.retry_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
                    last_exception = MatterhornRateLimitError(
                        response.status_code, "Rate limit hit", response.text
                    )
                    continue

                # Retry on server errors (5xx)
                if 500 <= response.status_code < 600 and attempt < self.retry_count:
                    await asyncio.sleep(self.retry_delay * (2 ** attempt))
                    last_exception = MatterhornAPIError(
                        response.status_code, response.text
                    )
                    continue

                # Client errors (4xx) - not retryable
                if 400 <= response.status_code < 500:
                    if response.status_code == 401:
                        raise MatterhornAuthenticationError(
                            response.status_code, response.text
                        )
                    if response.status_code == 403:
                        raise MatterhornAuthenticationError(
                            response.status_code, response.text
                        )
                    if response.status_code == 404:
                        raise MatterhornNotFoundError(
                            response.status_code, response.text
                        )
                    raise MatterhornAPIError(response.status_code, response.text)

                # Success
                return response

            except (MatterhornAPIError, MatterhornAuthenticationError, MatterhornNotFoundError, MatterhornRateLimitError):
                raise
            except Exception as exc:  # pylint: disable=broad-except
                last_exception = exc
                if attempt < self.retry_count:
                    await asyncio.sleep(self.retry_delay * (2 ** attempt))
                else:
                    raise

        # All retries exhausted
        if last_exception is not None:
            raise last_exception
        raise MatterhornAPIError(0, "Unknown request failure")

    # ── Pagination ─────────────────────────────────────────────────────────

    async def iterate_paged(
        self,
        endpoint: str,
        page_param: str = "page",
        limit_param: str = "limit",
        default_limit: int = DEFAULT_PAGE_SIZE,
        **kwargs: Any,
    ) -> Iterator[dict[str, Any]]:
        """Yield all items from a paginated endpoint.

        Args:
            endpoint: API endpoint path (relative to base_url)
            page_param: Query parameter name for pagination page
            limit_param: Query parameter name for page size
            default_limit: Default page size if not specified
            **kwargs: Additional query parameters

        Yields:
            Individual items from each page
        """
        page: int = 1
        limit: int = default_limit

        while True:
            response = await self._request(
                "GET",
                endpoint,
                params={page_param: page, limit_param: limit, **kwargs},
            )

            data = response.json()
            items = data.get("items") or data.get("products") or data.get("data") or []
            total = data.get("total") or data.get("total_items") or 0

            for item in items:
                yield item

            # Pagination termination
            if total == 0 or len(items) == 0:
                break

            if page >= total:
                break

            page += 1
            # Adaptive page size: increase up to max, but respect remaining
            if limit < MAX_PAGE_SIZE:
                limit = min(MAX_PAGE_SIZE, limit * 2)

    # ── Core API Methods ──────────────────────────────────────────────────

    # ── Products ──────────────────────────────────────────────────────────

    async def get_items(
        self,
        page: int = 1,
        limit: int | None = None,
        brand_id: int | None = None,
        category_id: int | None = None,
        new_collection: bool | None = None,
        last_update: str | None = None,
    ) -> dict[str, Any]:
        """Retrieve a page of products from Matterhorn.

        Supported filters:
        - page: Page number (1-based)
        - limit: Items per page (max 1000)
        - brand_id: Filter by brand ID
        - category_id: Filter by category ID
        - new_collection: Filter for new collection items
        - last_update: Filter by ISO datetime, only items updated after this time

        Returns a dict with 'items', 'total', 'page', 'limit', etc.
        """
        params: dict[str, Any] = {
            "page": page,
        }
        if limit is not None:
            params["limit"] = min(limit, MAX_PAGE_SIZE)
        if brand_id is not None:
            params["brand_id"] = brand_id
        if category_id is not None:
            params["category_id"] = category_id
        if new_collection is not None:
            params["new_collection"] = "1" if new_collection else "0"
        if last_update is not None:
            # Ensure ISO 8601 format
            if isinstance(last_update, datetime):
                last_update = last_update.astimezone(timezone.utc).isoformat()
            params["last_update"] = last_update

        response = await self._request("GET", "/ITEMS/", params=params)
        return response.json()

    async def get_item(self, product_id: int | str) -> dict[str, Any]:
        """Retrieve a single product by ID."""
        response = await self._request("GET", f"/ITEMS/{product_id}")
        return response.json()

    # ── Dictionaries ─────────────────────────────────────────────────────

    async def get_brands(self) -> list[dict[str, Any]]:
        """Retrieve all brands from the dictionary."""
        response = await self._request("GET", "/DICTIONARIES/BRANDS")
        return response.json()

    async def get_categories(self) -> list[dict[str, Any]]:
        """Retrieve all categories from the dictionary."""
        response = await self._request("GET", "/DICTIONARIES/CATEGORIES")
        return response.json()

    async def get_delivery(self, country: str | None = None) -> list[dict[str, Any]]:
        """Retrieve delivery/duty information.

        Args:
            country: ISO country code (DE, AT, CH, etc.) or None for all
        """
        params: dict[str, Any] = {}
        if country:
            params["country"] = country
        response = await self._request("GET", "/DICTIONARIES/DELIVERY", params=params)
        return response.json()

    # ── Orders ───────────────────────────────────────────────────────────

    async def create_order(
        self,
        items: list[dict[str, Any]],
        delivery_to: dict[str, str],
        currency: str = "EUR",
        delivery_method_id: int | None = None,
    ) -> dict[str, Any]:
        """Create a new order in Matterhorn.

        Order payload according to official documentation:
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
            "delivery_method_id": N (optional)
        }

        Args:
            items: List of item dicts with 'variant_uid' and 'quantity'
            delivery_to: Delivery address dict with required fields
            currency: Currency code (default EUR)
            delivery_method_id: Optional delivery method ID

        Returns:
            Order response from Matterhorn API
        """
        payload: dict[str, Any] = {
            "items": items,
            "delivery_to": delivery_to,
            "currency": currency,
        }
        if delivery_method_id is not None:
            payload["delivery_method_id"] = delivery_method_id

        response = await self._request("PUT", "/ACCOUNT/ORDERS/", json_body=payload)
        return response.json()

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """Retrieve an order by its ID."""
        response = await self._request("GET", f"/ACCOUNT/ORDERS/{order_id}")
        return response.json()

    # ── Configuration ─────────────────────────────────────────────────────

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def success_count(self) -> int:
        return self._success_count

    @property
    def api_version(self) -> str | None:
        return self._api_version

    @property
    def endpoint_stats(self) -> dict[str, int]:
        return self._endpoint_counts.copy()


# ── Module-level convenience ────────────────────────────────────────────────

async def create_connection_manager(
    api_key: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> MatterhornConnectionManager:
    """Factory function to create a configured MatterhornConnectionManager."""
    cm = MatterhornConnectionManager(base_url=base_url, api_key=api_key)
    await cm.__aenter__()
    return cm