"""OmniRoute AI client wrapper.

Encapsulates all HTTP communication with the OmniRoute backend
(``/models`` health check, ``/chat/completions`` AI analysis,
and ``/optimize/seo`` SEO optimisation).  Uses ``httpx.AsyncClient``
with strict timeouts and structured logging.

Typical usage::

    from services.omniroute_client import OmniRouteClient

    client = OmniRouteClient(
        base_url="http://localhost:4000/v1",
        api_key="sk-or-...",
    )
    healthy = await client.check_health()
    """
from __future__ import annotations

import json
import logging
from typing import Dict, Any, List, Optional

import httpx

logger = logging.getLogger(__name__)


class OmniRouteClient:
    """Client for the OmniRoute API v1."""

    def __init__(self, base_url: str, api_key: str) -> None:
        """Initialise the client.

        :param base_url: OmniRoute server base URL (e.g.
            ``http://localhost:4000/v1``).  Trailing slashes are auto-stripped.
        :param api_key: Bearer token for ``Authorization: Bearer <key>``.
        """
        self.base_url: str = base_url.rstrip("/")
        self.api_key: str = api_key
        self.headers: Dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def check_health(self) -> bool:
        """Check if the OmniRoute server is reachable via ``/models``.

        :return: ``True`` when the server responds with ``200 OK``,
            otherwise ``False``.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{self.base_url}/models",
                    headers=self.headers,
                )
                return response.status_code == 200
        except httpx.HTTPError as exc:
            logger.error(f"OmniRoute health check HTTP error: {exc}")
            return False
        except Exception as exc:  # pragma: no cover
            logger.exception(f"OmniRoute health check unexpected error: {exc}")
            return False

    async def get_ai_trend_analysis(
        self, prompt: str, model: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Send a prompt to OmniRoute for trend/JSON analysis.

        The OmniRoute server is expected to return a JSON-object response.
        If the response cannot be parsed, ``None`` is returned.

        :param prompt: The user prompt to send to the AI.
        :param model: Optional model override; falls back to the client's
            default configuration if ``None``.
        :return: Parsed JSON dict, or ``None`` on failure.
        """
        target_model: str = model or "omniroute-v1"
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": target_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a fashion trend analyst. "
                    "Always output valid JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url, headers=self.headers, json=payload
                )
                response.raise_for_status()
                data = response.json()
                content_str = data["choices"][0]["message"]["content"]
                return json.loads(content_str)
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"OmniRoute AI analysis HTTP error {exc.response.status_code}: {exc}"
            )
            return None
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            logger.error(
                f"OmniRoute AI analysis parse error: {exc}"
            )
            return None
        except Exception as exc:  # pragma: no cover
            logger.exception(f"OmniRoute AI analysis unexpected error: {exc}")
            return None

    async def optimize_seo(
        self, product_data: Dict[str, str]
    ) -> Dict[str, str]:
        """Optimise SEO titles and descriptions for a product.

        :param product_data: Dictionary containing at minimum ``title`` and
            ``description`` keys.
        :return: Dict with ``title`` and ``description`` keys.  Falls back
            to the values supplied in ``product_data`` on failure.
        """
        prompt = (
            f"Optimise SEO for this product: {json.dumps(product_data)}. "
            "Provide 'title' and 'description'."
        )
        result = await self.get_ai_trend_analysis(prompt)
        if result and isinstance(result, dict):
            return {
                "title": result.get("title", product_data.get("title", "")),
                "description": result.get(
                    "description", product_data.get("description", "")
                ),
            }
        return {"title": product_data.get("title", ""),
                "description": product_data.get("description", "")}