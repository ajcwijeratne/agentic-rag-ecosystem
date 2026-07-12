from __future__ import annotations

import importlib

import pytest


def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("MEDIA_DERIVED_ROOT", str(tmp_path / "derived"))
    monkeypatch.setenv("REMOTION_DIR", str(tmp_path / "missing-remotion"))
    from media import registry, render
    from orchestrator import production

    registry = importlib.reload(registry)
    render = importlib.reload(render)
    production = importlib.reload(production)
    return registry, render, production


def test_build_props_extracts_lines_scenes_captions_and_assets(tmp_path, monkeypatch):
    registry, render, production = _env(tmp_path, monkeypatch)
    prod = {
        "production_id": "prod-1",
        "title": "Retention beats recruitment",
        "project": "demo",
        "format": "linkedin_short",
        "owner": "Aaron",
        "script": {
            "outline": {"hook": "Retention is the margin.", "proof": "Recruitment costs more."},
            "draft": ["Open with the gap.", "Name the fix."],
        },
        "asset_plan": {
            "scenes": [
                {"scene_id": "s1", "title": "Gap", "text": "Show the leakage.", "asset_id": "asset-1"},
                {"scene_id": "s2", "needs_generation": True, "visual_brief": {"prompt": "dashboard"}},
            ]
        },
        "edit_plan": {"captions": ["Retention first.", "Then recruitment."]},
        "linked_assets": ["asset-1"],
    }

    props = render.build_props(prod)

    assert props["production_id"] == "prod-1"
    assert props["lines"][:2] == ["Retention is the margin.", "Recruitment costs more."]
    assert props["scenes"][0]["asset_id"] == "asset-1"
    assert props["scenes"][1]["needs_generation"] is True
    assert props["captions"] == ["Retention first.", "Then recruitment."]
    assert props["brand"]["name"] == "WijerCo"


def test_render_prepares_placeholder_and_links_sources(tmp_path, monkeypatch):
    registry, render, production = _env(tmp_path, monkeypatch)
    source = registry.add_asset("image", "media_input/source.png", "upload", rights="owned", status="ready")

    result = render.render(
        "prod-2",
        "linkedin_short",
        {"title": "Launch", "linked_assets": [source], "project": "demo"},
    )

    assert result["status"] == "prepared"
    asset = registry.get_asset(result["asset_id"])
    assert asset["type"] == "video"
    assert asset["source"] == "derived"
    assert asset["meta"]["template"] == "linkedin_short"
    assert asset["parents"][0]["linked_asset_id"] == source


def test_render_rejects_unknown_template(tmp_path, monkeypatch):
    registry, render, production = _env(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="template must be one of"):
        render.render("prod-3", "not_a_template", {})


@pytest.mark.asyncio
async def test_production_render_step_uses_render_props_contract(tmp_path, monkeypatch):
    registry, render, production = _env(tmp_path, monkeypatch)

    async def fake_agent(**kwargs):
        slug = kwargs["subagent"]
        if slug == "editor":
            return {"answer": '{"captions":["Caption one"],"cut_list":[]}'}
        return {"answer": f'{{"{slug.replace("-", "_")}":"ok"}}'}

    monkeypatch.setattr(production, "call_wijerco_agent", fake_agent)
    pid = production.create_production("Course teaser", "demo", "course_teaser", "Aaron")
    production.update_production(
        pid,
        script={"draft": ["This is the draft line."]},
        asset_plan={"scenes": [{"scene_id": "s1", "title": "Opening", "text": "Show the promise."}]},
    )
    production.transition(pid, "asset_plan", "test", "ready to render")

    result = await production.advance(pid, actor="test")

    assert result["production"]["state"] == "render"
    render_output = result["agent_output"][-1]["output"]
    assert render_output["status"] == "prepared"
    asset = registry.get_asset(render_output["asset_id"])
    assert asset["meta"]["template"] == "course_teaser"
