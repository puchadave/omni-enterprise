"""Competitor Intelligence - Research Provider Architecture.

Modular provider system for searching and comparing products across
competitor websites and marketplaces. Each provider implements a common
interface for search, product details, price retrieval, and availability.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Provider Status ───────────────────────────────────────────────────────

PROVIDER_CONNECTED: str = "connected"
PROVIDER_AUTH_REQUIRED: str = "authentication_required"
PROVIDER_RATE_LIMITED: str = "rate_limited"
PROVIDER_TEMPORARILY_UNAVAILABLE: str = "temporarily_unavailable"
PROVIDER_DISABLED: str = "disabled"
PROVIDER_ERROR: str = "error"


# ── Search Result ─────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """A single product search result from a provider."""
    source_url: str
    source_name: str
    ean: str = ""
    sku: str = ""
    product_name: str = ""
    brand: str = ""
    category: str = ""
    price: float = 0.0
    shipping_cost: float = 0.0
    total_price: float = 0.0
    currency: str = "EUR"
    availability: str = ""
    data_source: str = ""
    confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_url": self.source_url,
            "source_name": self.source_name,
            "ean": self.ean,
            "sku": self.sku,
            "product_name": self.product_name,
            "brand": self.brand,
            "category": self.category,
            "price": self.price,
            "shipping_cost": self.shipping_cost,
            "total_price": self.total_price,
            "currency": self.currency,
            "availability": self.availability,
            "data_source": self.data_source,
            "confidence": self.confidence,
        }


# ── Base Provider ─────────────────────────────────────────────────────────

class ResearchProvider(ABC):
    """Abstract base class for competitor research providers."""

    def __init__(
        self,
        provider_id: str,
        name: str,
        api_key: str | None = None,
        base_url: str | None = None,
        rate_limit_per_second: float = 1.0,
        timeout: float = 30.0,
    ) -> None:
        self.provider_id: str = provider_id
        self.name: str = name
        self.api_key: str | None = api_key
        self.base_url: str | None = base_url
        self.rate_limit_per_second: float = rate_limit_per_second
        self.timeout: float = timeout
        self.status: str = PROVIDER_DISABLED
        self.last_error: str | None = None
        self._client: Any | None = None

    @abstractmethod
    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Search for products matching the query."""
        ...

    @abstractmethod
    async def get_product_details(self, source_url: str) -> SearchResult | None:
        """Get detailed product information from a source URL."""
        ...

    @abstractmethod
    async def get_price(self, source_url: str) -> dict[str, Any] | None:
        """Get current price and availability for a product."""
        ...

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """Check if the provider is reachable and authenticated."""
        ...

    async def initialize(self) -> None:
        """Initialize the provider (create HTTP client, authenticate)."""
        self.status = PROVIDER_AUTH_REQUIRED if self.api_key is None else PROVIDER_CONNECTED

    async def close(self) -> None:
        """Close the provider and release resources."""
        self._client = None
        self.status = PROVIDER_DISABLED

    def _rate_limit_delay(self) -> float:
        """Return the minimum delay between requests in seconds."""
        return 1.0 / max(self.rate_limit_per_second, 0.1)


# ── Mock/Example Provider (for development without real API keys) ─────────

