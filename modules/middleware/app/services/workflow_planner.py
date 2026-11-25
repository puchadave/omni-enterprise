from __future__ import annotations

import json
import re
import uuid
from typing import Any

from .workflow_action_registry import build_default_registry


def _extract_json(text: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _heuristic_plan(message: str, registry) -> dict[str, Any]:
    msg = (message or "").lower()
    steps = []
    if "import" in msg and "matterhorn" in msg:
        steps.append({"id": "s1", "action": "matterhorn.import", "params": {"mode": "safe", "dry_run": "dry" in msg or "test" in msg}, "depends_on": [], "requires_confirmation": True, "guardrails": {"mass_action": False}})
    if "enrich" in msg or "seo" in msg or "marktplatz" in msg or "marketplace" in msg:
        steps.append({"id": f"s{len(steps)+1}", "action": "marketplace.enrich_products", "params": {}, "depends_on": [s["id"] for s in steps], "requires_confirmation": True, "guardrails": {"uses_ai": True}})
    if "status" in msg or "lauf" in msg or "import" in msg:
        steps.append({"id": f"s{len(steps)+1}", "action": "matterhorn.list_import_runs", "params": {"limit": 10}, "depends_on": [], "requires_confirmation": False, "guardrails": {"read_only": True}})
    if not steps:
        steps.append({"id": "s1", "action": "workflow.noop", "params": {"message": message}, "depends_on": [], "requires_confirmation": False, "guardrails": {"read_only": True}})
    # ensure registry-valid
    valid_steps = []
    for s in steps:
        ok, errs = registry.validate_step(s)
        if ok:
            valid_steps.append(s)
    if not valid_steps:
        valid_steps = [{"id": "s1", "action": "workflow.noop", "params": {"message": message}, "depends_on": [], "requires_confirmation": False, "guardrails": {"read_only": True}}]
    return {
        "intent": "heuristic_plan",
        "summary": "Automatisch erzeugter Workflow-Plan",
        "risk_level": "medium" if any(s.get("requires_confirmation") for s in valid_steps) else "low",
        "steps": valid_steps,
        "dry_run_supported": True,
    }


async def plan_workflow(message: str, *, runtime: Any = None, actor: str = "", context: dict[str, Any] | None = None) -> dict[str, Any]:
    registry = build_default_registry(runtime)
    actions = registry.list_actions()
    client = getattr(runtime, "omniroute", None) if runtime else None
    plan = None
    if client is not None:
        prompt = {
            "task": "Create a safe multi-step business workflow plan. Only use allowed actions. No shell/sql/code execution.",
            "message": message,
            "allowed_actions": actions,
            "output_schema": {
                "intent": "string",
                "summary": "string",
                "risk_level": "low|medium|high",
                "dry_run_supported": True,
                "steps": [{"id": "s1", "action": "name", "params": {}, "depends_on": [], "requires_confirmation": True, "guardrails": {}, "estimate_seconds": 5}],
            },
        }
        try:
            res = await client.chat_completion(model="auto", messages=[
                {"role": "system", "content": "Return ONLY valid JSON. Use only allowed actions. Mark write/productive steps requires_confirmation=true."},
                {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
            ], timeout=30.0)
            plan = _extract_json(str(res.get("content") or ""))
        except Exception:
            plan = None
    if not isinstance(plan, dict):
        plan = _heuristic_plan(message, registry)

    steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
    cleaned = []
    errors = []
    for i, s in enumerate(steps, start=1):
        if not isinstance(s, dict):
            continue
        s = {**s}
        s.setdefault("id", f"s{i}")
        s.setdefault("params", {})
        s.setdefault("depends_on", [])
        s.setdefault("guardrails", {})
        action = registry.get(str(s.get("action") or ""))
        if not action:
            errors.append(f"unknown_action:{s.get('action')}"); continue
        if action.requires_confirmation or action.destructive or not action.read_only:
            s["requires_confirmation"] = True
        ok, step_errors = registry.validate_step(s)
        if not ok:
            errors.extend(step_errors); continue
        cleaned.append(s)
    if not cleaned:
        cleaned = _heuristic_plan(message, registry)["steps"]
    risk = "high" if any(registry.get(s["action"]).destructive for s in cleaned if registry.get(s["action"])) else ("medium" if any(s.get("requires_confirmation") for s in cleaned) else "low")
    return {
        "workflow_id": str(uuid.uuid4()),
        "conversation_id": str((context or {}).get("conversation_id") or ""),
        "intent": str(plan.get("intent") or "workflow_plan"),
        "summary": str(plan.get("summary") or "Workflow-Plan"),
        "risk_level": str(plan.get("risk_level") or risk),
        "dry_run_supported": bool(plan.get("dry_run_supported", True)),
        "steps": cleaned,
        "guardrails": {"registry_enforced": True, "errors": errors[:50]},
        "requires_confirmation": any(bool(s.get("requires_confirmation")) for s in cleaned),
    }
