"""Sales Automation Engine for Omni-Core ERP & BI Enterprise Edition.

Provides product intelligence, conversion intelligence, and smart pricing
with 10% safety margin and configurable rule engine.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("omni.sales")


def calculate_sales_velocity(
    orders: List[Dict[str, Any]],
    product_sku: str,
    days: int = 30,
) -> Dict[str, Any]:
    """Calculate sales velocity for a product."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    relevant = [
        o for o in orders
        if o.get("sku") == product_sku
        and o.get("created_at", "") >= cutoff.isoformat()
    ]
    total_quantity = sum(o.get("quantity", 0) or 0 for o in relevant)
    order_count = len(relevant)
    avg_quantity = total_quantity / order_count if order_count else 0
    daily_avg = total_quantity / days if days else 0
    recent_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    prev_cutoff = datetime.now(timezone.utc) - timedelta(days=14)
    recent = [
        o for o in orders
        if o.get("sku") == product_sku
        and recent_cutoff.isoformat() <= o.get("created_at", "") < datetime.now(timezone.utc).isoformat()
    ]
    prev = [
        o for o in orders
        if o.get("sku") == product_sku
        and prev_cutoff.isoformat() <= o.get("created_at", "") < recent_cutoff.isoformat()
    ]
    recent_qty = sum(o.get("quantity", 0) or 0 for o in recent)
    prev_qty = sum(o.get("quantity", 0) or 0 for o in prev)
    trend = "stable"
    if prev_qty > 0:
        change_pct = (recent_qty - prev_qty) / prev_qty * 100
        if change_pct > 10:
            trend = "growing"
        elif change_pct < -10:
            trend = "declining"
    return {
        "product_sku": product_sku,
        "daily_average": daily_avg,
        "order_count_30d": order_count,
        "total_quantity_30d": total_quantity,
        "avg_quantity_per_order": avg_quantity,
        "recent_7d_quantity": recent_qty,
        "prev_7d_quantity": prev_qty,
        "velocity_trend": trend,
        "period_days": days,
    }


def calculate_abc_classification(
    orders: List[Dict[str, Any]],
    product_sku: str,
    days: int = 365,
) -> Dict[str, Any]:
    """Classify product as A, B, or C based on sales velocity (Pareto principle)."""
    vel = calculate_sales_velocity(orders, product_sku, days=days)
    total_qty = vel["total_quantity_30d"]
    # Simplified: A = top 20%, B = next 30%, C = rest
    if total_qty == 0:
        classification = "C"
        velocity_score = 0
    elif total_qty >= 100:
        classification = "A"
        velocity_score = 100
    elif total_qty >= 30:
        classification = "B"
        velocity_score = 60
    else:
        classification = "C"
        velocity_score = 20

    return {
        "product_sku": product_sku,
        "classification": classification,
        "velocity_score": velocity_score,
        "total_quantity_30d": total_qty,
        "classification_days": days,
    }


