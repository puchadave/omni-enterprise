"""Shopware 6 API Connection Manager.

Implements authenticated access to Shopware 6 Store API and Admin API.
Handles client-credentials authentication, token refresh, retry logic,
rate-limit handling, and structured error classes.

Shopware 6 uses OAuth2 client-credentials flow:
- POST /oauth/token with client_id and client_secret
- Bearer token in Authorization header for subsequent requests
- Store API: /store-api/{endpoint}
- Admin API: /api/{endpoint}
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from datetime import datetime, timezone, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Defaults ──────────────────────────────────────────────────────────────

DEFAULT_API_VERSION: str = "6.0"
DEFAULT_TIMEOUT: float = 30.0
DEFAULT_RETRY_COUNT: int = 3
DEFAULT_RETRY_DELAY: float = 1.0
TOKEN_REFRESH_MARGIN_SECONDS: int = 60


# ── Exceptions ─────────────────────────────────────────────────────────────

class ShopwareAPIError(RuntimeError):
    """Raised when the Shopware API returns a non-2xx status code."""
    def __init__(self, status_code: int, message: str, response_text: str | None = None) -> None:
        self.status_code = status_code
        self.message = message
        self.response_text = response_text
        super().__init__(f"Shopware API {status_code}: {message}")


class ShopwareAuthenticationError(ShopwareAPIError):
    """Raised when authentication fails (401/403)."""


class ShopwareNotFoundError(ShopwareAPIError):
    """Raised when a resource is not found (404)."""


class ShopwareRateLimitError(ShopwareAPIError):
    """Raised when rate limit is hit (429)."""


# ── Token Management ──────────────────────────────────────────────────────

class ShopwareAuthToken:
    """OAuth2 access token with expiration tracking."""

    def __init__(self, access_token: str, expires_in: int) -> None:
        self.access_token: str = access_token
        self.expires_in: int = expires_in
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


# ── Connection Manager ────────────────────────────────────────────────────

class ShopwareConnectionManager:
    """Central connection manager for Shopware 6 API.

    Handles:
    - OAuth2 client-credentials authentication
    - Token caching and automatic refresh
    - Retry logic with exponential backoff
    - Rate limit handling (429)
    - Store API and Admin API endpoints
    - Request/response logging (sans secrets)
    - Connection health checking
    """

    def __init__(
        self,
        shop_url: str,
        client_id: str,
        client_secret: str,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = DEFAULT_TIMEOUT,
        retry_count: int = DEFAULT_RETRY_COUNT,
        retry_delay: float = DEFAULT_RETRY_DELAY,
        host_header: str | None = None,
    ) -> None:
        self.shop_url: str = shop_url.rstrip("/")
        self.client_id: str = client_id
        self.client_secret: str = client_secret
        self.api_version: str = api_version
        self.timeout: float = timeout
        self.retry_count: int = retry_count
        self.retry_delay: float = retry_delay
        self.host_header: str | None = (host_header or "").strip() or None
        self._client: httpx.AsyncClient | None = None
        self._token: ShopwareAuthToken | None = None
        self._request_count: int = 0
        self._error_count: int = 0
        self._success_count: int = 0
        self._endpoint_counts: dict[str, int] = {}
        self._api_version_detected: str | None = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def _ensure_client(self) -> None:
        """Ensure the HTTP client is initialized."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.shop_url,
                timeout=self.timeout,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )

    async def close(self) -> None:
        """Close the HTTP client and release resources."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> ShopwareConnectionManager:
        await self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ── Authentication ──────────────────────────────────────────────────

    async def _authenticate(self) -> ShopwareAuthToken:
        """Perform OAuth2 client-credentials authentication."""
        await self._ensure_client()
        assert self._client is not None

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        headers = {"Host": self.host_header} if self.host_header else None
        last_resp = None
        for token_url in [f"{self.shop_url}/api/oauth/token", f"{self.shop_url}/oauth/token"]:
            response = await self._client.post(token_url, json=payload, headers=headers)
            self._request_count += 1
            last_resp = response
            if response.status_code == 200:
                data = response.json()
                self._success_count += 1
                token = ShopwareAuthToken(
                    access_token=data["access_token"],
                    expires_in=int(data.get("expires_in", 3600)),
                )
                self._token = token
                logger.info("Shopware authentication successful, token expires in %ds", token.expires_in)
                return token
            if response.status_code in (401, 403):
                self._error_count += 1
                raise ShopwareAuthenticationError(response.status_code, "Invalid client credentials", response.text)
            # try next endpoint on 404/html routing issues
            if response.status_code == 404 or "text/html" in (response.headers.get("content-type") or ""):
                continue
            self._error_count += 1
            raise ShopwareAPIError(response.status_code, response.text, response.text)
        if last_resp is not None:
            self._error_count += 1
            raise ShopwareAPIError(last_resp.status_code, last_resp.text, last_resp.text)
        raise ShopwareAPIError(0, "No token endpoint responded")

    async def _get_valid_token(self) -> ShopwareAuthToken:
        """Return a valid access token, refreshing if needed."""
        if self._token is None or self._token.is_expired:
            return await self._authenticate()
        return self._token

    # ── Request Execution ───────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        use_admin_api: bool = False,
    ) -> httpx.Response:
        """Execute a single HTTP request with retry logic and auth."""
        token = await self._get_valid_token()
        url = endpoint
        request_headers: dict[str, str] = dict(token.authorization_header)
        if self.host_header:
            request_headers["Host"] = self.host_header
        if headers:
            request_headers.update(headers)

        last_exception: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                self._request_count += 1
                response = await self._client.request(
                    method=method,
                    url=url,
                    headers=request_headers,
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
                )

                # Rate limit handling
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else self.retry_delay * (2 ** attempt)
                    await __import__("asyncio").sleep(delay)
                    last_exception = ShopwareRateLimitError(
                        response.status_code, "Rate limit hit", response.text
                    )
                    continue

                # Retry on server errors (5xx)
                if 500 <= response.status_code < 600 and attempt < self.retry_count:
                    await __import__("asyncio").sleep(self.retry_delay * (2 ** attempt))
                    last_exception = ShopwareAPIError(response.status_code, response.text)
                    continue

                # Client errors (4xx) - not retryable
                if 400 <= response.status_code < 500:
                    if response.status_code == 401:
                        # Token might be expired, force refresh
                        self._token = None
                        raise ShopwareAuthenticationError(
                            response.status_code, response.text, response.text
                        )
                    if response.status_code == 403:
                        raise ShopwareAuthenticationError(
                            response.status_code, response.text, response.text
                        )
                    if response.status_code == 404:
                        raise ShopwareNotFoundError(
                            response.status_code, response.text, response.text
                        )
                    raise ShopwareAPIError(response.status_code, response.text, response.text)

                # Success
                self._success_count += 1
                ep = endpoint.split("/")[-1] if endpoint else "root"
                self._endpoint_counts[ep] = self._endpoint_counts.get(ep, 0) + 1
                return response

            except (ShopwareAPIError, ShopwareAuthenticationError, ShopwareNotFoundError, ShopwareRateLimitError):
                raise
            except Exception as exc:
                last_exception = exc
                if attempt < self.retry_count:
                    await __import__("asyncio").sleep(self.retry_delay * (2 ** attempt))
                else:
                    raise

        if last_exception is not None:
            raise last_exception
        raise ShopwareAPIError(0, "Unknown request failure")

    # ── Store API Endpoints ─────────────────────────────────────────────

    async def get_products(
        self,
        limit: int = 25,
        page: int = 1,
        sales_channel_id: str | None = None,
        filter_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retrieve products from Shopware Store API."""
        headers: dict[str, str] = {}
        if sales_channel_id:
            headers["sw-context-token"] = sales_channel_id

        body: dict[str, Any] = {
            "filter": [{"type": "equals", "field": "active", "value": True}],
            "limit": limit,
            "page": page,
        }
        if filter_json:
            body["filter"].extend(filter_json.get("filter", []))

        response = await self._request("POST", "/store-api/product", json_body=body, headers=headers)
        return response.json()

    async def get_product(self, product_id: str) -> dict[str, Any]:
        """Retrieve a single product by ID."""
        response = await self._request("GET", f"/store-api/product/{product_id}")
        return response.json()

    async def create_product(self, product_data: dict[str, Any]) -> dict[str, Any]:
        """Create a new product via Admin API."""
        response = await self._request("POST", "/api/product", json_body=product_data, use_admin_api=True)
        if response.status_code == 204 or not (response.text or "").strip():
            loc = response.headers.get("location") or response.headers.get("Location") or ""
            pid = loc.rstrip("/").split("/")[-1] if loc else ""
            return {"id": pid} if pid else {"status": "ok"}
        try:
            return response.json()
        except Exception:
            return {"status": "ok", "raw": response.text[:500]}

    async def update_product(self, product_id: str, product_data: dict[str, Any]) -> dict[str, Any]:
        """Update an existing product via Admin API (PATCH)."""
        response = await self._request("PATCH", f"/api/product/{product_id}", json_body=product_data, use_admin_api=True)
        return response.json()

    async def delete_product(self, product_id: str) -> dict[str, Any]:
        """Delete a product via Admin API."""
        response = await self._request("DELETE", f"/api/product/{product_id}", use_admin_api=True)
        return response.json()

    # ── Categories ──────────────────────────────────────────────────────

    async def get_categories(self, limit: int = 100, page: int = 1) -> dict[str, Any]:
        """Retrieve categories from Shopware."""
        body = {"limit": limit, "page": page}
        response = await self._request("POST", "/store-api/category", json_body=body)
        return response.json()

    async def create_category(self, category_data: dict[str, Any]) -> dict[str, Any]:
        """Create a category via Admin API."""
        response = await self._request("POST", "/api/category", json_body=category_data, use_admin_api=True)
        return response.json()

    # ── Sales Channels ──────────────────────────────────────────────────

    async def get_sales_channels(self) -> dict[str, Any]:
        """Retrieve sales channels."""
        response = await self._request("POST", "/store-api/sales-channel", json_body={})
        return response.json()

    # ── Languages ───────────────────────────────────────────────────────

    async def get_languages(self) -> dict[str, Any]:
        """Retrieve available languages."""
        response = await self._request("POST", "/store-api/language", json_body={})
        return response.json()

    # ── Currencies ──────────────────────────────────────────────────────

    async def get_currencies(self) -> dict[str, Any]:
        """Retrieve available currencies."""
        response = await self._request("POST", "/store-api/currency", json_body={})
        return response.json()

    # ── Tax Rules ───────────────────────────────────────────────────────

    async def get_tax_rules(self) -> dict[str, Any]:
        """Retrieve tax rules."""
        response = await self._request("POST", "/store-api/tax", json_body={})
        return response.json()

    # ── Orders ──────────────────────────────────────────────────────────

    async def create_order(self, order_data: dict[str, Any]) -> dict[str, Any]:
        """Create an order via Admin API."""
        response = await self._request("POST", "/api/order", json_body=order_data, use_admin_api=True)
        return response.json()

    # ── Health & Diagnostics ────────────────────────────────────────────

    async def check_health(self) -> dict[str, Any]:
        """Check Shopware API availability and return basic info."""
        await self._ensure_client()
        start = time.monotonic()
        # Prefer admin API probes; dockware/sales-channel host routing can break store-api probes.
        for endpoint, use_admin in [("/api/_info/version", True), ("/api/_action/system-info", True), ("/store-api/context", False)]:
            try:
                response = await self._request("GET", endpoint, use_admin_api=use_admin)
                elapsed = time.monotonic() - start
                self._success_count += 1
                detected = self._api_version_detected or self.api_version
                try:
                    data = response.json()
                    if isinstance(data, dict):
                        detected = str(data.get("version") or data.get("shopwareVersion") or data.get("apiVersion") or detected)
                except Exception:
                    pass
                return {
                    "status": "healthy",
                    "shop_url": self.shop_url,
                    "api_version": self.api_version,
                    "detected_version": detected,
                    "response_time_ms": int(elapsed * 1000),
                    "authenticated": self._token is not None and not self._token.is_expired,
                }
            except ShopwareAuthenticationError as exc:
                self._error_count += 1
                return {"status": "auth_error", "response_time_ms": int((time.monotonic() - start) * 1000), "error": str(exc)}
            except Exception:
                continue
        self._error_count += 1
        return {"status": "error", "response_time_ms": int((time.monotonic() - start) * 1000), "error": "No Shopware health endpoint responded successfully"}

    async def detect_version(self) -> str | None:
        """Attempt to detect the Shopware version."""
        try:
            response = await self._request("GET", "/api/_action/system-info", use_admin_api=True)
            data = response.json()
            version = data.get("version") or data.get("shopwareVersion") or data.get("apiVersion")
            if version:
                self._api_version_detected = str(version)
                return str(version)
        except Exception:
            pass
        return self.api_version

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


    async def _request_binary(self, method: str, endpoint: str, *, content: bytes, content_type: str, params: dict[str, Any] | None = None) -> httpx.Response:
        token = await self._get_valid_token()
        headers = dict(token.authorization_header)
        headers["Content-Type"] = content_type
        if self.host_header:
            headers["Host"] = self.host_header
        await self._ensure_client()
        assert self._client is not None
        resp = await self._client.request(method, endpoint, params=params, content=content, headers=headers, timeout=max(self.timeout, 60.0))
        self._request_count += 1
        if resp.status_code >= 400:
            self._error_count += 1
            raise ShopwareAPIError(resp.status_code, resp.text, resp.text)
        self._success_count += 1
        return resp

    async def upload_product_media(self, *, file_path: str, file_name: str, mime_type: str = "image/jpeg", alt: str | None = None) -> str:
        import os, uuid
        await self._ensure_client()
        media_id = str(uuid.uuid4()).replace("-", "")
        await self._request("POST", "/api/media", json_body={"id": media_id, "alt": alt or file_name})
        ext = os.path.splitext(file_name)[1].lstrip(".") or "jpg"
        base = os.path.splitext(os.path.basename(file_name))[0]
        with open(file_path, "rb") as f:
            data = f.read()
        await self._request_binary("POST", f"/api/_action/media/{media_id}/upload", params={"extension": ext, "fileName": base}, content=data, content_type=mime_type)
        return media_id

    async def upsert_product_with_media(self, product_id: str | None, payload: dict[str, Any], media_files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        import uuid
        pid = str(product_id or payload.get("id") or uuid.uuid4()).replace("-", "")
        payload = dict(payload)
        payload["id"] = pid
        media_entries = []
        cover_id = None
        for i, m in enumerate(media_files or []):
            path = str(m.get("local_path") or m.get("path") or "")
            if not path or not os.path.exists(path):
                continue
            media_id = await self.upload_product_media(file_path=path, file_name=os.path.basename(path), mime_type=str(m.get("mime_type") or "image/jpeg"), alt=m.get("alt"))
            if i == 0:
                cover_id = media_id
            media_entries.append({"mediaId": media_id, "position": i})
        if media_entries:
            payload["media"] = media_entries
            payload["coverId"] = cover_id
        # create or update
        try:
            if product_id:
                await self._request("PATCH", f"/api/product/{pid}", json_body=payload)
            else:
                await self._request("POST", "/api/product", json_body=payload)
        except ShopwareNotFoundError:
            await self._request("POST", "/api/product", json_body=payload)
        return {"id": pid, "coverId": cover_id, "media_count": len(media_entries)}


# ── Module-level convenience ──────────────────────────────────────────────

async def create_shopware_connection(
    shop_url: str,
    client_id: str,
    client_secret: str,
    api_version: str = DEFAULT_API_VERSION,
) -> ShopwareConnectionManager:
    """Factory function to create a configured ShopwareConnectionManager."""
    cm = ShopwareConnectionManager(
        shop_url=shop_url,
        client_id=client_id,
        client_secret=client_secret,
        api_version=api_version,
    )
    await cm.__aenter__()
    return cm