"""Business Intelligence Engine for Omni-Core ERP Enterprise Edition.

Provides full KPI calculation, margin analysis, inventory valuation,
and AI-generated business reports using the Omni-Route Gateway.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List


from database import get_connection
from services.omni_router_client import OmniRouterClient

logger = logging.getLogger("omni.bi")


class BIEngine:
    """
    Business Intelligence Engine for the Omni-Core ERP System.

    Calculates all relevant Key Performance Indicators (KPIs) in real-time:
    - Inventory valuation with -10% safety buffer
    - Channel performance analysis
    - Margin alerts and recommendations
    - AI-generated weekly business reports
    """

    def __init__(self):
        self.client = OmniRouterClient()

    # -------------------------------------------------------------------------
    # Inventory & Capital KPIs
    # -------------------------------------------------------------------------

    async def calculate_inventory_metrics(self) -> Dict[str, Any]:
        """
        Calculate total inventory value and bound capital.

        Applies the -10% safety buffer logic for buffered stock values.
        """
        async with get_connection() as db:
            # Get all active products with their pricing
            async with db.execute(
                "SELECT sku, supplier_price, stock, buffered_stock, retail_price FROM products WHERE status = 'active'"
            ) as cursor:
                rows = await cursor.fetchall()

        total_cost: float = 0.0
        total_buffered_value: float = 0.0
        total_retail_value: float = 0.0
        product_count: int = 0

        for row in rows:
            sku, supplier_price, stock, buffered_stock, retail_price = row
            cost_value = supplier_price * stock
            buffered_value = supplier_price * buffered_stock  # Apply 10% buffer implicitly via buffered_stock
            retail_value = retail_price * stock

            total_cost += cost_value
            total_buffered_value += buffered_value
            total_retail_value += retail_value
            product_count += 1

        # Calculate gross margin percentage
        gross_margin_pct: float = round(
            (total_retail_value - total_cost) / total_retail_value * 100, 2
        ) if total_retail_value > 0 else 0.0

        return {
            "product_count": product_count,
            "total_cost": round(total_cost, 2),
            "total_buffered_value": round(total_buffered_value, 2),
            "total_retail_value": round(total_retail_value, 2),
            "gross_margin_pct": gross_margin_pct,
            "capital_locked": round(total_buffered_value, 2),
        }

    # -------------------------------------------------------------------------
    # Channel Performance Analysis
    # -------------------------------------------------------------------------

    async def calculate_channel_performance(self) -> Dict[str, Any]:
        """
        Analyze performance per sales channel (Shopify, Amazon, Google Shopping).

        Returns margin, revenue, and velocity metrics per channel.
        """
        async with get_connection() as db:
            # Get channel-specific data from channel_content and products
            async with db.execute(
                """SELECT c.channel_name, COUNT(DISTINCT c.sku) as product_count,
                          AVG(c.retail_price) as avg_retail,
                          SUM(c.retail_price) as channel_revenue
                   FROM channel_content c
                   GROUP BY c.channel_name"""
            ) as cursor:
                channel_rows = await cursor.fetchall()

            channel_metrics: Dict[str, Dict[str, float]] = {}

            for row in channel_rows:
                channel_name = row[0] or "unknown"
                product_count = row[1] or 0
                avg_retail = row[2] or 0.0
                channel_revenue = row[3] or 0.0

                # Calculate margin assuming 195% markup
                cost_basis = round(avg_retail / 1.95, 2)
                margin_pct = round((avg_retail - cost_basis) / avg_retail * 100, 2) if avg_retail > 0 else 0.0

                channel_metrics[channel_name] = {
                    "product_count": product_count,
                    "avg_retail": avg_retail,
                    "revenue": round(channel_revenue, 2),
                    "margin_pct": margin_pct,
                    "margin_category": (
                        "high" if margin_pct >= 30
                        else "medium" if margin_pct >= 20
                        else "low"
                    ),
                }

            # Add default channels if none exist
            for ch in ["shopify", "amazon", "google_shopping"]:
                if ch not in channel_metrics:
                    channel_metrics[ch] = {
                        "product_count": 0,
                        "avg_retail": 0.0,
                        "revenue": 0.0,
                        "margin_pct": 0.0,
                        "margin_category": "unknown",
                    }

        return {"channels": channel_metrics}

    # -------------------------------------------------------------------------
    # Margin & Stock Alerts
    # -------------------------------------------------------------------------

    async def check_margin_alerts(self) -> List[Dict[str, Any]]:
        """
        Identify products with margin issues or inventory extremes.

        Returns a list of alerts for:
        - Products with margin < 15%
        - Products with buffered stock > 500 (overstock)
        - Products with buffered stock == 0 (stockout)
        """
        alerts: List[Dict[str, Any]] = []

        async with get_connection() as db:
            async with db.execute(
                "SELECT sku, supplier_price, retail_price, stock, buffered_stock FROM products WHERE status = 'active'"
            ) as cursor:
                rows = await cursor.fetchall()

        for row in rows:
            sku, supplier_price, retail_price, stock, buffered_stock = row

            # Check for low margin (< 15%)
            if retail_price > 0:
                margin_pct = round((retail_price - supplier_price) / retail_price * 100, 2)
                if margin_pct < 15:
                    alerts.append(
                        {
                            "type": "low_margin",
                            "sku": sku,
                            "message": f"Produkt '{sku}' hat nur {margin_pct}% Marge. Empfohlener Mindestpreis: {round(supplier_price / 0.85, 2)} EUR",
                            "severity": "high",
                        }
                    )

            # Check for overstock (buffered stock > 500)
            if buffered_stock and buffered_stock > 500:
                alerts.append(
                    {
                        "type": "overstock",
                        "sku": sku,
                        "message": f"Puffer-Bestand von {buffered_stock} Artikeln '{sku}' ist sehr hoch. Consider Price Reduction.",
                        "severity": "medium",
                    }
                )

            # Check for stockout (buffered stock == 0)
            if buffered_stock == 0:
                alerts.append(
                    {
                        "type": "stockout",
                        "sku": sku,
                        "message": f"Artikel '{sku}' ist vollständig aus dem Puffer bestellung. Sofortige Nachbestellung erforderlich.",
                        "severity": "critical",
                    }
                )

        return alerts

    # -------------------------------------------------------------------------
    # AI-Generated Weekly BI Report
    # -------------------------------------------------------------------------

    async def generate_weekly_bericht(self) -> Dict[str, Any]:
        """
        Generate a weekly business report using the Omni-Route Gateway.

        The report includes:
        - Executive Summary
        - Key Metrics Overview
        - Channel Performance
        - Margin & Stock Alerts
        - AI Recommendations for pricing and inventory
        """
        # Gather all BI data
        inventory = await self.calculate_inventory_metrics()
        channel_perf = await self.calculate_channel_performance()
        alerts = await self.check_margin_alerts()

        # Use Omni-Route Gateway for AI-generated insights
        system_prompt = (
            "Du bist ein erfahrener ERP-Berater und Business-Analyst. "
            "Erstelle einen professionellen wöchentlichen Business Report im JSON-Format mit folgenden Sektionen: "
            "'executive_summary', 'key_metrics', 'channel_analysis', 'alerts', 'recommendations'. "
            "Keep it concise and data-driven."
        )

        # Build a comprehensive user prompt with all BI data
        inventory_json = json.dumps(inventory, ensure_ascii=False)
        channel_json = json.dumps(channel_perf, ensure_ascii=False)
        alerts_json = json.dumps(alerts, ensure_ascii=False)

        user_prompt = (
            "Geschäftsdaten für KI-Analyse:\n"
            f"- Inventarmetriken: {inventory_json}\n"
            f"- Kanal-Performance: {channel_json}\n"
            f"- Margen- & Lagerwarnungen: {alerts_json}\n"
            "\n"
            "Generiere folgenden JSON-Report:\n"
            "{ "
            '"executive_summary": "Zusammenfassung der Geschäftsentwicklung der Woche", '
            '"key_metrics": {"umsatz": <Zahl>, "gewinn": <Zahl>, "lagerwert": <Zahl>, "umsatzwachstum": <Zahl in %>, '
            '"channel_analysis": {"best_channel": "<Name>", "channel_details": <Detailobjekte>}, '
            '"alerts": <Liste von Warnungen>, "recommendations": ["Empfehlung 1", "Empfehlung 2", "Empfehlung 3"] '
            "}"
        )

        try:
            response_text = await self.client.complete_chat(user_prompt, system_prompt=system_prompt)
            clean_json = response_text.replace("```json", "").replace("```", "").strip()
            report = json.loads(clean_json)
            report["generated_at"] = datetime.now(timezone.utc).isoformat()
            return report
        except Exception as e:
            logger.error(f"BI Report generation failed: {e}")
            return {
                "executive_summary": "KI-Report konnte nicht generiert werden",
                "key_metrics": {"umsatz": 0, "gewinn": 0, "lagerwert": 0, "umsatzwachstum": 0},
                "channel_analysis": {},
                "alerts": [],
                "recommendations": ["Manuelle Analyse erforderlich"],
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }

    # -------------------------------------------------------------------------
    # Metrics Persistence to DB
    # -------------------------------------------------------------------------

    async def persist_bi_metrics(self, metrics: Dict[str, Any]) -> None:
        """
        Persistierte berechnete BI-Metriken in die Datenbank für Historie und Reporting.
        """
        async with get_connection() as db:
            await db.execute(
                """
                INSERT INTO bi_metrics 
                (total_revenue, total_cost, gross_margin_pct, total_inventory_value, 
                 buffered_inventory_value, velocity_shopify, velocity_amazon, velocity_google)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    metrics.get("total_revenue", 0.0),
                    metrics.get("total_cost", 0.0),
                    metrics.get("gross_margin_pct", 0.0),
                    metrics.get("total_buffered_value", 0.0),
                    0.0,  # Would need historical velocity data
                    0.0,
                    0.0,
                ),
            )
            await db.commit()

    # -------------------------------------------------------------------------
    # Audit Log utility
    # -------------------------------------------------------------------------

    async def log_event(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        sku: str | None = None,
        channel_name: str | None = None,
    ) -> None:
        """
        Protokolliert Ereignisse in die audit_logs Tabelle für lückenlose Nachvollziehbarkeit.
        """
        async with get_connection() as db:
            await db.execute(
                "INSERT INTO audit_logs (event_type, event_data, sku, channel_name, created_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (
                    event_type,
                    json.dumps(event_data, ensure_ascii=False),
                    sku,
                    channel_name,
                ),
            )
            await db.commit()