class ExampleProvider(ResearchProvider):
    """Example provider for development and testing.

    This provider returns simulated results and is clearly marked as
    a placeholder. Real providers should be implemented with actual
    API integrations.
    """

    def __init__(self, provider_id: str = "example", **kwargs: Any) -> None:
        super().__init__(provider_id=provider_id, name="Example Provider", **kwargs)

    async def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Return simulated search results."""
        await asyncio.sleep(0.1)  # Simulate network delay
        results = []
        for i in range(min(limit, 3)):
            results.append(SearchResult(
                source_url=f"https://example-shop.de/product/{i}",
                source_name="Example Shop",
                ean=f"400000000000{i}",
                sku=f"EX-{i:04d}",
                product_name=f"{query} - Beispielprodukt {i+1}",
                brand="Beispiel-Marke",
                category="Mode",
                price=29.99 + (i * 5.0),
                shipping_cost=4.95,
                total_price=34.94 + (i * 5.0),
                currency="EUR",
                availability="auf Lager",
                data_source="example_provider",
                confidence=0.85 - (i * 0.1),
            ))
        return results

    async def get_product_details(self, source_url: str) -> SearchResult | None:
        """Return simulated product details."""
        return SearchResult(
            source_url=source_url,
            source_name="Example Shop",
            product_name="Beispielprodukt",
            brand="Beispiel-Marke",
            price=29.99,
            shipping_cost=4.95,
            total_price=34.94,
            currency="EUR",
            availability="auf Lager",
            data_source="example_provider",
            confidence=0.9,
        )

    async def get_price(self, source_url: str) -> dict[str, Any] | None:
        """Return simulated price."""
        return {
            "price": 29.99,
            "shipping_cost": 4.95,
            "total_price": 34.94,
            "currency": "EUR",
            "availability": "auf Lager",
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    async def health_check(self) -> dict[str, Any]:
        """Return simulated health status."""
        self.status = PROVIDER_CONNECTED
        return {
            "provider_id": self.provider_id,
            "status": self.status,
            "response_time_ms": 50,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }


# ── Provider Registry ─────────────────────────────────────────────────────

class ProviderRegistry:
    """Manages research providers."""

    def __init__(self) -> None:
        self.providers: dict[str, ResearchProvider] = {}
        self._initialized: bool = False

    def register(self, provider: ResearchProvider) -> None:
        """Register a provider."""
        self.providers[provider.provider_id] = provider
        logger.info("Registered provider: %s (%s)", provider.name, provider.provider_id)

    def unregister(self, provider_id: str) -> None:
        """Unregister a provider."""
        self.providers.pop(provider_id, None)

    def get(self, provider_id: str) -> ResearchProvider | None:
        """Get a provider by ID."""
        return self.providers.get(provider_id)

    def list(self) -> list[ResearchProvider]:
        """List all registered providers."""
        return list(self.providers.values())

    def list_by_status(self, status: str) -> list[ResearchProvider]:
        """List providers by status."""
        return [p for p in self.providers.values() if p.status == status]

    async def initialize_all(self) -> None:
        """Initialize all providers."""
        for provider in self.providers.values():
            try:
                await provider.initialize()
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Failed to initialize provider %s: %s", provider.provider_id, exc)
                provider.status = PROVIDER_ERROR
                provider.last_error = str(exc)
        self._initialized = True

    async def close_all(self) -> None:
        """Close all providers."""
        for provider in self.providers.values():
            try:
                await provider.close()
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Error closing provider %s: %s", provider.provider_id, exc)

    async def search_all(self, query: str, limit_per_provider: int = 5) -> dict[str, list[SearchResult]]:
        """Search across all connected providers."""
        results: dict[str, list[SearchResult]] = {}
        tasks = []

        for provider in self.providers.values():
            if provider.status in (PROVIDER_CONNECTED, PROVIDER_AUTH_REQUIRED):
                tasks.append(self._search_provider(provider, query, limit_per_provider))

        if tasks:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for provider, outcome in zip(
                [p for p in self.providers.values() if p.status in (PROVIDER_CONNECTED, PROVIDER_AUTH_REQUIRED)],
                outcomes,
            ):
                if isinstance(outcome, Exception):
                    logger.error("Provider %s search failed: %s", provider.provider_id, outcome)
                    results[provider.provider_id] = []
                else:
                    results[provider.provider_id] = outcome

        return results

    async def _search_provider(
        self,
        provider: ResearchProvider,
        query: str,
        limit: int,
    ) -> list[SearchResult]:
        """Search a single provider with error handling."""
        try:
            return await provider.search(query, limit)
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Provider %s search error: %s", provider.provider_id, exc)
            provider.status = PROVIDER_ERROR
            provider.last_error = str(exc)
            return []


# ── Global Registry ──────────────────────────────────────────────────────

provider_registry: ProviderRegistry = ProviderRegistry()

# Register example provider by default
provider_registry.register(ExampleProvider())