from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("MEDIA_DERIVED_ROOT", str(tmp_path / "derived"))
    monkeypatch.setenv("REMOTION_DIR", str(tmp_path / "missing-remotion"))
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("MEDIA_TOOL_FFMPEG_COMMAND", "python")

    from media import registry, render, tool_registry
    from media.generate import common, dispatcher, plan, video
    from orchestrator import production, production_media
    import orchestrator.main as main

    for module in (registry, render, tool_registry, common, plan, video, dispatcher, production, production_media, main):
        importlib.reload(module)
    return TestClient(main.app), production, registry


def test_media_tools_route_lists_defaults(tmp_path, monkeypatch):
    client, production, registry = _client(tmp_path, monkeypatch)

    response = client.get("/media/tools", headers={"x-api-key": "test-key"})

    assert response.status_code == 200
    names = {item["name"] for item in response.json()["items"]}
    assert "ffmpeg" in names
    assert "comfyui" in names


def test_media_generate_route_blocks_missing_image_endpoint(tmp_path, monkeypatch):
    client, production, registry = _client(tmp_path, monkeypatch)

    response = client.post(
        "/media/generate",
        headers={"x-api-key": "admin-key"},
        json={"capability": "image", "brief": {"prompt": "x"}},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "blocked"


def test_production_generate_route_links_asset(tmp_path, monkeypatch):
    client, production, registry = _client(tmp_path, monkeypatch)
    pid = production.create_production("Launch", "demo", "linkedin_short", "Aaron")

    response = client.post(
        f"/production/{pid}/generate",
        headers={"x-api-key": "admin-key"},
        json={"capability": "video"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["generation"]["status"] == "completed"
    asset_id = payload["generation"]["asset_id"]
    assert asset_id in payload["production"]["linked_assets"]
    assert registry.get_asset(asset_id)["type"] == "video"


def test_production_generate_plan_dry_run(tmp_path, monkeypatch):
    client, production, registry = _client(tmp_path, monkeypatch)
    pid = production.create_production("Plan", "demo", "linkedin_short", "Aaron")
    production.update_production(pid, asset_plan={
        "scenes": [{"scene_id": "s1", "visual_brief": {"prompt": "warm studio image"}}],
    })

    response = client.post(
        f"/production/{pid}/generate-plan",
        headers={"x-api-key": "admin-key"},
        json={"dry_run": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["jobs"][0]["capability"] == "image"
    assert payload["results"] == []


def test_production_generate_plan_runs_and_links_assets(tmp_path, monkeypatch):
    client, production, registry = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("MEDIA_TOOL_COMFYUI_ENDPOINT", "http://comfy.test")
    pid = production.create_production("Plan", "demo", "linkedin_short", "Aaron")
    production.update_production(pid, asset_plan={
        "scenes": [{"scene_id": "s1", "visual_brief": {"prompt": "warm studio image"}}],
    })

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"path": "C:/tmp/generated-plan.png"}

    monkeypatch.setattr("media.generate.image.httpx.post", lambda *args, **kwargs: FakeResponse())

    response = client.post(
        f"/production/{pid}/generate-plan",
        headers={"x-api-key": "admin-key"},
        json={"capabilities": ["image"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["results"][0]["status"] == "completed"
    asset_id = payload["results"][0]["asset_id"]
    assert asset_id in payload["production"]["linked_assets"]
    asset = registry.get_asset(asset_id)
    assert asset["meta"]["production_id"] == pid
    assert asset["meta"]["governance"]["review_status"] == "pending"
    assert asset["meta"]["governance"]["gate"] == "generated_image"
