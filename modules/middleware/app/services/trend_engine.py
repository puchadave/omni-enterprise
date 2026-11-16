"""Weekly AI trend analysis and safe bundle generation."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from database import list_products, upsert_bundle, write_log
from services.omniroute_client import OmniRouteClient

logger = logging.getLogger(__name__)


class TrendEngine:
    """Generate up to ten inventory-backed outfits from OmniRoute trend output."""

    def __init__(self, client: OmniRouteClient) -> None:
        self.client = client

    async def run_weekly_trend_job(self) -> dict[str, Any]:
        """Fetch trends, select stocked components, and create safe bundle definitions."""
        result: dict[str, Any] = {"trends_found": 0, "bundles_created": 0, "errors": []}
        trends = await self._fetch_trends()
        if trends is None:
            result["errors"].append("OmniRoute returned no usable trend data.")
            return result
        raw_trends = trends.get("trends", [])
        if not isinstance(raw_trends, list):
            result["errors"].append("OmniRoute trend response has an invalid schema.")
            return result
        result["trends_found"] = len(raw_trends)

        inventory = [
            item for item in await list_products(500)
            if not item["is_bundle"] and item["status"] == "active" and int(item["stock"]) > 0
        ]
        if len(inventory) < 2:
            result["errors"].append("At least two stocked single products are required for a bundle.")
            return result

        used_skus: set[str] = set()
        for index, trend in enumerate(raw_trends[:10], start=1):
            if not isinstance(trend, dict):
                continue
            category = str(trend.get("category", ""))
            candidates = [
                item for item in inventory
                if str(item["sku"]) not in used_skus and (not category or item["category"] == category)
            ]
            if len(candidates) < 2:
                candidates = [item for item in inventory if str(item["sku"]) not in used_skus]
            if len(candidates) < 2:
                break

            components = random.sample(candidates, k=min(3, len(candidates)))
            component_skus = [str(item["sku"]) for item in components]
            used_skus.update(component_skus)
            trend_name = str(trend.get("name", f"Trend {index}"))[:80]
            safe_name = "".join(char if char.isalnum() else "-" for char in trend_name.upper()).strip("-")[:32]
            await upsert_bundle(
                bundle_sku=f"TREND-{index:03d}-{safe_name or 'OUTFIT'}",
                title=f"{trend_name} Outfit Bundle",
                component_skus=component_skus,
            )
            result["bundles_created"] += 1

        await write_log(
            "INFO",
            "trend_job_completed",
            f"Created {result['bundles_created']} inventory-backed trend bundles.",
            {"trends_found": result["trends_found"]},
        )
        return result

    async def _fetch_trends(self) -> dict[str, Any] | None:
        """Request a constrained structured trend response from OmniRoute."""
        prompt = (
            "Return JSON only: {\"trends\":[{\"name\":string,\"category\":string}]}. "
            "Provide at most ten current fashion trends. Categories should match available product groups."
        )
        return await self.client.get_ai_trend_analysis(prompt)


async def start_trend_scheduler(client: OmniRouteClient, interval_seconds: int = 604800) -> None:
    """Run the trend job on the requested interval without blocking the event loop."""
    engine = TrendEngine(client)
    while True:
        try:
            await engine.run_weekly_trend_job()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduled trend job failed")
        await asyncio.sleep(interval_seconds)
