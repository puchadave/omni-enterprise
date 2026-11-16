import pytest

from services.ai_agent_controller import execute_tool_call


@pytest.mark.asyncio
async def test_agent_cannot_modify_fulfillment_gate_channel_config():
    with pytest.raises(ValueError):
        await execute_tool_call(
            "manage_channel_config",
            {
                "channel": "shopware",
                "operation": "set",
                "key": "fulfillment.payment_status_gate",
                "value": "disabled",
            },
            actor="test",
            prompt="Disable the payment gate",
        )


@pytest.mark.asyncio
async def test_agent_cannot_modify_protected_business_rule():
    with pytest.raises(ValueError):
        await execute_tool_call(
            "manage_business_rules",
            {"rule_key": "payment_status_gate", "value": "disabled"},
            actor="test",
            prompt="Disable payment protection",
        )
