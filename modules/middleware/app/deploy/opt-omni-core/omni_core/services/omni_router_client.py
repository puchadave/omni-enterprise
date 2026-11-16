"""Async Omni-Route Gateway Client for Omni-Core E-Commerce Middleware.

Provides a robust client for communicating with the OpenAI-compatible
Omni-Route Gateway service (default: http://localhost:20128/v1).

Features:
- httpx.AsyncClient for all HTTP requests
- Automatic fallback on gateway failures (returns graceful defaults)
- Chat completion (text generation)
- Structured JSON response parsing
- Embedding generation
- Health check endpoint
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Union

import httpx

logger = logging.getLogger(__name__)

# Default Omni-Route Gateway URL (can be overridden via environment variable)
OMNI_ROUTE_URL: str = "http://localhost:20128/v1"
# Allow override via environment variable
import os
OMNI_ROUTE_URL = os.environ.get("OMNI_ROUTE_URL", OMNI_ROUTE_URL)
OMNI_ROUTE_API_KEY = os.environ.get("OMNI_ROUTE_API_KEY")

GATEWAY_HEADERS: dict[str, str] = {
    "Content-Type": "application/json",
}


class OmniRouterClient:
    """
    Async client for the Omni-Route Gateway service.

    Communicates with the gateway via /v1/... endpoints
    (OpenAI-compatible) for text generation, structured responses,
    and embeddings. Designed for graceful degradation when the
    gateway is unavailable.
    """

    def __init__(self, base_url: str = OMNI_ROUTE_URL, api_key: str | None = None):
        """
        Initialize the OmniRouterClient.

        :param base_url: Base URL of the Omni-Route Gateway
            (default: http://localhost:20128/v1)
        :param api_key: API key for authentication (optional;
            can be set via OMNI_ROUTE_API_KEY env var)
        """
        self.base_url: str = base_url.rstrip("/")
        self.api_key: str | None = api_key or OMNI_ROUTE_API_KEY
        self.client: httpx.AsyncClient = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        endpoint: str,
        json_payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Make an HTTP request to the Omni-Route Gateway with error handling
        and automatic fallback to defaults on failure.

        :param method: HTTP method ("GET", "POST", etc.)
        :param endpoint: Gateway endpoint path (e.g. "/chat/completions")
        :param json_payload: JSON payload for the request
        :param headers: Additional headers
        :return: Parsed JSON response (or default structure on failure)
        """
        url: str = f"{self.base_url}{endpoint}"
        request_headers: dict[str, str] = dict(GATEWAY_HEADERS)
        if headers:
            request_headers.update(headers)
        if self.api_key:
            request_headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            response: httpx.Response = await self.client.request(
                method, url, json=json_payload, headers=request_headers
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"Omni-Route Gateway HTTP error {e.response.status_code}: {e}")
            return self._default_failure_response()
        except httpx.RequestError as e:
            logger.error(f"Omni-Route Gateway connection error: {e}")
            return self._default_failure_response()
        except Exception as e:  # pragma: no cover
            logger.exception(f"Unexpected error communicating with Omni-Route Gateway: {e}")
            return self._default_failure_response()

    def _default_failure_response(self) -> dict[str, Any]:
        """Return a graceful default response when the gateway is unavailable."""
        return {
            "choices": [
                {
                    "message": {
                        "content": "{}",
                        "role": "assistant",
                    }
                }
            ]
        }

    # -------------------------------------------------------------------------
    # Chat Completion (Text Generation)
    # -------------------------------------------------------------------------

    async def complete_chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        fallback_to_default: bool = True,
    ) -> dict[str, Any]:
        """
        Send a chat completion request to the Omni-Route Gateway.

        :param messages: List of message dicts with 'role' and 'content'
        :param model: Model name (optional; uses gateway default)
        :param temperature: Sampling temperature (0.0-1.0)
        :param fallback_to_default: If True, returns default on failure
        :return: Response dictionary (or default structure on failure)
        """
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
        }
        if model:
            payload["model"] = model

        response: dict[str, Any] = await self._request(
            "POST", "/chat/completions", json_payload=payload
        )

        if fallback_to_default and not response.get("choices"):
            return self._default_failure_response()

        return response

    # -------------------------------------------------------------------------
    # Structured JSON Response
    # -------------------------------------------------------------------------

    async def get_structured_response(
        self,
        system_prompt: str,
        user_prompt: str,
        response_format: dict[str, str] | None = None,
        model: str | None = None,
        fallback_to_default: bool = True,
    ) -> dict[str, Any]:
        """
        Send a request and expect a structured JSON response.

        Useful for generating formatted output like product descriptions,
        analysis results, etc.

        :param system_prompt: System prompt setting the behavior
        :param user_prompt: User request/prompt
        :param response_format: Expected JSON structure hint
        :param model: Model name (optional)
        :param fallback_to_default: If True, returns default on failure
        :return: Parsed JSON dictionary (or default on failure)
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        response: dict[str, Any] = await self._request(
            "POST",
            "/chat/completions",
            json_payload={
                "messages": messages,
                "response_format": response_format or {"type": "json_object"},
            },
        )

        if fallback_to_default and not response.get("choices"):
            return self._default_failure_response()

        try:
            choice = response.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content", "{}")
            parsed: dict[str, Any] = json.loads(content)
            return parsed
        except (json.JSONDecodeError, KeyError, TypeError, IndexError) as e:
            logger.warning(f"Failed to parse structured response: {e}")
            if fallback_to_default:
                return self._default_failure_response()
            return {}

    # -------------------------------------------------------------------------
    # Embedding Generation
    # -------------------------------------------------------------------------

    async def embed_text(self, text: str, model: str | None = None) -> list[float] | None:
        """
        Generate embeddings for the given text.

        :param text: Text to embed
        :param model: Embedding model name (optional)
        :return: Embedding vector, or None on failure
        """
        payload: dict[str, Any] = {"input": text}
        if model:
            payload["model"] = model

        response: dict[str, Any] = await self._request("POST", "/embeddings", json_payload=payload)

        try:
            data = response.get("data", [])
            if data and len(data) > 0:
                return data[0].get("embedding")
        except (KeyError, TypeError, IndexError) as e:
            logger.warning(f"Failed to parse embedding response: {e}")

        return None

    # -------------------------------------------------------------------------
    # Health Check
    # -------------------------------------------------------------------------

    async def health_check(self) -> bool:
        """
        Check if the Omni-Route Gateway is reachable.

        :return: True if the gateway responds, False otherwise
        """
        try:
            response = await self._request("GET", "/health")
            return response.get("status") == "ok"
        except Exception:
            return False