def detect_slow_movers(
    orders: List[Dict[str, Any]],
    product_sku: str,
    days: int = 90,
    threshold_qty: int = 5,
) -> Dict[str, Any]:
    """Detect products with low sales velocity (potential dead stock)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    relevant = [
        o for o in orders
        if o.get("sku") == product_sku
        and o.get("created_at", "") >= cutoff.isoformat()
    ]
    total_qty = sum(o.get("quantity", 0) or 0 for o in relevant)
    order_count = len(relevant)
    is_slow_mover = total_qty <= threshold_qty

    return {
        "product_sku": product_sku,
        "is_slow_mover": is_slow_mover,
        "total_quantity_90d": total_qty,
        "order_count_90d": order_count,
        "threshold_qty": threshold_qty,
        "days_analyzed": days,
    }


def calculate_smart_price(
    retail_price: float,
    cost_price: float,
    platform_fee_percent: float = 5.0,
    shipping_cost: float = 0.0,
    safety_margin_percent: float = 10.0,
) -> Dict[str, Any]:
    """Calculate smart price with 10% safety margin.

    The 10% safety margin calculates the MINIMUM achievable price.
    It does NOT simply deduct 10% from retail price.
    Instead it computes the lowest price that still achieves a positive margin
    after all costs (purchase + platform fees + shipping).

    Args:
        retail_price: Original retail price
        cost_price: Base cost price (purchase price)
        platform_fee_percent: Platform fee percentage (default 5%)
        shipping_cost: Shipping cost per unit
        safety_margin_percent: Safety margin percentage (default 10%)

    Returns:
        Dict with calculated prices and margin information
    """
    # Calculate costs per unit
    platform_fee_factor = platform_fee_percent / 100.0
    total_cost_per_unit = cost_price + (retail_price * platform_fee_factor) + shipping_cost

    # Calculate minimum price with safety margin
    # The safety margin ensures we don't price below cost + margin
    min_price_with_margin = total_cost_per_unit * (1 + safety_margin_percent / 100.0)

    # Calculate gross margin at retail
    gross_margin_at_retail = retail_price - total_cost_per_unit
    gross_margin_pct_at_retail = (gross_margin_at_retail / retail_price * 100) if retail_price > 0 else 0

    # Calculate margin percentage at minimum price
    margin_at_min_price = min_price_with_margin - total_cost_per_unit
    margin_pct_at_min_price = (margin_at_min_price / min_price_with_margin * 100) if min_price_with_margin > 0 else 0

    # Determine recommendation
    if gross_margin_at_retail > 0 and min_price_with_margin >= retail_price * 0.9:
        recommendation = "keep_price"
        price_change_pct = 0.0
    elif min_price_with_margin < retail_price:
        recommendation = "reduce_price"
        price_change_pct = ((min_price_with_margin - retail_price) / retail_price) * 100
    else:
        recommendation = "increase_price"
        price_change_pct = ((min_price_with_margin - retail_price) / retail_price) * 100

    return {
        "retail_price": retail_price,
        "cost_price": cost_price,
        "platform_fee_percent": platform_fee_percent,
        "shipping_cost": shipping_cost,
        "total_cost_per_unit": round(total_cost_per_unit, 2),
        "min_price_with_10pct_safety_margin": round(min_price_with_margin, 2),
        "gross_margin_at_retail": round(gross_margin_at_retail, 2),
        "gross_margin_pct_at_retail": round(gross_margin_pct_at_retail, 2),
        "margin_at_min_price": round(margin_at_min_price, 2),
        "margin_pct_at_min_price": round(margin_pct_at_min_price, 2),
        "recommendation": recommendation,
        "price_change_pct": round(price_change_pct, 2),
        "safety_margin_percent": safety_margin_percent,
        "calculation_timestamp": datetime.now(timezone.utc).isoformat(),
    }


def calculate_conversion_rate(
    orders: List[Dict[str, Any]],
    product_sku: str,
    visits: Optional[List[Dict[str, Any]]] = None,
    days: int = 30,
) -> Dict[str, Any]:
    """Calculate conversion rate for a product."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    relevant_orders = [
        o for o in orders
        if o.get("sku") == product_sku
        and o.get("created_at", "") >= cutoff.isoformat()
    ]

    order_count = len(relevant_orders)
    total_visits = 0

    if visits:
        relevant_visits = [
            v for v in visits
            if v.get("sku") == product_sku
            and v.get("created_at", "") >= cutoff.isoformat()
        ]
        total_visits = len(relevant_visits)

    conversion_rate = (order_count / total_visits * 100) if total_visits > 0 else 0

    return {
        "product_sku": product_sku,
        "conversion_rate_pct": round(conversion_rate, 2),
        "order_count_30d": order_count,
        "total_visits_30d": total_visits,
        "period_days": days,
    }


