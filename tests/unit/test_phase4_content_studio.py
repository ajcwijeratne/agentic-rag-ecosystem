from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


CONTENT_STUDIO_SLUGS = [
    "brief-builder",
    "research-producer",
    "scriptwriter",
    "storyboarder",
    "visual-director",
    "editor",
    "qa-brand-reviewer",
]


def _client(monkeypatch):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    import orchestrator.main as main

    main = importlib.reload(main)
    return TestClient(main.app), main


def test_content_studio_is_compatibility_capability_not_department():
    from orchestrator.wijerco_roster import get_roster, lookup_subagent

    roster = get_roster()
    assert "content_studio" not in {d["key"] for d in roster["departments"]}
    assert all(lookup_subagent(slug) for slug in CONTENT_STUDIO_SLUGS)


def test_hybrid_routes_each_content_studio_subagent(monkeypatch):
    client, main = _client(monkeypatch)
    calls = []

    async def fake_agent(**kwargs):
        calls.append(kwargs)
        return {
            "answer": f"ran {kwargs['subagent']}",
            "department": kwargs["department"],
            "model": "stub",
            "cost_usd": 0.0,
            "error": None,
        }

    monkeypatch.setattr(main, "call_wijerco_agent", fake_agent)

    for slug in CONTENT_STUDIO_SLUGS:
        response = client.post(
            "/hybrid",
            headers={"x-api-key": "test-key"},
            json={
                "query": "Advance this production record.",
                "force_route": "content_studio",
                "subagent": slug,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["department"] in {"marketing_sales", "research_intelligence", "operations"}
        assert payload["subagent"] == slug
        assert payload["answer"] == f"ran {slug}"

    assert [call["subagent"] for call in calls] == CONTENT_STUDIO_SLUGS
    assert all(call["department"] in {"marketing_sales", "research_intelligence", "operations"} for call in calls)


def test_wijerco_endpoint_validates_subagent_department(monkeypatch):
    client, main = _client(monkeypatch)

    response = client.post(
        "/wijerco",
        headers={"x-api-key": "test-key"},
        json={
            "query": "Write the script.",
            "department": "operations",
            "subagent": "scriptwriter",
        },
    )

    assert response.status_code == 422
    assert "belongs to marketing_sales" in response.json()["detail"]


def test_wijerco_endpoint_passes_named_agent(monkeypatch):
    client, main = _client(monkeypatch)

    async def fake_agent(**kwargs):
        return {
            "answer": "brief ready",
            "department": kwargs["department"],
            "model": "stub",
            "tokens_used": 0,
            "error": None,
        }

    monkeypatch.setattr(main, "call_wijerco_agent", fake_agent)

    response = client.post(
        "/wijerco",
        headers={"x-api-key": "test-key"},
        json={"query": "Build a brief.", "subagent": "brief-builder"},
    )

    assert response.status_code == 200
    assert response.json()["department"] == "marketing_sales"
    assert response.json()["subagent"] == "brief-builder"


def test_content_studio_prompt_loads_department_and_role(tmp_path, monkeypatch):
    root = tmp_path / "WijerCo"
    (root / "ABOUT ME").mkdir(parents=True)
    (root / "AGENTS" / "departments").mkdir(parents=True)
    (root / "AGENTS" / "subagents").mkdir(parents=True)
    (root / "KNOWLEDGE BASE").mkdir(parents=True)
    (root / "ABOUT ME" / "about-me.md").write_text("Aaron voice.", encoding="utf-8")
    (root / "ABOUT ME" / "anti-ai-writing-style.md").write_text("No filler.", encoding="utf-8")
    (root / "ABOUT ME" / "my-company.md").write_text("WijerCo context.", encoding="utf-8")
    (root / "AGENTS" / "departments" / "content-studio.md").write_text("Content Studio department.", encoding="utf-8")
    (root / "AGENTS" / "subagents" / "brief-builder.md").write_text(
        "---\nname: Brief Builder\n---\nReturn a structured brief.",
        encoding="utf-8",
    )
    (root / "KNOWLEDGE BASE" / "wijerco-services.md").write_text("Services.", encoding="utf-8")
    (root / "KNOWLEDGE BASE" / "wijerco-positioning.md").write_text("Positioning.", encoding="utf-8")
    monkeypatch.setenv("WIJERCO_PATH", str(root))

    import orchestrator.wijerco_agent as agent

    agent = importlib.reload(agent)
    prompt = agent._build_system_prompt("content_studio", subagent="brief-builder")

    assert "Content Studio department." in prompt
    assert "Return a structured brief." in prompt
    assert "name: Brief Builder" not in prompt
