"""OmniRoute Gateway - Provider Management.

Dynamic provider management for the OmniRoute AI Gateway.
Supports multiple providers with modular adapters, health monitoring,
and credential management.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Provider Statuses ────────────────────────────────────────────────────

PROVIDER_HEALTHY: str = "healthy"
PROVIDER_DEGRADED: str = "degraded"
PROVIDER_UNHEALTHY: str = "unhealthy"
PROVIDER_UNKNOWN: str = "unknown"


# ── Provider Definition ──────────────────────────────────────────────────

class AIProvider:
    """Represents an AI provider configuration."""

    def __init__(
        self,
        provider_id: str,
        name: str,
        provider_type: str,
        api_endpoint: str,
        api_key_encrypted: str,
        auth_type: str = "bearer",
        priority: int = 100,
        rate_limit_per_minute: int = 60,
        timeout_seconds: int = 30,
        max_retries: int = 3,
        retry_delay_seconds: int = 1,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout_seconds: int = 300,
        capabilities: list[str] | None = None,
        health_status: str = PROVIDER_UNKNOWN,
        is_active: bool = True,
    ) -> None:
        self.provider_id: str = provider_id
        self.name: str = name
        self.provider_type: str = provider_type
        self.api_endpoint: str = api_endpoint
        self.api_key_encrypted: str = api_key_encrypted
        self.auth_type: str = auth_type
        self.priority: int = priority
        self.rate_limit_per_minute: int = rate_limit_per_minute
        self.timeout_seconds: int = timeout_seconds
        self.max_retries: int = max_retries
        self.retry_delay_seconds: int = retry_delay_seconds
        self.circuit_breaker_threshold: int = circuit_breaker_threshold
        self.circuit_breaker_timeout_seconds: int = circuit_breaker_timeout_seconds
        self.capabilities: list[str] = capabilities or []
        self.health_status: str = health_status
        self.is_active: bool = is_active
        self.last_health_check: str | None = None
        self.created_at: str = datetime.now(timezone.utc).isoformat()
        self.updated_at: str = self.created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "name": self.name,
            "provider_type": self.provider_type,
            "api_endpoint": self.api_endpoint,
            "auth_type": self.auth_type,
            "priority": self.priority,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "retry_delay_seconds": self.retry_delay_seconds,
            "circuit_breaker_threshold": self.circuit_breaker_threshold,
            "circuit_breaker_timeout_seconds": self.circuit_breaker_timeout_seconds,
            "capabilities": self.capabilities,
            "health_status": self.health_status,
            "is_active": self.is_active,
            "last_health_check": self.last_health_check,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ── Provider Manager ─────────────────────────────────────────────────────

class ProviderManager:
    """Manages AI providers with health monitoring and credential handling."""

    def __init__(self, db: Any | None = None) -> None:
        self.db: Any | None = db
        self.providers: dict[str, AIProvider] = {}
        self._health_check_interval: int = 300  # 5 minutes
        self._last_health_check: float = 0.0

    def add_provider(self, provider: AIProvider) -> None:
        """Add a new provider."""
        self.providers[provider.provider_id] = provider
        logger.info("Added AI provider: %s (%s)", provider.name, provider.provider_id)

    def remove_provider(self, provider_id: str) -> bool:
        """Remove a provider. Returns True if removed."""
        if provider_id in self.providers:
            del self.providers[provider_id]
            logger.info("Removed AI provider: %s", provider_id)
            return True
        return False

    def get_provider(self, provider_id: str) -> AIProvider | None:
        """Get a provider by ID."""
        return self.providers.get(provider_id)

    def list_providers(self, active_only: bool = False) -> list[AIProvider]:
        """List all providers."""
        providers = list(self.providers.values())
        if active_only:
            providers = [p for p in providers if p.is_active]
        return sorted(providers, key=lambda p: (p.priority, p.name))

    def enable_provider(self, provider_id: str) -> bool:
        """Enable a provider."""
        provider = self.providers.get(provider_id)
        if provider:
            provider.is_active = True
            provider.updated_at = datetime.now(timezone.utc).isoformat()
            return True
        return False

    def disable_provider(self, provider_id: str) -> bool:
        """Disable a provider."""
        provider = self.providers.get(provider_id)
        if provider:
            provider.is_active = False
            provider.updated_at = datetime.now(timezone.utc).isoformat()
            return True
        return False

    def update_provider(self, provider_id: str, **updates: Any) -> bool:
        """Update provider configuration."""
        provider = self.providers.get(provider_id)
        if not provider:
            return False
        for key, value in updates.items():
            if hasattr(provider, key):
                setattr(provider, key, value)
        provider.updated_at = datetime.now(timezone.utc).isoformat()
        return True

    async def test_connection(self, provider_id: str) -> dict[str, Any]:
        """Test connection to a provider."""
        provider = self.providers.get(provider_id)
        if not provider:
            return {
                "provider_id": provider_id,
                "status": "error",
                "error": "Provider not found",
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        import httpx
        start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=provider.timeout_seconds) as client:
                response = await client.get(
                    f"{provider.api_endpoint.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {provider.api_key_encrypted}"},
                )
                elapsed = time.monotonic() - start

                if response.status_code == 200:
                    provider.health_status = PROVIDER_HEALTHY
                    provider.last_health_check = datetime.now(timezone.utc).isoformat()
                    return {
                        "provider_id": provider_id,
                        "status": "healthy",
                        "response_time_ms": int(elapsed * 1000),
                        "checked_at": provider.last_health_check,
                    }
                elif response.status_code == 401:
                    provider.health_status = PROVIDER_UNHEALTHY
                    return {
                        "provider_id": provider_id,
                        "status": "auth_error",
                        "response_time_ms": int(elapsed * 1000),
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    provider.health_status = PROVIDER_DEGRADED
                    return {
                        "provider_id": provider_id,
                        "status": "degraded",
                        "response_time_ms": int(elapsed * 1000),
                        "error": f"HTTP {response.status_code}",
                        "checked_at": datetime.now(timezone.utc).isoformat(),
                    }
        except Exception as exc:  # pylint: disable=broad-except
            provider.health_status = PROVIDER_UNHEALTHY
            provider.last_health_check = datetime.now(timezone.utc).isoformat()
            return {
                "provider_id": provider_id,
                "status": "error",
                "error": str(exc),
                "checked_at": provider.last_health_check,
            }

    async def health_check_all(self) -> dict[str, Any]:
        """Health check all providers."""
        results: dict[str, Any] = {}
        tasks = []

        for provider in self.providers.values():
            if provider.is_active:
                tasks.append(self.test_connection(provider.provider_id))

        if tasks:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
            for provider_id, outcome in zip(
                [p.provider_id for p in self.providers.values() if p.is_active],
                outcomes,
            ):
                if isinstance(outcome, Exception):
                    results[provider_id] = {
                        "status": "error",
                        "error": str(outcome),
                    }
                else:
                    results[provider_id] = outcome

        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "providers": results,
            "healthy_count": sum(1 for r in results.values() if r.get("status") == "healthy"),
            "total_count": len(results),
        }

    def discover_models(self, provider_id: str) -> list[dict[str, Any]]:
        """Discover available models from a provider."""
        provider = self.providers.get(provider_id)
        if not provider:
            return []

        # This would call the provider's models endpoint
        # For now, return the provider's capabilities as model hints
        models = []
        for cap in provider.capabilities:
            models.append({
                "model_id": f"{provider_id}-{cap}",
                "model_name": f"{provider.name} - {cap}",
                "model_category": provider.provider_type,
                "capabilities": [cap],
                "source": "discovered",
            })
        return models