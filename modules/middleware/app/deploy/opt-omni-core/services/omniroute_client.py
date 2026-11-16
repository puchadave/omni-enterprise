"""OmniRoute AI Gateway client."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


class OmniRouteClient:
    """Thin async client for the OmniRoute OpenAI-compatible gateway."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url: str = base_url.rstrip("/")
        self.api_key: str = api_key
        self.headers: Dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def check_health(self) -> bool:
        """Check OmniRoute availability via the OpenAI-compatible /models endpoint."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/models", headers=self.headers)
                return response.status_code == 200
        except Exception as exc:
            logger.error("OmniRoute health check failed: %s", exc)
            return False

    async def chat_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] = "auto",
        timeout: float = 60.0,
    ) -> dict[str, Any]:
        """Call the OpenAI-compatible chat completions endpoint with optional tools."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "tool_choice": tool_choice,
        }
        if tools:
            payload["tools"] = tools
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self.headers,
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                message = data.get("choices", [{}])[0].get("message", {})
                tool_calls: list[dict[str, Any]] = []
                for call in message.get("tool_calls", []) or []:
                    function = call.get("function", {})
                    tool_calls.append({
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "arguments": function.get("arguments", "{}"),
                    })
                return {
                    "content": message.get("content", ""),
                    "tool_calls": tool_calls,
                    "raw": data,
                }
        except Exception as exc:
            logger.error("OmniRoute chat completion failed: %s", exc)
            raise

    async def get_ai_trend_analysis(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Request a structured JSON fashion trend analysis from OmniRoute."""
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": "gemini-2.5-flash",
            "messages": [
                {"role": "system", "content": "You are a fashion trend analyst. Always output valid JSON."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=self.headers, json=payload)
                response.raise_for_status()
                data = response.json()
                content_str = data["choices"][0]["message"]["content"]
                return json.loads(content_str)
        except Exception as exc:
            logger.error("OmniRoute AI analysis failed: %s", exc)
            return None

    async def optimize_seo(self, product_data: Dict[str, Any]) -> Dict[str, str]:
        """Optimize SEO title/description via OmniRoute."""
        prompt = f"Optimize SEO for this product: {json.dumps(product_data)}. Provide 'title' and 'description'."
        result = await self.get_ai_trend_analysis(prompt)
        if result:
            return {
                "title": result.get("title", product_data.get("title", "")),
                "description": result.get("description", product_data.get("description", "")),
            }
        return {"title": product_data.get("title", ""), "description": product_data.get("description", "")}