def analyze_funnel(
    orders: List[Dict[str, Any]],
    product_sku: str,
    funnel_steps: List[str] | None = None,
    days: int = 30,
) -> Dict[str, Any]:
    """Analyze sales funnel for a product."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    relevant = [
        o for o in orders
        if o.get("sku") == product_sku
        and o.get("created_at", "") >= cutoff.isoformat()
    ]

    total_start = len(relevant)

    # Simplified funnel analysis based on order status/state
    # In a real implementation, this would track steps like: view -> add_to_cart -> checkout -> purchase
    step_counts = {}
    if funnel_steps:
        for step in funnel_steps:
            step_counts[step] = len([
                o for o in relevant
                if o.get("status", "").lower() == step.lower()
            ])
    else:
        # Default status-based funnel
        statuses = ["viewed", "added_to_cart", "checked_out", "purchased"]
        for step in statuses:
            step_counts[step] = len([
                o for o in relevant
                if o.get("status", "").lower() == step.lower()
            ])

    last_step = funnel_steps[-1] if funnel_steps else "purchased"
    last_step_count = step_counts.get(last_step, 0)

    conversion_at_last_step = (last_step_count / total_start * 100) if total_start > 0 else 0

    return {
        "product_sku": product_sku,
        "funnel_steps_defined": funnel_steps or ["viewed", "added_to_cart", "checked_out", "purchased"],
        "total_started": total_start,
        "last_step_count": last_step_count,
        "conversion_at_last_step_pct": round(conversion_at_last_step, 2),
        "funnel_drop-off": {
            step: round((step_counts.get(step, 0) / total_start * 100) if total_start > 0 else 0, 2)
            for step in (funnel_steps or ["viewed", "added_to_cart", "checked_out", "purchased"])
        },
        "period_days": days,
    }


class PricingRuleEngine:
    """Configurable pricing rule engine with 10% safety margin.

    Rules:
    - Minimum price must not fall below cost + platform fees + shipping + 10% margin
    - Price changes must be saved as suggestions first
    - Production changes require explicit authorization
    """

    def __init__(
        self,
        safety_margin_percent: float = 10.0,
        min_margin_pct: float = 15.0,
        require_authorization: bool = True,
    ) -> None:
        self.safety_margin_percent = safety_margin_percent
        self.min_margin_pct = min_margin_pct
        self.require_authorization = require_authorization
        self.rules: Dict[str, Dict[str, Any]] = {}
        self.pending_changes: Dict[str, Dict[str, Any]] = {}

    def add_rule(
        self,
        product_sku: str,
        rule_name: str,
        rule_config: Dict[str, Any],
    ) -> None:
        """Add a pricing rule for a product."""
        if product_sku not in self.rules:
            self.rules[product_sku] = {}
        self.rules[product_sku][rule_name] = {
            "config": rule_config,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(f"Pricing rule '{rule_name}' added for {product_sku}")

    def evaluate_price(
        self,
        product_sku: str,
        retail_price: float,
        cost_price: float,
        platform_fee_percent: float = 5.0,
        shipping_cost: float = 0.0,
    ) -> Dict[str, Any]:
        """Evaluate price against rules and return recommendation."""
        # Calculate base smart price
        base = calculate_smart_price(
            retail_price=retail_price,
            cost_price=cost_price,
            platform_fee_percent=platform_fee_percent,
            shipping_cost=shipping_cost,
            safety_margin_percent=self.safety_margin_percent,
        )

        # Apply rules
        rules_applied = []
        if product_sku in self.rules:
            for rule_name, rule_data in self.rules[product_sku].items():
                config = rule_data["config"]
                rule_result = self._apply_rule(
                    rule_name, base, config
                )
                rules_applied.append({
                    "rule_name": rule_name,
                    "result": rule_result,
                })

        # Determine final recommendation
        recommended_price = base["min_price_with_10pct_safety_margin"]
        recommendation = base["recommendation"]

        # Check minimum margin rule
        total_cost = base["total_cost_per_unit"]
        min_price = base["min_price_with_10pct_safety_margin"]
        current_margin_pct = ((retail_price - total_cost) / retail_price * 100) if retail_price > 0 else 0

        if current_margin_pct < self.min_margin_pct:
            recommendation = "increase_price"
            # Force price to minimum acceptable
            recommended_price = max(
                retail_price * (1 - 0.15),  # Max 15% reduction allowed
                min_price,
            )

        # Check if price would go below safety margin
        if recommended_price < min_price:
            recommended_price = min_price
            if base["recommendation"] != "increase_price":
                recommendation = "increase_price"

        return {
            "product_sku": product_sku,
            "retail_price": retail_price,
            "recommended_price": round(recommended_price, 2),
            "recommendation": recommendation,
            "current_margin_pct": round(current_margin_pct, 2),
            "min_acceptable_price": round(min_price, 2),
            "safety_margin_percent": self.safety_margin_percent,
            "min_margin_pct": self.min_margin_pct,
            "rules_applied": rules_applied,
            "requires_authorization": self.require_authorization,
        }

    def _apply_rule(
        self,
        rule_name: str,
        base_result: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply a single pricing rule."""
        rule_type = config.get("type", "none")

        if rule_type == "minimum_price":
            floor_price = config.get("floor_price", 0)
            base_result["min_price_with_10pct_safety_margin"] = max(
                floor_price, base_result["min_price_with_10pct_safety_margin"]
            )
        elif rule_type == "max_reduction_pct":
            max_pct = config.get("max_reduction_pct", 0)
            current_retail = base_result["retail_price"]
            if max_pct > 0:
                floor = current_retail * (1 - max_pct / 100)
                base_result["min_price_with_10pct_safety_margin"] = max(
                    floor, base_result["min_price_with_10pct_safety_margin"]
                )
        elif rule_type == "fixed_margin":
            min_margin = config.get("min_margin", 0)
            if min_margin > 0:
                cost = base_result["total_cost_per_unit"]
                base_result["min_price_with_10pct_safety_margin"] = max(
                    cost * (1 + min_margin / 100), base_result["min_price_with_10pct_safety_margin"]
                )

        return base_result

    def save_pending_change(
        self,
        product_sku: str,
        new_price: float,
        triggered_by: str,
    ) -> str:
        """Save a price change as pending (requires authorization)."""
        change_id = f"{product_sku}_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        self.pending_changes[change_id] = {
            "product_sku": product_sku,
            "new_price": new_price,
            "triggered_by": triggered_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending_authorization",
        }
        logger.info(f"Pending price change saved: {change_id} for {product_sku} at {new_price}")
        return change_id

    def get_pending_changes(self) -> List[Dict[str, Any]]:
        """Get all pending price changes awaiting authorization."""
        return [
            change for change in self.pending_changes.values()
            if change["status"] == "pending_authorization"
        ]

    def authorize_change(self, change_id: str) -> Dict[str, Any]:
        """Authorize a pending price change."""
        if change_id in self.pending_changes:
            self.pending_changes[change_id]["status"] = "authorized"
            logger.info(f"Price change authorized: {change_id}")
            return self.pending_changes[change_id]
        raise ValueError(f"Unknown change ID: {change_id}")

    def get_rules_summary(self) -> Dict[str, Any]:
        """Get summary of all active pricing rules."""
        total_rules = sum(len(rules) for rules in self.rules.values())
        products_with_rules = len(self.rules)

        return {
            "total_rules": total_rules,
            "products_with_rules": products_with_rules,
            "safety_margin_percent": self.safety_margin_percent,
            "min_margin_pct": self.min_margin_pct,
            "require_authorization": self.require_authorization,
        }


# Convenience function
def get_sales_automation() -> Dict[str, Any]:
    """Factory function for sales automation components."""
    return {
        "calculate_sales_velocity": calculate_sales_velocity,
        "calculate_abc_classification": calculate_abc_classification,
        "detect_slow_movers": detect_slow_movers,
        "calculate_smart_price": calculate_smart_price,
        "calculate_conversion_rate": calculate_conversion_rate,
        "analyze_funnel": analyze_funnel,
        "PricingRuleEngine": PricingRuleEngine,
    }