"""Export approval gate for all sales channels.
Conservative by default: review_required unless explicitly approved.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

APPROVED = "approved"
REJECTED = "rejected"
HOLD = "hold"
REVIEW_REQUIRED = "review_required"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def gate_mode() -> str:
    return (os.getenv("EXPORT_GATE_MODE") or "review_required").strip().lower()


def auto_approve_enabled() -> bool:
    v = (os.getenv("PRODUCT_INTELLIGENCE_AUTO_APPROVE") or "false").strip().lower()
    return v in {"1", "true", "yes", "on"}


@dataclass
class GateDecision:
    allowed: bool
    decision: str
    reason: str
    source: str = "export_gate"
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "decision": self.decision,
            "reason": self.reason,
            "source": self.source,
            "details": self.details or {},
            "checked_at": _now_iso(),
        }


async def get_latest_approval(sku: str, channel: str) -> Optional[dict[str, Any]]:
    try:
        from database import get_product_export_approval
        return await get_product_export_approval(sku=sku, channel=channel)
    except Exception:
        return None


async def is_export_approved(sku: str, channel: str, *, payload: dict[str, Any] | None = None) -> GateDecision:
    """Check whether a SKU may be exported to channel. Fail-closed to review_required."""
    if not sku:
        return GateDecision(False, REVIEW_REQUIRED, "missing_sku")
    approval = await get_latest_approval(sku, channel)
    if not approval:
        # conservative default
        mode = gate_mode()
        if mode == "open":
            return GateDecision(True, APPROVED, "gate_mode_open_no_record")
        return GateDecision(False, REVIEW_REQUIRED, "no_approval_record")
    decision = str(approval.get("decision") or REVIEW_REQUIRED).lower()
    if decision == APPROVED:
        # optional expiry
        expires = approval.get("expires_at")
        if expires:
            try:
                exp = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp:
                    return GateDecision(False, REVIEW_REQUIRED, "approval_expired", details={"expired_at": str(expires)})
            except Exception:
                pass
        return GateDecision(True, APPROVED, "approved", source=str(approval.get("source") or "approval"))
    if decision == REJECTED:
        return GateDecision(False, REJECTED, "rejected", details={"reasons": approval.get("reasons")})
    if decision == HOLD:
        return GateDecision(False, HOLD, "on_hold", details={"reasons": approval.get("reasons")})
    return GateDecision(False, REVIEW_REQUIRED, f"decision_{decision}")


async def record_decision(
    *,
    sku: str,
    channel: str,
    decision: str,
    source: str = "manual",
    reasons: Any = None,
    score: float | None = None,
    modifications: Any = None,
    actor: str = "system",
    expires_at: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    from database import upsert_product_export_approval
    rec = {
        "sku": sku,
        "channel": channel,
        "decision": decision,
        "source": source,
        "reasons_json": json.dumps(reasons or [], ensure_ascii=False),
        "score": score,
        "modifications_json": json.dumps(modifications or {}, ensure_ascii=False),
        "actor": actor,
        "expires_at": expires_at,
        "run_id": run_id,
        "updated_at": _now_iso(),
    }
    return await upsert_product_export_approval(rec)
