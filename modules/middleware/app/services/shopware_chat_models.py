from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ChannelScope = Literal["shopware"]
PlanStatus = Literal["draft", "confirmed", "executing", "completed", "failed", "rolled_back"]


@dataclass
class ShopwareChatPlan:
    plan_id: str
    channel_scope: ChannelScope
    intent: str
    summary: str
    skus: list[str]
    diff: dict[str, Any]
    guardrails: dict[str, Any]
    idempotency_key: str
    status: PlanStatus = "draft"
    created_by: str = ""
    prompt: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "channel_scope": self.channel_scope,
            "intent": self.intent,
            "summary": self.summary,
            "skus": self.skus,
            "diff": self.diff,
            "guardrails": self.guardrails,
            "idempotency_key": self.idempotency_key,
            "status": self.status,
            "created_by": self.created_by,
            "prompt": self.prompt,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
