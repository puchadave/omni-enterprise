"""Google Merchant Center API Client.

Implements the official Google Merchant API v1 for product management,
inventory synchronization, and diagnostics.

Authentication:
- OAuth2 with access/refresh tokens
- Service Account credentials

API Base: https://merchantapi.googleapis.com/

Not using the discontinued Content API.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_BASE_URL: str = "https://merchantapi.googleapis.com/"
DEFAULT_API_VERSION: str = "v1"
DEFAULT_TIMEOUT: float = 30.0
DEFAULT_RETRY_COUNT: int = 3
DEFAULT_RETRY_DELAY: float = 1.0
TOKEN_REFRESH_MARGIN_SECONDS: int = 60


# ── Exceptions ─────────────────────────────────────────────────────────────

class GoogleMerchantError(RuntimeError):
    """Raised when the Google Merchant API returns an error."""
    def __init__(self, status_code: int, message: str, response_text: str | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.response_text = response_text
        super().__init__(f"Google Merchant API {status_code}: {message}")


class GoogleMerchantAuthenticationError(GoogleMerchantError):
    """Raised when authentication fails."""


class GoogleMerchantNotFoundError(GoogleMerchantError):
    """Raised when a resource is not found."""


class GoogleMerchantRateLimitError(GoogleMerchantError):
    """Raised when rate limit is hit."""


# ── OAuth2 Token Management ──────────────────────────────────────────────

class GoogleOAuth2Token:
    """OAuth2 access token with expiration tracking."""

    def __init__(self, access_token: str, expires_in: int,
                 refresh_token: str | None = None) -> None:
        self.access_token: str = access_token
        self.expires_in: int = expires_in
        self.refresh_token: str | None = refresh_token
        self.obtained_at: datetime = datetime.now(timezone.utc)

    @property
    def is_expired(self) -> bool:
        """Check if token is expired (with refresh margin)."""
        expiry = self.obtained_at + timedelta(seconds=self.expires_in - TOKEN_REFRESH_MARGIN_SECONDS)
        return datetime.now(timezone.utc) >= expiry

    @property
    def authorization_header(self) -> dict[str, str]:
        """Return Authorization header dict."""
        return {"Authorization": f"Bearer {self.access_token}"}


# ── Google Merchant API Client ───────────────────────────────────────────

class GoogleMerchantClient:
    """Client for the Google Merchant API v1.

    Handles:
    - OAuth2 authentication with automatic token refresh
    - Service Account authentication
    - Product input management (insert, update, delete)
    - Data source management
    - Inventory synchronization
    - Product diagnostics
    - Account configuration
    - Rate limiting and retry logic
    """

    def __init__(
        self,
        merchant_id: str,
        base_url: str = DEFAULT_BASE_URL,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = DEFAULT_TIMEOUT,
        retry_count: int = DEFAULT_RETRY_COUNT,
        retry_delay: float = DEFAULT_RETRY_DELAY,
    ) -> None:
        self.merchant_id: str = merchant_id
        self.base_url: str = base_url.rstrip("/")
        self.api_version: str = api_version
        self.timeout: float = timeout
        self.retry_count: int = retry_count
        self.retry_delay: float = retry_delay
        self._client: httpx.AsyncClient | None = None
        self._token: GoogleOAuth2Token | None = None
        self._service_account_credentials: dict[str, Any] | None = None
        self._request_count: int = 0
        self._error_count: int = 0
        self._success_count: int = 0
        self._endpoint_counts: dict[str, int] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def _ensure_client(self) -> None:
        """Ensure the HTTP client is initialized."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> GoogleMerchantClient:
        await self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ── Authentication ──────────────────────────────────────────────────

    def set_oauth2_token(self, access_token: str, expires_in: int,
                          refresh_token: str | None = None) -> None:
        """Set an OAuth2 access token."""
        self._token = GoogleOAuth2Token(access_token, expires_in, refresh_token)

    def set_service_account(self, credentials: dict[str, Any]) -> None:
        """Set service account credentials."""
        self._service_account_credentials = credentials

    async def _authenticate_oauth2(self) -> GoogleOAuth2Token:
        """Refresh OAuth2 token using refresh token."""
        if not self._token or not self._token.refresh_token:
            raise GoogleMerchantAuthenticationError(401, "No refresh token available")

        await self._ensure_client()
        assert self._client is not None

        token_url = "https://oauth2.googleapis.com/token"
        payload = {
            "client_id": self._service_account_credentials.get("client_id", "") if self._service_account_credentials else "",
            "client_secret": "",
            "refresh_token": self._token.refresh_token,
            "grant_type": "refresh_token",
        }

        response = await self._client.post(token_url, data=payload)
        if response.status_code == 200:
            data = response.json()
            self._token = GoogleOAuth2Token(
                access_token=data["access_token"],
                expires_in=int(data.get("expires_in", 3600)),
                refresh_token=self._token.refresh_token,
            )
            logger.info("OAuth2 token refreshed successfully")
            return self._token
        else:
            raise GoogleMerchantAuthenticationError(
                response.status_code, "Token refresh failed", response.text
            )

    async def _get_valid_token(self) -> GoogleOAuth2Token:
        """Return a valid access token, refreshing if needed."""
        if self._token is None or self._token.is_expired:
            if self._token and self._token.refresh_token:
                return await self._authenticate_oauth2()
            raise GoogleMerchantAuthenticationError(401, "No valid token available")
        return self._token

    # ── Request Execution ───────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute an authenticated API request with retry logic."""
        token = await self._get_valid_token()
        url = f"/{self.api_version}/{path.lstrip('/')}"
        headers: dict[str, str] = dict(token.authorization_header)
        parent = f"accounts/{self.merchant_id}"

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
                    await __import__("asyncio").sleep(delay)
                    last_exception = GoogleMerchantRateLimitError(
                        response.status_code, "Rate limit hit", response.text
                    )
                    continue

                # Retry on server errors (5xx)
                if 500 <= response.status_code < 600 and attempt < self.retry_count:
                    await __import__("asyncio").sleep(self.retry_delay * (2 ** attempt))
                    last_exception = GoogleMerchantError(response.status_code, response.text)
                    continue

                # Client errors (4xx)
                if 400 <= response.status_code < 500:
                    if response.status_code == 401:
                        self._token = None
                        raise GoogleMerchantAuthenticationError(
                            response.status_code, response.text, response.text
                        )
                    if response.status_code == 403:
                        raise GoogleMerchantAuthenticationError(
                            response.status_code, response.text, response.text
                        )
                    if response.status_code == 404:
                        raise GoogleMerchantNotFoundError(
                            response.status_code, response.text, response.text
                        )
                    raise GoogleMerchantError(response.status_code, response.text, response.text)

                self._success_count += 1
                ep = path.split("/")[-1] if path else "root"
                self._endpoint_counts[ep] = self._endpoint_counts.get(ep, 0) + 1
                return response

            except (GoogleMerchantError, GoogleMerchantAuthenticationError,
                    GoogleMerchantNotFoundError, GoogleMerchantRateLimitError):
                raise
            except Exception as exc:
                last_exception = exc
                if attempt < self.retry_count:
                    await __import__("asyncio").sleep(self.retry_delay * (2 ** attempt))
                else:
                    raise

        if last_exception is not None:
            raise last_exception
        raise GoogleMerchantError(0, "Unknown request failure")

    # ── Product Management ──────────────────────────────────────────────

    async def insert_product(self, product_data: dict[str, Any],
                              data_source_id: str | None = None) -> dict[str, Any]:
        """Insert a new product into Google Merchant Center."""
        params: dict[str, Any] = {}
        if data_source_id:
            params["dataSource"] = data_source_id

        response = await self._request(
            "POST",
            f"accounts/{self.merchant_id}/products",
            params=params,
            json_body=product_data,
        )
        return response.json()

    async def get_product(self, offer_id: str,
                           data_source_id: str | None = None) -> dict[str, Any]:
        """Retrieve a single product by offer ID."""
        params: dict[str, Any] = {}
        if data_source_id:
            params["dataSource"] = data_source_id

        response = await self._request(
            "GET",
            f"accounts/{self.merchant_id}/products/{offer_id}",
            params=params,
        )
        return response.json()

    async def update_product(self, product_data: dict[str, Any],
                              data_source_id: str | None = None) -> dict[str, Any]:
        """Update an existing product."""
        params: dict[str, Any] = {}
        if data_source_id:
            params["dataSource"] = data_source_id

        offer_id = product_data.get("offerId", "")
        response = await self._request(
            "PUT",
            f"accounts/{self.merchant_id}/products/{offer_id}",
            params=params,
            json_body=product_data,
        )
        return response.json()

    async def delete_product(self, offer_id: str,
                              data_source_id: str | None = None) -> dict[str, Any]:
        """Delete a product from Google Merchant Center."""
        params: dict[str, Any] = {}
        if data_source_id:
            params["dataSource"] = data_source_id

        response = await self._request(
            "DELETE",
            f"accounts/{self.merchant_id}/products/{offer_id}",
            params=params,
        )
        return response.json()

    async def list_products(self, page_size: int = 100,
                             page_token: str | None = None,
                             data_source_id: str | None = None) -> dict[str, Any]:
        """List products with pagination."""
        params: dict[str, Any] = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        if data_source_id:
            params["dataSource"] = data_source_id

        response = await self._request(
            "GET",
            f"accounts/{self.merchant_id}/products",
            params=params,
        )
        return response.json()

    # ── Inventory Synchronization ───────────────────────────────────────

    async def update_inventory(self, offer_id: str, quantity: int,
                                data_source_id: str | None = None) -> dict[str, Any]:
        """Update product inventory quantity."""
        inventory_update = {
            "offerId": offer_id,
            "quantity": quantity,
        }
        params: dict[str, Any] = {}
        if data_source_id:
            params["dataSource"] = data_source_id

        response = await self._request(
            "POST",
            f"accounts/{self.merchant_id}/products/{offer_id}:updateInventory",
            params=params,
            json_body=inventory_update,
        )
        return response.json()

    # ── Product Diagnostics ─────────────────────────────────────────────

    async def get_product_diagnostics(self, offer_id: str,
                                       data_source_id: str | None = None) -> dict[str, Any]:
        """Get product diagnostics/issues."""
        params: dict[str, Any] = {}
        if data_source_id:
            params["dataSource"] = data_source_id

        response = await self._request(
            "GET",
            f"accounts/{self.merchant_id}/products/{offer_id}/diagnostics",
            params=params,
        )
        return response.json()

    # ── Data Source Management ──────────────────────────────────────────

    async def list_data_sources(self) -> dict[str, Any]:
        """List all data sources for the merchant account."""
        response = await self._request(
            "GET",
            f"accounts/{self.merchant_id}/dataSources",
        )
        return response.json()

    async def get_data_source(self, data_source_id: str) -> dict[str, Any]:
        """Get a single data source by ID."""
        response = await self._request(
            "GET",
            f"accounts/{self.merchant_id}/dataSources/{data_source_id}",
        )
        return response.json()

    # ── Account Information ─────────────────────────────────────────────

    async def get_account_info(self) -> dict[str, Any]:
        """Get merchant account information."""
        response = await self._request(
            "GET",
            f"accounts/{self.merchant_id}",
        )
        return response.json()

    # ── Health Check ────────────────────────────────────────────────────

    async def check_health(self) -> dict[str, Any]:
        """Check API availability and authentication status."""
        await self._ensure_client()
        start = time.monotonic()
        try:
            response = await self.get_account_info()
            elapsed = time.monotonic() - start
            self._success_count += 1
            return {
                "status": "healthy",
                "merchant_id": self.merchant_id,
                "api_version": self.api_version,
                "response_time_ms": int(elapsed * 1000),
                "authenticated": self._token is not None and not self._token.is_expired,
                "account_info": response,
            }
        except GoogleMerchantAuthenticationError as exc:
            self._error_count += 1
            return {
                "status": "auth_error",
                "response_time_ms": int((time.monotonic() - start) * 1000),
                "error": str(exc),
            }
        except Exception as exc:  # pylint: disable=broad-except
            self._error_count += 1
            return {
                "status": "error",
                "response_time_ms": int((time.monotonic() - start) * 1000),
                "error": str(exc),
            }

    # ── Configuration ───────────────────────────────────────────────────

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
    def endpoint_stats(self) -> dict[str, int]:
        return self._endpoint_counts.copy()

    @property
    def is_authenticated(self) -> bool:
        return self._token is not None and not self._token.is_expired


# ── Module-level convenience ──────────────────────────────────────────────

async def create_google_merchant_client(
    merchant_id: str,
    base_url: str = DEFAULT_BASE_URL,
    api_version: str = DEFAULT_API_VERSION,
) -> GoogleMerchantClient:
    """Factory function to create a configured GoogleMerchantClient."""
    client = GoogleMerchantClient(
        merchant_id=merchant_id,
        base_url=base_url,
        api_version=api_version,
    )
    await client.__aenter__()
    return client