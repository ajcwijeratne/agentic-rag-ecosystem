from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient


def _modules(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("OBSIDIAN_PROJECTS_PATH", "Projects")
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    from orchestrator import governance, obsidian_projects, operating, production
    import orchestrator.agent_executor as agent_executor
    import orchestrator.main as main

    governance = importlib.reload(governance)
    production = importlib.reload(production)
    operating = importlib.reload(operating)
    obsidian_projects = importlib.reload(obsidian_projects)
    agent_executor = importlib.reload(agent_executor)
    main = importlib.reload(main)
    return operating, production, governance, obsidian_projects, agent_executor, TestClient(main.app)


def test_operating_plan_task_memory_and_daily_brief(tmp_path, monkeypatch):
    operating, production, governance, obsidian_projects, agent_executor, client = _modules(tmp_path, monkeypatch)
    plan_id = operating.create_plan(
        "Launch operating cadence",
        project="WijerCo",
        goal="Run daily autonomous checks",
        tasks=[{"title": "Review approval queue", "type": "approval", "status": "todo", "priority": 5}],
    )
    task_id = operating.add_task(plan_id, "Prepare daily brief", type="agent", status="doing")
    memory_id = operating.add_project_memory("WijerCo", "Prefer review before publication.", source="test")

    plan = operating.get_plan(plan_id)
    assert plan["task_counts"]["todo"] == 1
    assert plan["task_counts"]["doing"] == 1
    assert operating.update_task(task_id, status="done") is True

    brief = operating.daily_brief()
    assert brief["date"]
    assert any(m["memory_id"] == memory_id for m in brief["project_memory"])


def test_operating_syncs_pending_governance_to_tasks(tmp_path, monkeypatch):
    operating, production, governance, obsidian_projects, agent_executor, client = _modules(tmp_path, monkeypatch)
    pid = production.create_production("Gate check", "demo", "linkedin_short", "Aaron")
    production.transition(pid, "review", "test", "ready")

    created = operating.sync_approval_tasks()
    assert {item["gate"] for item in created} == {"public_claim", "external_publish"}

    overview = operating.overview()
    assert overview["stats"]["waiting_approval"] == 2
    assert all(task["status"] == "waiting_approval" for task in overview["approval_tasks"])


def test_operating_sync_closes_tasks_after_gates_clear(tmp_path, monkeypatch):
    operating, production, governance, obsidian_projects, agent_executor, client = _modules(tmp_path, monkeypatch)
    pid = production.create_production("Resolved gate", "demo", "linkedin_short", "Aaron")
    production.transition(pid, "review", "test", "ready")
    operating.sync_approval_tasks()

    waiting = operating.list_tasks(status="waiting_approval", limit=20)
    assert {task["meta"]["gate"] for task in waiting} == {"public_claim", "external_publish"}

    governance.approve("public_claim", pid, "test")
    governance.approve("external_publish", pid, "test")
    assert operating.sync_approval_tasks() == []

    assert operating.list_tasks(status="waiting_approval", limit=20) == []
    resolved = [task for task in operating.list_tasks(status="done", limit=20) if task["target_id"] == pid]
    assert len(resolved) == 2
    assert all(task["meta"]["approval_sync"]["status"] == "cleared" for task in resolved)


def test_operating_syncs_production_next_actions_to_tasks(tmp_path, monkeypatch):
    operating, production, governance, obsidian_projects, agent_executor, client = _modules(tmp_path, monkeypatch)
    pid = production.create_production("Next action piece", "demo", "linkedin_short", "Aaron")

    created = operating.sync_production_tasks()
    assert len(created) == 1
    assert created[0]["production_id"] == pid
    assert created[0]["next_action"] == "Build brief"

    overview = operating.overview()
    production_tasks = overview["production_tasks"]
    assert overview["stats"]["production_tasks"] == 1
    assert production_tasks[0]["type"] == "production"
    assert production_tasks[0]["target_id"] == pid
    assert production_tasks[0]["meta"]["production"]["next_action"] == "Build brief"

    assert operating.sync_production_tasks() == []


def test_blocked_production_syncs_as_approval_not_production_task(tmp_path, monkeypatch):
    operating, production, governance, obsidian_projects, agent_executor, client = _modules(tmp_path, monkeypatch)
    pid = production.create_production("Blocked publish", "demo", "linkedin_short", "Aaron")
    production.transition(pid, "review", "test", "ready")

    overview = operating.overview()
    assert overview["stats"]["waiting_approval"] == 2
    assert not [t for t in overview["production_tasks"] if t["target_id"] == pid]


def test_generate_plan_from_goal_creates_dependencies_and_next_action(tmp_path, monkeypatch):
    operating, production, governance, obsidian_projects, agent_executor, client = _modules(tmp_path, monkeypatch)

    generated = operating.generate_plan_from_goal(
        "Harden deployment with backup, rollback, monitoring and operational rehearsal",
        project="ops",
        owner="Aaron",
    )

    assert generated["created"] is True
    assert generated["workflow"] == "deployment"
    assert generated["confidence"] >= 0.55
    assert generated["next_action"]["title"].startswith("Confirm deployment scope")

    tasks = generated["tasks"]
    assert len(tasks) == 6
    dependent = next(task for task in tasks if task["title"].startswith("Run migrations"))
    assert dependent["meta"]["planner"]["depends_on"]

    next_action = operating.recommend_next_action(plan_id=generated["plan"]["plan_id"])
    assert next_action["task"]["task_id"] == generated["next_action"]["task_id"]


def test_generate_plan_route_preview_and_create(tmp_path, monkeypatch):
    operating, production, governance, obsidian_projects, agent_executor, client = _modules(tmp_path, monkeypatch)

    preview = client.post(
        "/operating/plans/generate",
        headers={"x-api-key": "admin-key"},
        json={
            "goal": "Create a campaign video from brief to publish",
            "project": "content",
            "create": False,
        },
    )
    assert preview.status_code == 200
    assert preview.json()["created"] is False
    assert preview.json()["workflow"] == "content_studio"

    created = client.post(
        "/operating/plans/generate",
        headers={"x-api-key": "admin-key"},
        json={
            "goal": "Create a campaign video from brief to publish",
            "project": "content",
        },
    )
    assert created.status_code == 200
    plan_id = created.json()["plan"]["plan_id"]

    next_action = client.get(
        "/operating/next-action",
        headers={"x-api-key": "test-key"},
        params={"plan_id": plan_id},
    )
    assert next_action.status_code == 200
    assert next_action.json()["task"]["title"].startswith("Clarify brief")


def test_obsidian_project_sync_and_import(tmp_path, monkeypatch):
    operating, production, governance, obsidian_projects, agent_executor, client = _modules(tmp_path, monkeypatch)
    generated = operating.generate_plan_from_goal(
        "Create a campaign video from brief to publish",
        project="Client Alpha",
        owner="Aaron",
    )
    plan_id = generated["plan"]["plan_id"]

    status = client.get("/operating/projects/obsidian-status", headers={"x-api-key": "test-key"})
    assert status.status_code == 200
    assert status.json()["configured"] is True

    synced = client.post(
        f"/operating/plans/{plan_id}/sync-obsidian",
        headers={"x-api-key": "admin-key"},
        json={"overwrite": True},
    )
    assert synced.status_code == 200
    path = synced.json()["path"]
    text = open(path, encoding="utf-8").read()
    assert "planner_plan_id:" in text
    assert "## Tasks" in text
    assert "Clarify brief" in text

    text = text.replace("## Decisions\n\n\n## Risks", "## Decisions\n\nUse a concise executive tone.\n\n## Risks")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)

    imported = client.post(
        "/operating/projects/import-obsidian",
        headers={"x-api-key": "admin-key"},
        json={"project": "Client Alpha"},
    )
    assert imported.status_code == 200
    assert imported.json()["imported"] == 1
    memories = operating.list_project_memory("Client Alpha")
    assert any("concise executive tone" in item["content"] for item in memories)


