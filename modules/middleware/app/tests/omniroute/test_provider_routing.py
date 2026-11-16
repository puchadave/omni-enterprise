"""Tests for OmniRoute Control Center - Provider Management and Routing."""

from __future__ import annotations

import pytest

from services.omniroute_provider import AIProvider, ProviderManager, PROVIDER_HEALTHY
from services.omniroute_model_registry import (
    AIModel, ModelRegistry, RoutingRule, RoutingPolicy,
    IntelligentRoutingEngine, CostCalculator,
)


# ── Provider Tests ──────────────────────────────────────────────────────

def test_provider_initialization():
    """Test AIProvider initialization."""
    provider = AIProvider(
        provider_id="p1",
        name="Test Provider",
        provider_type="text",
        api_endpoint="https://api.example.com",
        api_key_encrypted="encrypted_key",
        priority=50,
        rate_limit_per_minute=120,
    )
    assert provider.provider_id == "p1"
    assert provider.name == "Test Provider"
    assert provider.provider_type == "text"
    assert provider.priority == 50
    assert provider.rate_limit_per_minute == 120
    assert provider.health_status == "unknown"


def test_provider_to_dict():
    """Test provider to_dict serialization."""
    provider = AIProvider(
        provider_id="p1",
        name="Test Provider",
        provider_type="text",
        api_endpoint="https://api.example.com",
        api_key_encrypted="key",
    )
    d = provider.to_dict()
    assert d["provider_id"] == "p1"
    assert d["name"] == "Test Provider"
    assert d["capabilities"] == []


# ── Provider Manager Tests ──────────────────────────────────────────────

def test_provider_manager_add_remove():
    """Test adding and removing providers."""
    manager = ProviderManager()
    provider = AIProvider(
        provider_id="p1", name="Test", provider_type="text",
        api_endpoint="https://api.test.com", api_key_encrypted="key",
    )
    manager.add_provider(provider)
    assert len(manager.list_providers()) == 1
    assert manager.get_provider("p1") is not None
    
    assert manager.remove_provider("p1") is True
    assert len(manager.list_providers()) == 0
    assert manager.get_provider("p1") is None
    assert manager.remove_provider("nonexistent") is False


def test_provider_manager_enable_disable():
    """Test enabling/disabling providers."""
    manager = ProviderManager()
    provider = AIProvider(
        provider_id="p1", name="Test", provider_type="text",
        api_endpoint="https://api.test.com", api_key_encrypted="key",
    )
    manager.add_provider(provider)
    
    assert manager.disable_provider("p1") is True
    assert manager.get_provider("p1").is_active is False
    
    assert manager.enable_provider("p1") is True
    assert manager.get_provider("p1").is_active is True


def test_provider_manager_list_active():
    """Test listing active providers only."""
    manager = ProviderManager()
    p1 = AIProvider(provider_id="p1", name="Active", provider_type="text",
                    api_endpoint="https://api1.com", api_key_encrypted="key", is_active=True)
    p2 = AIProvider(provider_id="p2", name="Inactive", provider_type="image",
                    api_endpoint="https://api2.com", api_key_encrypted="key", is_active=False)
    manager.add_provider(p1)
    manager.add_provider(p2)
    
    all_providers = manager.list_providers()
    active_providers = manager.list_providers(active_only=True)
    
    assert len(all_providers) == 2
    assert len(active_providers) == 1
    assert active_providers[0].provider_id == "p1"


# ── Model Registry Tests ────────────────────────────────────────────────

def test_model_registry_register():
    """Test model registration."""
    registry = ModelRegistry()
    model = AIModel(
        model_id="m1", provider_id="p1", model_id_str="gpt-4",
        model_name="GPT-4", model_category="text",
        capabilities=["text_generation", "summarization"],
        context_window=8192,
        input_price_per_1k=0.03,
        output_price_per_1k=0.06,
    )
    registry.register_model(model)
    assert registry.get_model("m1") is not None
    assert len(registry.list_models()) == 1


def test_model_registry_list_by_category():
    """Test listing models by category."""
    registry = ModelRegistry()
    m1 = AIModel(model_id="m1", provider_id="p1", model_id_str="t1",
                 model_name="Text Model", model_category="text")
    m2 = AIModel(model_id="m2", provider_id="p1", model_id_str="i1",
                 model_name="Image Model", model_category="image")
    registry.register_model(m1)
    registry.register_model(m2)
    
    text_models = registry.list_models(category="text")
    image_models = registry.list_models(category="image")
    
    assert len(text_models) == 1
    assert len(image_models) == 1
    assert text_models[0].model_name == "Text Model"


