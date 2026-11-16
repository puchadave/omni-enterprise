"""Trend engine for Omni-Core Middleware.

Autonomous weekly background task that:

1. Queries OmniRoute for current fashion trends.
2. Matches trends against local inventory.
3. Generates exactly 10 new trend-based outfit bundles (idempotent,
   i.e. re-running the job will upsert the same bundle data).

All database access goes through the ``get_connection()`` / ``close_connection()``
helpers from ``database.py``, which configure WAL mode and busy timeout
automatically.  The engine is designed to be called from the
``start_trend_scheduler`` helper or directly from a FastAPI background
task.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import List, Dict, Any, Optional

from omni_core.services.omniroute_client import OmniRouteClient

from database import get_connection, close_connection

logger = logging.getLogger(__name__)

CATEGORIES: list[str] = ["Jacke", "Hose", "Oberteil", "Kleid", "Schuhe", "Accessoire"]
SEASONS: list[str] = ["Frühling", "Sommer", "Herbst", "Winter"]

STYLES: list[str] = ["Streetwear", "Business-Casual", "Sportlich", "Elegant", "Boho", "Minimalistisch"]


class TrendEngine:
    """Execute the weekly trend-analysis and bundle-generation job."""

    def __init__(self, omniroute_client: OmniRouteClient) -> None:
        """Initialise the trend engine."""
        self.omniroute_client: OmniRouteClient = omniroute_client

    async def run_weekly_trend_job(self) -> Dict[str, Any]:
        """Execute the full weekly trend analysis and bundle generation.

        Returns a summary dict with the counts of trends found, bundles
        persisted, and any errors encountered.

        :return: Summary dict with keys::

            - ``trends_found``: ``int`` – number of trends returned from
              OmniRoute.
            - ``bundles_created``: ``int`` – number of bundles persisted
              to the database (always ``<= 10``).
            - ``errors``: ``list[str]`` – human-readable error messages
              (empty list if the job succeeded).
        """
        result: Dict[str, Any] = {
            "trends_found": 0,
            "bundles_created": 0,
            "errors": [],  # type: ignore[dict-key]
        }

        try:
            trends = await self._fetch_trends()
            if not trends:
                result["errors"].append("No trends returned from OmniRoute")
                return result

            result["trends_found"] = len(trends.get("trends", []))

            inventory = await self._load_inventory()
            if not inventory:
                result["errors"].append("Inventory empty - cannot build bundles")
                return result

            bundles = self._build_bundles(trends, inventory)
            for bundle in bundles:
                await self._persist_bundle(bundle)
                result["bundles_created"] += 1

            logger.info(f"Trend job complete: {result['bundles_created']} bundles created")

        except Exception as exc:  # pragma: no cover
            logger.exception(f"Trend job failed: {exc}")

        return result

    async def _fetch_trends(self) -> Optional[Dict[str, Any]]:
        """Query OmniRoute for current fashion trends.

        The prompt asks for exactly 5 trends in the defined schema.
        If the AI returns fewer, the engine pads the result set to reach
        the target of 10 bundles.

        :return: Parsed JSON dict from OmniRoute, or ``None`` on failure.
        """
        prompt = (
            "Analysiere aktuelle Fashion-Trends 2026. "
            "Gib eine JSON-Antwort mit folgendem Schema zurück: "
            r'{"trends": [{"name": "<Trend-Name>", "category": "<Jacke|Hose|Oberteil|Kleid|Schuhe|Accessoire>", '
            '"keywords": ["<...>"], "season": "<Frühling|Sommer|Herbst|Winter>", "style": "<Stil>"}]}. '
            "Liefere exakt 5 Trends."
        )
        return await self.omniroute_client.get_ai_trend_analysis(prompt)

    async def _load_inventory(self) -> List[Dict[str, Any]]:
        """Load all active non-bundle products with stock > 0.

        Returns a list of dicts keyed by the product row fields
        (``sku``, ``title``, ``category``, ``stock``).
        """
        products: List[Dict[str, Any]] = []
        async with get_connection() as db:
            async with db.execute(
                "SELECT sku, title, category, stock FROM products WHERE status = 'active' AND is_bundle = 0 AND stock > 0"
            ) as cursor:
                async for row in cursor:
                    products.append(dict(row))
        return products

    @staticmethod
    def _build_bundles(
        trends: Dict[str, Any],
        inventory: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Build up to 10 trend bundles from the given inventory.

        The algorithm:

        1. Extract the trend list from the AI response; if fewer than 5
           trends are returned, synthetic trends are generated from the
           constant lists ``STYLES``, ``CATEGORIES``, ``SEASONS``.
        2. For each trend, match inventory items whose ``category`` equals
           the trend's category and that have not already been used in a
           previous bundle.
        3. If not enough matching items are available, fall back to any
           unused product.
        4. Randomly select 2-4 components per bundle (currently 3 unless
           fewer are available).
        5. Assign a ``bundle_sku`` and ``title``.
        6. Stop after 10 bundles have been generated.

        The function is *deterministic* aside from the ``random.sample``
        calls, which are seeded by the global ``random`` state at the time
        of the call.

        :param trends: Dict as returned by ``_fetch_trends``, expected to
            contain a ``trends`` list.
        :param inventory: List of product dicts from ``_load_inventory``.
        :return: List of bundle dicts, each with keys::

            - ``bundle_sku``
            - ``component_skus`` (comma-separated string)
            - ``title``
            - ``trend``
            - ``style``
            - ``season``
        """
        trend_list: List[Dict[str, Any]] = trends.get("trends", [])
        if not trend_list:
            # Generate synthetic trends from the constant dictionaries
            trend_list = [
                {
                    "name": f"{style} Look",
                    "category": cat,
                    "keywords": [cat.lower()],
                    "season": season,
                    "style": style,
                }
                for style in STYLES[:5]
                for cat, season in [(CATEGORIES[0], SEASONS[0])]
            ][:5]

        bundles: List[Dict[str, Any]] = []
        used_skus: set[str] = set()
        bundle_count: int = 0

        for i, trend in enumerate(trend_list):
            if bundle_count >= 10:
                break

            category: str = trend.get("category", "Oberteil")

            # Match inventory by category, avoiding already-used SKUs
            matching = [
                p for p in inventory
                if p["category"] == category and p["sku"] not in used_skus
            ]

            # Fallback: any unused product if category match is insufficient
            if len(matching) < 2:
                matching = [
                    p for p in inventory if p["sku"] not in used_skus
                ]

            if len(matching) < 2:
                # Not enough products to form a bundle; skip this trend
                continue

            # Select 3 components (or fewer if remaining inventory is thin)
            n_components: int = min(3, len(matching))
            components = random.sample(matching, n_components)
            component_skus: List[str] = [c["sku"] for c in components]
            used_skus.update(component_skus)

            bundle_sku: str = f"TREND-{i + 1:03d}-{trend['name'][:8].upper().replace(' ', '-')}"
            bundles.append({
                "bundle_sku": bundle_sku,
                "component_skus": ",".join(component_skus),
                "title": f"{trend['name']} Outfit Bundle",
                "trend": trend.get("name", ""),
                "style": trend.get("style", ""),
                "season": trend.get("season", ""),
            })
            bundle_count += 1

        # Pad to exactly 10 if the AI returned fewer trends
        idx: int = 0
        while bundle_count < 10 and inventory:
            available = [
                p for p in inventory if p["sku"] not in used_skus
            ]
            if len(available) < 2:
                # Not enough remaining products to form another bundle
                break

            n_components = min(3, len(available))
            components = random.sample(available, n_components)
            component_skus: List[str] = [c["sku"] for c in components]
            used_skus.update(component_skus)

            bundle_sku: str = f"TREND-PAD-{bundle_count + 1:03d}"
            bundles.append({
                "bundle_sku": bundle_sku,
                "component_skus": ",".join(component_skus),
                "title": f"Curated Outfit Bundle #{bundle_count + 1}",
                "trend": "Generic",
                "style": "Mixed",
                "season": "All",
            })
            bundle_count += 1
            idx += 1

        return bundles[:10]

    async def _persist_bundle(self, bundle: Dict[str, Any]) -> None:
        """Insert or upsert a bundle and its parent product row.

        The operation is idempotent: running it multiple times with the
        same ``bundle_sku`` will update (not duplicate) the rows.

        Two tables are affected:

        1. ``products`` – parent product entry with ``is_bundle = 1``.
        2. ``bundles`` – the component mapping.

        :param bundle: Bundle dict as produced by ``_build_bundles``.
        """
        async with get_connection() as db:
            # Ensure the parent product exists; mark as bundle
            await db.execute(
                """
                INSERT INTO products (sku, title, category, supplier_price, stock, is_bundle, status)
                VALUES (?, ?, ?, 0, 0, 1, 'active')
                ON CONFLICT(sku) DO UPDATE
                    SET title = excluded.title,
                        is_bundle = 1,
                        status = 'active'
                """,
                (bundle["bundle_sku"], bundle["title"], "Bundle"),
            )
            # Upsert the bundle-to-components mapping
            await db.execute(
                """
                INSERT INTO bundles (bundle_sku, component_skus, title)
                VALUES (?, ?, ?)
                ON CONFLICT(bundle_sku) DO UPDATE
                    SET component_skus = excluded.component_skus,
                        title = excluded.title
                """,
                (bundle["bundle_sku"], bundle["component_skus"], bundle["title"]),
            )
            await db.commit()


async def start_trend_scheduler(
    omniroute_client: OmniRouteClient,
    interval_seconds: int = 604800,  # 7 days
) -> None:
    """Run the trend job on a weekly interval (default 7 days).

    This is an infinite loop that should be started as an asyncio task:

    .. code-block:: python

        from fastapi import FastAPI
        from database import init_db
        from omni_core.services.omniroute_client import OmniRouteClient
        from services.trend_engine import start_trend_scheduler

        app = FastAPI(title="Omni-Core")

        @app.on_event("startup")
        async def startup():
            await init_db()
            client = OmniRouteClient(
                base_url="http://localhost:4000/v1",
                api_key="sk-or-...",
            )
            asyncio.create_task(start_trend_scheduler(client))

    :param omniroute_client: Configured ``OmniRouteClient``.
    :param interval_seconds: Seconds between job iterations.  Default 604800
        (7 days).
    """
    engine = TrendEngine(omniroute_client)
    while True:
        try:
            await engine.run_weekly_trend_job()
        except Exception as exc:  # pragma: no cover
            logger.exception(f"Trend scheduler iteration failed: {exc}")
        await asyncio.sleep(interval_seconds)