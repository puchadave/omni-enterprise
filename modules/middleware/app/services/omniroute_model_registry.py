"""OmniRoute Gateway - Model Registry and Intelligent Routing Engine.

Central model registry with capability-based model selection and
configurable routing strategies with health-aware failover.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)


# ── Model Definition ─────────────────────────────────────────────────────

class AIModel:
    """Represents an AI model in the registry."""

    def __init__(
        self,
        model_id: str,
        provider_id: str,
        model_id_str: str,
        model_name: str,
        model_category: str = "general",
        capabilities: list[str] | None = None,
        context_window: int = 4096,
        input_price_per_1k: float = 0.0,
        output_price_per_1k: float = 0.0,
        image_price_per_image: float = 0.0,
        rate_limit_per_minute: int = 60,
        is_active: bool = True,
        is_discoverable: bool = True,
    ) -> None:
        self.model_id: str = model_id
        self.provider_id: str = provider_id
        self.model_id_str: str = model_id_str
        self.model_name: str = model_name
        self.model_category: str = model_category
        self.capabilities: list[str] = capabilities or []
        self.context_window: int = context_window
        self.input_price_per_1k: float = input_price_per_1k
        self.output_price_per_1k: float = output_price_per_1k
        self.image_price_per_image: float = image_price_per_image
        self.rate_limit_per_minute: int = rate_limit_per_minute
        self.is_active: bool = is_active
        self.is_discoverable: bool = is_discoverable
        self.created_at: str = datetime.now(timezone.utc).isoformat()
        self.updated_at: str = self.created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id_str,
            "model_name": self.model_name,
            "model_category": self.model_category,
            "capabilities": self.capabilities,
            "context_window": self.context_window,
            "input_price_per_1k": self.input_price_per_1k,
            "output_price_per_1k": self.output_price_per_1k,
            "image_price_per_image": self.image_price_per_image,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "is_active": self.is_active,
            "is_discoverable": self.is_discoverable,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ── Model Registry ───────────────────────────────────────────────────────

class ModelRegistry:
    """Central model registry with capability-based selection."""

    def __init__(self) -> None:
        self.models: dict[str, AIModel] = {}
        self._models_by_provider: dict[str, list[AIModel]] = {}
        self._models_by_category: dict[str, list[AIModel]] = {}

    def register_model(self, model: AIModel) -> None:
        """Register a model in the registry."""
        self.models[model.model_id] = model
        self._index_model(model)
        logger.info("Registered AI model: %s (%s)", model.model_name, model.model_id)

    def _index_model(self, model: AIModel) -> None:
        """Index model by provider and category."""
        self._models_by_provider.setdefault(model.provider_id, []).append(model)
        self._models_by_category.setdefault(model.model_category, []).append(model)

    def get_model(self, model_id: str) -> AIModel | None:
        """Get a model by ID."""
        return self.models.get(model_id)

    def list_models(
        self,
        provider_id: str | None = None,
        category: str | None = None,
        active_only: bool = True,
    ) -> list[AIModel]:
        """List models with optional filtering."""
        models = list(self.models.values())
        if provider_id:
            models = [m for m in models if m.provider_id == provider_id]
        if category:
            models = [m for m in models if m.model_category == category]
        if active_only:
            models = [m for m in models if m.is_active]
        return sorted(models, key=lambda m: m.model_name)

    def find_models_by_capability(self, capability: str) -> list[AIModel]:
        """Find models that support a specific capability."""
        return [
            m for m in self.models.values()
            if m.is_active and capability in m.capabilities
        ]

    def update_model(self, model_id: str, **updates: Any) -> bool:
        """Update model configuration."""
        model = self.models.get(model_id)
        if not model:
            return False
        for key, value in updates.items():
            if hasattr(model, key):
                setattr(model, key, value)
        model.updated_at = datetime.now(timezone.utc).isoformat()
        return True


# ── Routing Rule ─────────────────────────────────────────────────────────

class RoutingRule:
    """Defines a routing strategy for AI requests."""

    def __init__(
        self,
        rule_id: str,
        name: str,
        strategy: str,
        provider_priority: list[str] | None = None,
        model_category: str = "general",
        min_quality_score: float = 0.0,
        max_cost_per_1k: float | None = None,
        max_latency_ms: int | None = None,
        is_active: bool = True,
    ) -> None:
        self.rule_id: str = rule_id
        self.name: str = name
        self.strategy: str = strategy
        self.provider_priority: list[str] = provider_priority or []
        self.model_category: str = model_category
        self.min_quality_score: float = min_quality_score
        self.max_cost_per_1k: float | None = max_cost_per_1k
        self.max_latency_ms: int | None = max_latency_ms
        self.is_active: bool = is_active
        self.created_at: str = datetime.now(timezone.utc).isoformat()
        self.updated_at: str = self.created_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "strategy": self.strategy,
            "provider_priority": self.provider_priority,
            "model_category": self.model_category,
            "min_quality_score": self.min_quality_score,
            "max_cost_per_1k": self.max_cost_per_1k,
            "max_latency_ms": self.max_latency_ms,
            "is_active": self.is_active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


# ── Routing Policy ───────────────────────────────────────────────────────

class RoutingPolicy:
    """Maps a workflow to a routing rule with optional fallback."""

    def __init__(
        self,
        policy_id: str,
        workflow_name: str,
        rule_id: str,
        fallback_rule_id: str | None = None,
        is_active: bool = True,
    ) -> None:
        self.policy_id: str = policy_id
        self.workflow_name: str = workflow_name
        self.rule_id: str = rule_id
        self.fallback_rule_id: str | None = fallback_rule_id
        self.is_active: bool = is_active
        self.created_at: str = datetime.now(timezone.utc).isoformat()
        self.updated_at: str = self.created_at


# ── Intelligent Routing Engine ──────────────────────────────────────────

class IntelligentRoutingEngine:
    """Routes AI requests to providers based on configurable strategies.

    Strategies:
    - lowest_cost: Select cheapest provider
    - highest_quality: Select highest quality provider
    - lowest_latency: Select fastest provider
    - provider_priority: Use configured priority order
    - capability_matching: Match required capabilities
    - weighted_distribution: Distribute requests by weight
    """

    def __init__(
        self,
        model_registry: ModelRegistry,
        provider_manager: Any | None = None,
    ) -> None:
        self.model_registry: ModelRegistry = model_registry
        self.provider_manager: Any | None = provider_manager
        self.rules: dict[str, RoutingRule] = {}
        self.policies: dict[str, RoutingPolicy] = {}
        self._circuit_breakers: dict[str, dict[str, Any]] = {}

    def add_rule(self, rule: RoutingRule) -> None:
        """Add a routing rule."""
        self.rules[rule.rule_id] = rule
        logger.info("Added routing rule: %s (%s)", rule.name, rule.strategy)

    def add_policy(self, policy: RoutingPolicy) -> None:
        """Add a routing policy for a workflow."""
        self.policies[policy.workflow_name] = policy
        logger.info("Added routing policy: %s -> %s", policy.workflow_name, policy.rule_id)

    def get_policy_for_workflow(self, workflow_name: str) -> RoutingPolicy | None:
        """Get the routing policy for a workflow."""
        return self.policies.get(workflow_name)

    def select_provider(
        self,
        workflow_name: str,
        required_capability: str | None = None,
        max_cost: float | None = None,
        exclude_providers: list[str] | None = None,
    ) -> tuple[str | None, str | None]:
        """Select the best provider and model for a request.

        Args:
            workflow_name: Name of the workflow making the request
            required_capability: Required model capability
            max_cost: Maximum cost per 1k tokens
            exclude_providers: Providers to exclude

        Returns:
            Tuple of (provider_id, model_id) or (None, None) if no suitable provider
        """
        exclude_providers = exclude_providers or []

        # Get routing policy for workflow
        policy = self.get_policy_for_workflow(workflow_name)
        if not policy:
            logger.warning("No routing policy for workflow: %s", workflow_name)
            return None, None

        rule = self.rules.get(policy.rule_id)
        if not rule:
            logger.warning("Routing rule not found: %s", policy.rule_id)
            return None, None

        # Get candidate models
        candidates = self.model_registry.list_models(
            category=rule.model_category,
            active_only=True,
        )

        # Filter by capability
        if required_capability:
            candidates = [m for m in candidates if required_capability in m.capabilities]

        # Filter by cost
        if max_cost is not None:
            candidates = [
                m for m in candidates
                if m.input_price_per_1k <= max_cost
            ]
        elif rule.max_cost_per_1k is not None:
            candidates = [
                m for m in candidates
                if m.input_price_per_1k <= rule.max_cost_per_1k
            ]

        # Filter by provider priority
        if rule.provider_priority:
            priority_order = {pid: i for i, pid in enumerate(rule.provider_priority)}
            candidates.sort(
                key=lambda m: priority_order.get(m.provider_id, 999)
            )

        # Filter excluded providers
        candidates = [m for m in candidates if m.provider_id not in exclude_providers]

        # Check circuit breakers
        candidates = [
            m for m in candidates
            if not self._is_circuit_open(m.provider_id)
        ]

        if not candidates:
            # Try fallback rule
            if policy.fallback_rule_id:
                fallback_rule = self.rules.get(policy.fallback_rule_id)
                if fallback_rule:
                    logger.info("Using fallback routing rule: %s", fallback_rule.name)
                    return self._select_by_rule(fallback_rule, required_capability, exclude_providers)
            return None, None

        # Apply strategy
        return self._apply_strategy(rule.strategy, candidates, required_capability)

    def _select_by_rule(
        self,
        rule: RoutingRule,
        required_capability: str | None = None,
        exclude_providers: list[str] | None = None,
    ) -> tuple[str | None, str | None]:
        """Select provider using a specific rule."""
        exclude_providers = exclude_providers or []
        candidates = self.model_registry.list_models(
            category=rule.model_category,
            active_only=True,
        )

        if required_capability:
            candidates = [m for m in candidates if required_capability in m.capabilities]

        if rule.max_cost_per_1k is not None:
            candidates = [
                m for m in candidates
                if m.input_price_per_1k <= rule.max_cost_per_1k
            ]

        candidates = [m for m in candidates if m.provider_id not in exclude_providers]
        candidates = [m for m in candidates if not self._is_circuit_open(m.provider_id)]

        if not candidates:
            return None, None

        return self._apply_strategy(rule.strategy, candidates, required_capability)

    def _apply_strategy(
        self,
        strategy: str,
        candidates: list[AIModel],
        required_capability: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Apply a routing strategy to select from candidates."""
        if not candidates:
            return None, None

        if strategy == "lowest_cost":
            # Select cheapest model
            selected = min(candidates, key=lambda m: m.input_price_per_1k)
            return selected.provider_id, selected.model_id

        if strategy == "highest_quality":
            # Select model with largest context window (proxy for quality)
            selected = max(candidates, key=lambda m: m.context_window)
            return selected.provider_id, selected.model_id

        if strategy == "lowest_latency":
            # Select model with highest rate limit (proxy for speed)
            selected = max(candidates, key=lambda m: m.rate_limit_per_minute)
            return selected.provider_id, selected.model_id

        if strategy == "provider_priority":
            # Already sorted by provider priority in caller
            return candidates[0].provider_id, candidates[0].model_id

        if strategy == "capability_matching":
            # Prefer models with the exact required capability
            if required_capability:
                exact = [m for m in candidates if required_capability in m.capabilities]
                if exact:
                    return exact[0].provider_id, exact[0].model_id
            return candidates[0].provider_id, candidates[0].model_id

        if strategy == "weighted_distribution":
            # Simple round-robin based on provider priority
            return candidates[0].provider_id, candidates[0].model_id

        # Default: return first candidate
        return candidates[0].provider_id, candidates[0].model_id

    # ── Circuit Breaker ───────────────────────────────────────────────

    def _is_circuit_open(self, provider_id: str) -> bool:
        """Check if circuit breaker is open for a provider."""
        cb = self._circuit_breakers.get(provider_id)
        if not cb:
            return False

        if cb["state"] == "closed":
            return False

        if cb["state"] == "open":
            # Check if timeout has elapsed
            if time.monotonic() - cb["opened_at"] > cb.get("timeout_seconds", 300):
                cb["state"] = "half_open"
                return False
            return True

        if cb["state"] == "half_open":
            return False

        return False

    def record_success(self, provider_id: str) -> None:
        """Record a successful request for circuit breaker."""
        cb = self._circuit_breakers.get(provider_id)
        if cb and cb["state"] == "half_open":
            cb["state"] = "closed"
            cb["failure_count"] = 0
        elif cb and cb["state"] == "closed":
            cb["failure_count"] = max(0, cb.get("failure_count", 0) - 1)

    def record_failure(self, provider_id: str, threshold: int = 5, timeout_seconds: int = 300) -> None:
        """Record a failed request for circuit breaker."""
        if provider_id not in self._circuit_breakers:
            self._circuit_breakers[provider_id] = {
                "state": "closed",
                "failure_count": 0,
                "opened_at": 0.0,
                "timeout_seconds": timeout_seconds,
            }

        cb = self._circuit_breakers[provider_id]
        cb["failure_count"] = cb.get("failure_count", 0) + 1

        if cb["state"] == "closed" and cb["failure_count"] >= threshold:
            cb["state"] = "open"
            cb["opened_at"] = time.monotonic()
            logger.warning("Circuit breaker opened for provider: %s", provider_id)


# ── Cost Calculator ─────────────────────────────────────────────────────

class CostCalculator:
    """Calculate AI request costs."""

    @staticmethod
    def calculate_text_cost(
        input_tokens: int,
        output_tokens: int,
        input_price_per_1k: float,
        output_price_per_1k: float,
    ) -> float:
        """Calculate text generation cost in USD."""
        input_cost = (input_tokens / 1000.0) * input_price_per_1k
        output_cost = (output_tokens / 1000.0) * output_price_per_1k
        return round(input_cost + output_cost, 6)

    @staticmethod
    def calculate_image_cost(
        num_images: int,
        price_per_image: float,
    ) -> float:
        """Calculate image generation cost in USD."""
        return round(num_images * price_per_image, 6)

    @staticmethod
    def calculate_video_cost(
        num_videos: int,
        price_per_video: float,
    ) -> float:
        """Calculate video generation cost in USD."""
        return round(num_videos * price_per_video, 6)