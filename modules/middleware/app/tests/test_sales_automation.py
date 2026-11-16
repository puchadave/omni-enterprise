"""Sales Automation Tests"""

import pytest
from datetime import datetime, timedelta, timezone
from services.sales_automation import (
    calculate_sales_velocity,
    calculate_abc_classification,
    detect_slow_movers,
    calculate_smart_price,
    calculate_conversion_rate,
    analyze_funnel,
    PricingRuleEngine,
    get_sales_automation,
)


# Helper to create orders with controlled dates
def make_order(sku, quantity, days_ago=0):
    date = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"sku": sku, "quantity": quantity, "created_at": date.isoformat()}


class TestSalesVelocity:
    """Test sales velocity calculation."""

    def test_basic_velocity_calculation(self):
        # Orders within last 30 days
        orders = [
            make_order("TEST001", 2, days_ago=5),
            make_order("TEST001", 3, days_ago=15),
            make_order("TEST001", 1, days_ago=25),
        ]
        vel = calculate_sales_velocity(orders, "TEST001", days=30)
        assert vel["product_sku"] == "TEST001"
        assert vel["total_quantity_30d"] == 6
        assert vel["order_count_30d"] == 3

    def test_velocity_with_trend(self):
        orders = [
            make_order("TEST002", 5, days_ago=1),
            make_order("TEST002", 5, days_ago=15),
            make_order("TEST002", 5, days_ago=29),
        ]
        vel = calculate_sales_velocity(orders, "TEST002", days=30)
        assert vel["total_quantity_30d"] == 15


class TestABCClassification:
    """Test ABC classification."""

    def test_abc_classification_a(self):
        orders = [
            make_order("FAST001", 150, days_ago=5),
            make_order("FAST001", 200, days_ago=10),
        ]
        classification = calculate_abc_classification(orders, "FAST001", days=365)
        assert classification["classification"] in ["A", "B", "C"]

    def test_abc_classification_no_sales(self):
        orders = []
        classification = calculate_abc_classification(orders, "NOSALE", days=365)
        assert classification["classification"] == "C"


class TestSlowMovers:
    """Test slow mover detection."""

    def test_slow_mover_detected(self):
        orders = [
            make_order("SLOW001", 3, days_ago=5),
            make_order("SLOW001", 2, days_ago=10),
        ]
        result = detect_slow_movers(orders, "SLOW001", days=90, threshold_qty=5)
        # 5 units in 90 days with threshold 5 = slow mover
        assert result["is_slow_mover"] is True

    def test_not_a_slow_mover(self):
        orders = [
            make_order("FAST001", 50, days_ago=5),
            make_order("FAST001", 30, days_ago=10),
        ]
        result = detect_slow_movers(orders, "FAST001", days=90, threshold_qty=5)
        # 80 units > threshold 5 = not a slow mover
        assert result["is_slow_mover"] is False


class TestSmartPricing:
    """Test smart pricing with 10% safety margin."""

    def test_smart_price_basic(self):
        result = calculate_smart_price(retail_price=100.0, cost_price=60.0)
        assert result["retail_price"] == 100.0
        assert result["cost_price"] == 60.0
        assert "min_price_with_10pct_safety_margin" in result
        assert result["recommendation"] in ["keep_price", "reduce_price", "increase_price"]

    def test_smart_price_with_margin(self):
        result = calculate_smart_price(
            retail_price=100.0, cost_price=40.0, platform_fee_percent=5.0, shipping_cost=5.0
        )
        assert result["gross_margin_pct_at_retail"] > 0

    def test_safety_margin_not_deducted_from_retail(self):
        result = calculate_smart_price(retail_price=100.0, cost_price=85.0)
        min_price = result["min_price_with_10pct_safety_margin"]
        assert min_price > 85.0


class TestPricingRuleEngine:
    """Test pricing rule engine."""

    def test_rule_engine_initialization(self):
        engine = PricingRuleEngine(safety_margin_percent=10.0, min_margin_pct=15.0)
        rules = engine.get_rules_summary()
        assert "total_rules" in rules
        assert "products_with_rules" in rules

    def test_rule_engine_with_rule(self):
        engine = PricingRuleEngine(safety_margin_percent=10.0)
        engine.add_rule("SKU001", "min_price", {"type": "minimum_price", "floor_price": 50.0})
        result = engine.evaluate_price(
            product_sku="SKU001", retail_price=100.0, cost_price=60.0
        )
        assert "recommended_price" in result


class TestPricingRecommendations:
    """Test pricing recommendations."""

    def test_calculate_smart_price_various_scenarios(self):
        # Low margin
        result = calculate_smart_price(retail_price=50.0, cost_price=45.0)
        assert result["gross_margin_pct_at_retail"] < 10

        # High margin
        result = calculate_smart_price(retail_price=100.0, cost_price=30.0)
        assert result["gross_margin_pct_at_retail"] > 50


class TestSalesAutomationFactory:
    """Test factory function."""

    def test_get_sales_automation(self):
        components = get_sales_automation()
        assert "calculate_sales_velocity" in components
        assert "calculate_abc_classification" in components
        assert "detect_slow_movers" in components
        assert "calculate_smart_price" in components