def test_model_registry_find_by_capability():
    """Test finding models by capability."""
    registry = ModelRegistry()
    m1 = AIModel(model_id="m1", provider_id="p1", model_id_str="t1",
                 model_name="Text Model", model_category="text",
                 capabilities=["text_generation", "summarization"])
    m2 = AIModel(model_id="m2", provider_id="p1", model_id_str="i1",
                 model_name="Image Model", model_category="image",
                 capabilities=["image_generation"])
    registry.register_model(m1)
    registry.register_model(m2)
    
    text_gen_models = registry.find_models_by_capability("text_generation")
    image_gen_models = registry.find_models_by_capability("image_generation")
    
    assert len(text_gen_models) == 1
    assert len(image_gen_models) == 1


# ── Routing Rule Tests ─────────────────────────────────────────────────

def test_routing_rule_creation():
    """Test routing rule creation."""
    rule = RoutingRule(
        rule_id="r1", name="Lowest Cost", strategy="lowest_cost",
        provider_priority=["p1", "p2"],
        model_category="text",
        min_quality_score=0.8,
        max_cost_per_1k=0.10,
    )
    assert rule.rule_id == "r1"
    assert rule.strategy == "lowest_cost"
    assert rule.max_cost_per_1k == 0.10


# ── Intelligent Routing Engine Tests ────────────────────────────────────

def test_routing_engine_select_lowest_cost():
    """Test lowest cost routing strategy."""
    registry = ModelRegistry()
    engine = IntelligentRoutingEngine(registry)
    
    # Register models with different costs
    m1 = AIModel(model_id="m1", provider_id="p1", model_id_str="cheap",
                 model_name="Cheap Model", model_category="text",
                 input_price_per_1k=0.01, output_price_per_1k=0.02)
    m2 = AIModel(model_id="m2", provider_id="p2", model_id_str="expensive",
                 model_name="Expensive Model", model_category="text",
                 input_price_per_1k=0.10, output_price_per_1k=0.20)
    registry.register_model(m1)
    registry.register_model(m2)
    
    rule = RoutingRule(rule_id="r1", name="Lowest Cost", strategy="lowest_cost",
                       model_category="text")
    engine.add_rule(rule)
    
    policy = RoutingPolicy(policy_id="p1", workflow_name="test_workflow",
                           rule_id="r1")
    engine.add_policy(policy)
    
    provider_id, model_id = engine.select_provider(workflow_name="test_workflow")
    assert provider_id == "p1"  # Cheapest provider
    # model_id_str is the external model identifier
    model = registry.get_model(model_id)
    assert model.model_id_str == "cheap"


def test_routing_engine_no_policy():
    """Test routing with no policy configured."""
    registry = ModelRegistry()
    engine = IntelligentRoutingEngine(registry)
    
    provider_id, model_id = engine.select_provider(workflow_name="nonexistent")
    assert provider_id is None
    assert model_id is None


def test_routing_engine_no_candidates():
    """Test routing when no providers match criteria."""
    registry = ModelRegistry()
    engine = IntelligentRoutingEngine(registry)
    
    m1 = AIModel(model_id="m1", provider_id="p1", model_id_str="t1",
                 model_name="Text Model", model_category="text",
                 input_price_per_1k=0.05)
    registry.register_model(m1)
    
    rule = RoutingRule(rule_id="r1", name="Test", strategy="lowest_cost",
                       model_category="text", max_cost_per_1k=0.01)  # Too low
    engine.add_rule(rule)
    
    policy = RoutingPolicy(policy_id="p1", workflow_name="test",
                           rule_id="r1")
    engine.add_policy(policy)
    
    provider_id, model_id = engine.select_provider(
        workflow_name="test", max_cost=0.01
    )
    assert provider_id is None  # No model meets cost requirement


# ── Cost Calculator Tests ──────────────────────────────────────────────

def test_cost_calculator_text():
    """Test text cost calculation."""
    cost = CostCalculator.calculate_text_cost(
        input_tokens=1000, output_tokens=500,
        input_price_per_1k=0.03, output_price_per_1k=0.06,
    )
    assert cost == 0.06  # (1000/1000 * 0.03) + (500/1000 * 0.06) = 0.03 + 0.03 = 0.06


def test_cost_calculator_image():
    """Test image cost calculation."""
    cost = CostCalculator.calculate_image_cost(
        num_images=4, price_per_image=0.04,
    )
    assert cost == 0.16


def test_cost_calculator_total():
    """Test total cost calculation."""
    text_cost = CostCalculator.calculate_text_cost(
        input_tokens=1000, output_tokens=500,
        input_price_per_1k=0.03, output_price_per_1k=0.06,
    )
    image_cost = CostCalculator.calculate_image_cost(
        num_images=2, price_per_image=0.04,
    )
    total = text_cost + image_cost
    assert total == 0.14  # 0.06 + 0.08