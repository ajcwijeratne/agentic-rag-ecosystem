from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("MEDIA_DERIVED_ROOT", str(tmp_path / "derived"))
    monkeypatch.setenv("REMOTION_DIR", str(tmp_path / "missing-remotion"))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    from orchestrator import dashboard, governance, operating, production
    import orchestrator.main as main

    dashboard = importlib.reload(dashboard)
    governance = importlib.reload(governance)
    operating = importlib.reload(operating)
    production = importlib.reload(production)
    main = importlib.reload(main)
    return TestClient(main.app), main, production


def test_production_routes_create_list_get_transition_and_board(tmp_path, monkeypatch):
    client, main, production = _client(tmp_path, monkeypatch)

    created = client.post(
        "/production",
        headers={"x-api-key": "admin-key"},
        json={
            "title": "Retention short",
            "project": "demo",
            "format": "linkedin_short",
            "owner": "Aaron",
        },
    )

    assert created.status_code == 200
    pid = created.json()["production_id"]
    assert created.json()["state"] == "idea"

    listed = client.get("/production?project=demo", headers={"x-api-key": "test-key"})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["production_id"] == pid

    fetched = client.get(f"/production/{pid}", headers={"x-api-key": "test-key"})
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Retention short"

    moved = client.post(
        f"/production/{pid}/transition",
        headers={"x-api-key": "admin-key"},
        json={"to_state": "draft", "note": "manual move", "actor": "test"},
    )
    assert moved.status_code == 200
    assert moved.json()["state"] == "draft"
    assert moved.json()["events"][0]["note"] == "manual move"

    board = client.get("/production/board", headers={"x-api-key": "test-key"})
    assert board.status_code == 200
    assert board.json()["Drafting"][0]["production_id"] == pid


def test_production_advance_route_returns_agent_output(tmp_path, monkeypatch):
    client, main, production = _client(tmp_path, monkeypatch)
    pid = production.create_production("Advance me", "demo", "course_teaser", "Aaron")

    async def fake_advance(production_id: str, actor: str = "operator"):
        prod = production.transition(production_id, "brief", actor, "stub advance")
        return {
            "production": prod,
            "agent_output": [
                {"subagent": "brief-builder", "field": "brief", "output": {"working_title": "Advance me"}}
            ],
        }

    monkeypatch.setattr(main.production_store, "advance", fake_advance)

    response = client.post(
        f"/production/{pid}/advance?actor=tester",
        headers={"x-api-key": "admin-key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["production"]["state"] == "brief"
    assert payload["agent_output"][0]["subagent"] == "brief-builder"


def test_production_action_route_runs_named_workflow_step(tmp_path, monkeypatch):
    client, main, production = _client(tmp_path, monkeypatch)
    pid = production.create_production("Action me", "demo", "linkedin_short", "Aaron")

    async def fake_agent(**kwargs):
        return {"answer": '{"working_title":"Action me"}'}

    monkeypatch.setattr(production, "call_wijerco_agent", fake_agent)

    response = client.post(
        f"/production/{pid}/action",
        headers={"x-api-key": "admin-key"},
        json={"action": "brief", "actor": "tester"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"]["key"] == "brief"
    assert payload["production"]["state"] == "brief"
    assert payload["agent_output"][0]["subagent"] == "brief-builder"
    assert payload["production"]["brief"]["working_title"] == "Action me"


def test_production_action_route_rejects_wrong_stage(tmp_path, monkeypatch):
    client, main, production = _client(tmp_path, monkeypatch)
    pid = production.create_production("Wrong stage", "demo", "linkedin_short", "Aaron")

    response = client.post(
        f"/production/{pid}/action",
        headers={"x-api-key": "admin-key"},
        json={"action": "draft", "actor": "tester"},
    )

    assert response.status_code == 422
    assert "can only run from outline" in response.json()["detail"]


def test_production_handoff_requires_published_state(tmp_path, monkeypatch):
    client, main, production = _client(tmp_path, monkeypatch)
    pid = production.create_production("Too early", "demo", "linkedin_short", "Aaron")

    response = client.post(
        f"/production/{pid}/handoff",
        headers={"x-api-key": "admin-key"},
        json={"actor": "tester"},
    )

    assert response.status_code == 422
    assert "must be published" in response.json()["detail"]


def test_production_handoff_creates_deliverable_and_memory(tmp_path, monkeypatch):
    client, main, production = _client(tmp_path, monkeypatch)
    pid = production.create_production("Finished piece", "demo", "linkedin_short", "Aaron")
    production.transition(pid, "publish", "test", "published")

    response = client.post(
        f"/production/{pid}/handoff",
        headers={"x-api-key": "admin-key"},
        json={"actor": "tester", "note": "Ready for library"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["deliverable"]["item"]["production_id"] == pid
    deliverable_path = tmp_path / "vault" / payload["deliverable"]["path"]
    assert deliverable_path.is_file()
    text = deliverable_path.read_text(encoding="utf-8")
    assert "Production Handoff" in text
    assert pid in text
    assert payload["memory"]["memory_id"]
    assert payload["production"]["events"][-1]["note"].startswith("deliverable handoff:")


def test_production_intelligence_recommends_next_best_and_gaps(tmp_path, monkeypatch):
    client, main, production = _client(tmp_path, monkeypatch)
    first = production.create_production("Idea ready", "demo", "linkedin_short", "Aaron")
    second = production.create_production("Needs assets", "demo", "talking_head_clip", "Aaron")
    production.transition(second, "draft", "test", "manual")

    response = client.get("/production/intelligence", headers={"x-api-key": "test-key"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["active"] == 2
    assert payload["next_best"]["production_id"] == second
    assert any(item["production_id"] == second for item in payload["weak_evidence"])
    assert any(item["production_id"] == second for item in payload["asset_gaps"])
    assert payload["daily_run"]


def test_production_intelligence_prioritises_blocked_when_no_ready_actions(tmp_path, monkeypatch):
    client, main, production = _client(tmp_path, monkeypatch)
    pid = production.create_production("Blocked one", "demo", "linkedin_short", "Aaron")
    production.transition(pid, "review", "test", "ready")

    response = client.get("/production/intelligence", headers={"x-api-key": "test-key"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["blocked"] == 1
    assert payload["next_best"]["production_id"] == pid
    assert payload["daily_run"][0]["type"] == "approval"


def test_production_intelligence_surfaces_media_ready_asset_plans(tmp_path, monkeypatch):
    client, main, production = _client(tmp_path, monkeypatch)
    pid = production.create_production("Media ready", "demo", "talking_head_clip", "Aaron")
    production.transition(pid, "asset_plan", "test", "assets planned")
    production.update_production(pid, asset_plan={
        "scenes": [{"scene_id": "s1", "visual_brief": {"prompt": "warm studio image"}}],
    })

    response = client.get("/production/intelligence", headers={"x-api-key": "test-key"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["ready_to_generate"] == 1
    assert payload["ready_to_generate"][0]["production_id"] == pid
    assert payload["ready_to_generate"][0]["asset_status"] == "planned"
    assert any(item["type"] == "media" for item in payload["daily_run"])