def test_operating_routes_round_trip(tmp_path, monkeypatch):
    operating, production, governance, obsidian_projects, agent_executor, client = _modules(tmp_path, monkeypatch)

    created = client.post(
        "/operating/plans",
        headers={"x-api-key": "admin-key"},
        json={
            "title": "Autonomous week",
            "project": "demo",
            "goal": "Keep work moving",
            "tasks": [{"title": "Check blockers", "type": "manual", "status": "todo"}],
        },
    )
    assert created.status_code == 200
    plan_id = created.json()["plan_id"]
    task_id = created.json()["tasks"][0]["task_id"]

    patched = client.patch(
        f"/operating/tasks/{task_id}",
        headers={"x-api-key": "admin-key"},
        json={"status": "done"},
    )
    assert patched.status_code == 200
    assert patched.json()["items"][0]["status"] == "done"

    memory = client.post(
        "/operating/project-memory",
        headers={"x-api-key": "admin-key"},
        json={"project": "demo", "content": "Client prefers concise updates.", "source": "test"},
    )
    assert memory.status_code == 200
    assert memory.json()["items"][0]["content"] == "Client prefers concise updates."

    brief = client.get("/operating/daily-brief", headers={"x-api-key": "test-key"})
    assert brief.status_code == 200
    assert "summary" in brief.json()

    fetched = client.get(f"/operating/plans/{plan_id}", headers={"x-api-key": "test-key"})
    assert fetched.status_code == 200
    assert fetched.json()["task_counts"]["done"] == 1


@pytest.mark.asyncio
async def test_agent_executor_operating_tools(tmp_path, monkeypatch):
    operating, production, governance, obsidian_projects, agent_executor, client = _modules(tmp_path, monkeypatch)

    plan_raw = await agent_executor._execute_local_tool(
        "create_operating_plan",
        {"title": "Agent plan", "project": "demo", "tasks": [{"title": "First task"}]},
    )
    plan = json.loads(plan_raw)
    assert plan["title"] == "Agent plan"
    assert plan["tasks"][0]["title"] == "First task"

    memory_raw = await agent_executor._execute_local_tool(
        "remember_project_fact",
        {"project": "demo", "content": "Use evidence-led language."},
    )
    assert json.loads(memory_raw)["memory_id"]

    brief_raw = await agent_executor._execute_local_tool("get_operating_daily_brief", {})
    assert "priorities" in json.loads(brief_raw)

    generated_raw = await agent_executor._execute_local_tool(
        "generate_operating_plan",
        {"goal": "Run operational hardening rehearsal before release", "project": "ops"},
    )
    generated = json.loads(generated_raw)
    assert generated["workflow"] == "deployment"
    assert generated["next_action"]["title"].startswith("Confirm deployment scope")

    synced_raw = await agent_executor._execute_local_tool(
        "sync_operating_plan_to_obsidian",
        {"plan_id": generated["plan"]["plan_id"]},
    )
    synced = json.loads(synced_raw)
    assert synced["status"] == "ok"
