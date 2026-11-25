from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from database import (
    workflow_run_create,
    workflow_run_get,
    workflow_run_update,
    workflow_step_create,
    workflow_step_update,
    workflow_steps_list,
    workflow_template_upsert,
    workflow_templates_list,
)
from .workflow_action_registry import build_default_registry

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def create_workflow_run(plan: dict[str, Any], *, actor: str = "", conversation_id: str = "") -> dict[str, Any]:
    run = await workflow_run_create({
        "conversation_id": conversation_id or plan.get("conversation_id") or "",
        "actor": actor,
        "intent": plan.get("intent") or "",
        "summary": plan.get("summary") or "",
        "status": "awaiting_confirmation" if plan.get("requires_confirmation") else "planned",
        "plan": plan,
    })
    run_id = str(run.get("id"))
    for i, step in enumerate(plan.get("steps") or []):
        await workflow_step_create(run_id, i, step)
    return await get_workflow_run(run_id)


async def get_workflow_run(run_id: str) -> dict[str, Any] | None:
    run = await workflow_run_get(run_id)
    if not run:
        return None
    steps = await workflow_steps_list(run_id)
    run["steps"] = steps
    return run


async def list_workflow_templates() -> list[dict[str, Any]]:
    return await workflow_templates_list()


async def save_workflow_template(*, name: str, description: str, plan: dict[str, Any], created_by: str = "", trigger_examples: list[str] | None = None) -> dict[str, Any]:
    return await workflow_template_upsert({
        "name": name,
        "description": description,
        "plan": plan,
        "created_by": created_by,
        "trigger_examples": trigger_examples or [],
    })


def _deps_done(step: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> bool:
    for dep in (step.get("depends_on") or []):
        dep_id = str(dep)
        dep_step = by_id.get(dep_id)
        if not dep_step or str(dep_step.get("status")) != "succeeded":
            return False
    return True


async def execute_workflow_run(run_id: str, *, actor: str = "", runtime: Any = None, confirmed: bool = False) -> dict[str, Any]:
    run = await get_workflow_run(run_id)
    if not run:
        raise ValueError("run_not_found")
    registry = build_default_registry(runtime)
    plan = run.get("plan_json") if isinstance(run.get("plan_json"), dict) else {}
    steps = run.get("steps") or []
    by_id = {str(s.get("id")): s for s in steps}
    write_steps = [s for s in steps if bool(s.get("requires_confirmation")) or not registry.get(str(s.get("action")) or "").read_only]
    if write_steps and not confirmed and str(run.get("status")) in {"planned", "awaiting_confirmation"}:
        await workflow_run_update(run_id, status="awaiting_confirmation")
        return await get_workflow_run(run_id)

    await workflow_run_update(run_id, status="running", started_at=_now(), error_message="")
    ctx = {"run_id": run_id, "actor": actor}
    for step in steps:
        sid = str(step.get("id"))
        status = str(step.get("status") or "pending")
        if status in {"succeeded", "skipped"}:
            continue
        if not _deps_done(step, by_id):
            await workflow_step_update(sid, status="blocked", error_message="dependencies_not_satisfied")
            by_id[sid]["status"] = "blocked"
            continue
        action = registry.get(str(step.get("action") or ""))
        if not action or not action.handler:
            await workflow_step_update(sid, status="failed", error_message="action_not_available")
            by_id[sid]["status"] = "failed"
            continue
        if (action.requires_confirmation or action.destructive or not action.read_only) and not confirmed:
            await workflow_step_update(sid, status="awaiting_confirmation")
            by_id[sid]["status"] = "awaiting_confirmation"
            await workflow_run_update(run_id, status="awaiting_confirmation")
            return await get_workflow_run(run_id)
        await workflow_step_update(sid, status="running", started_at=_now(), error_message="")
        try:
            params = step.get("params_json") if isinstance(step.get("params_json"), dict) else {}
            result = await action.handler(params, ctx)
            await workflow_step_update(sid, status="succeeded", result_json=result if isinstance(result, dict) else {"result": result}, finished_at=_now(), error_message="")
            by_id[sid]["status"] = "succeeded"
        except Exception as exc:
            logger.exception("workflow_step_failed run=%s step=%s", run_id, sid)
            await workflow_step_update(sid, status="failed", error_message=str(exc)[:1000], finished_at=_now())
            by_id[sid]["status"] = "failed"
            await workflow_run_update(run_id, status="failed", error_message=str(exc)[:1000], finished_at=_now())
            return await get_workflow_run(run_id)

    final_steps = await workflow_steps_list(run_id)
    if any(str(s.get("status")) == "failed" for s in final_steps):
        final = "failed"
    elif any(str(s.get("status")) in {"awaiting_confirmation","blocked"} for s in final_steps):
        final = "awaiting_confirmation"
    else:
        final = "completed"
    await workflow_run_update(run_id, status=final, finished_at=_now() if final in {"completed","failed"} else None)
    return await get_workflow_run(run_id)


async def retry_workflow_step(run_id: str, step_id: str, *, actor: str = "", runtime: Any = None, confirmed: bool = False) -> dict[str, Any]:
    await workflow_step_update(step_id, status="pending", error_message="", result_json={})
    await workflow_run_update(run_id, status="planned", error_message="")
    return await execute_workflow_run(run_id, actor=actor, runtime=runtime, confirmed=confirmed)
