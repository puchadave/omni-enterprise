import asyncio
import os

import pytest

os.environ["DATABASE_URL"] = ""


@pytest.fixture()
def temp_db(monkeypatch, tmp_path):
    db_path = tmp_path / "wf.sqlite"
    import database
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)
    asyncio.run(database.init_db())
    yield database


def test_workflow_plan_and_registry_guardrails(temp_db):
    from services.workflow_planner import plan_workflow
    from services.workflow_action_registry import build_default_registry

    reg = build_default_registry(runtime=None)
    plan = asyncio.run(plan_workflow("Importiere Matterhorn und enrichere alle Produkte für alle Marktplätze", runtime=None))
    assert plan["steps"]
    for step in plan["steps"]:
        ok, errs = reg.validate_step(step)
        assert ok, errs
    assert any(step["action"] == "matterhorn.import" for step in plan["steps"])
    assert any(step["requires_confirmation"] for step in plan["steps"])


def test_workflow_run_requires_confirmation_and_executes(temp_db):
    from services.workflow_orchestrator import create_workflow_run, execute_workflow_run

    plan = {
        "intent": "test",
        "summary": "noop workflow",
        "requires_confirmation": False,
        "steps": [
            {"id": "s1", "action": "workflow.noop", "params": {"x": 1}, "depends_on": [], "requires_confirmation": False, "guardrails": {}}
        ],
    }
    run = asyncio.run(create_workflow_run(plan, actor="tester"))
    assert run["status"] in {"planned", "awaiting_confirmation"}
    done = asyncio.run(execute_workflow_run(run["id"], actor="tester", runtime=None, confirmed=True))
    assert done["status"] == "completed"
    assert done["steps"][0]["status"] == "succeeded"
