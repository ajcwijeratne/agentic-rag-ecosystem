from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def generation_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("MEDIA_DERIVED_ROOT", str(tmp_path / "derived"))
    monkeypatch.setenv("REMOTION_DIR", str(tmp_path / "missing-remotion"))
    from media import registry, render, tool_registry
    from media.generate import animation, avatar, common, dispatcher, image, video, voice

    for module in (registry, render, tool_registry, common, image, voice, avatar, animation, video, dispatcher):
        importlib.reload(module)
    return registry, tool_registry, dispatcher


def test_image_worker_registers_returned_output(generation_env, monkeypatch):
    registry, tool_registry, dispatcher = generation_env
    monkeypatch.setenv("MEDIA_TOOL_COMFYUI_ENDPOINT", "http://comfy.test")

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"path": "C:/tmp/generated.png", "prompt_id": "p-1"}

    def fake_post(url, json, timeout):
        assert url == "http://comfy.test/prompt"
        assert json["prompt"] == "studio product shot"
        return FakeResponse()

    monkeypatch.setattr("media.generate.image.httpx.post", fake_post)
    result = dispatcher.generate_dict({
        "capability": "image",
        "brief": {"prompt": "studio product shot"},
        "rights": "owned",
    })

    assert result["status"] == "completed"
    asset = registry.get_asset(result["asset_id"])
    assert asset["type"] == "image"
    assert asset["meta"]["tool"] == "comfyui"


def test_image_worker_blocks_when_endpoint_missing(generation_env):
    registry, tool_registry, dispatcher = generation_env
    result = dispatcher.generate_dict({"capability": "image", "brief": {"prompt": "x"}})

    assert result["status"] == "blocked"
    assert "not available" in result["error"]


def test_voice_worker_blocks_without_voice_model(generation_env, monkeypatch):
    registry, tool_registry, dispatcher = generation_env
    monkeypatch.setenv("MEDIA_TOOL_PIPER_COMMAND", "python")

    result = dispatcher.generate_dict({"capability": "voice", "brief": {"text": "Hello"}})

    assert result["status"] == "blocked"
    assert "voice_model" in result["error"]


def test_animation_worker_writes_plan_when_scene_missing(generation_env, monkeypatch):
    registry, tool_registry, dispatcher = generation_env
    monkeypatch.setenv("MEDIA_TOOL_MANIM_COMMAND", "python")

    result = dispatcher.generate_dict({
        "capability": "animation",
        "brief": {"prompt": "animate the three-step process"},
        "production_id": "prod-1",
    })

    assert result["status"] == "completed"
    assert result["path"].endswith(".plan.json")
    assert result["meta"]["prepared"] is True


def test_video_worker_uses_render_service_placeholder(generation_env, monkeypatch):
    registry, tool_registry, dispatcher = generation_env
    monkeypatch.setenv("MEDIA_TOOL_FFMPEG_COMMAND", "python")

    result = dispatcher.generate_dict({
        "capability": "video",
        "production_id": "prod-2",
        "brief": {"template": "linkedin_short", "props": {"title": "Launch"}},
        "rights": "owned",
    })

    assert result["status"] == "completed"
    assert result["meta"]["status"] == "prepared"
    asset = registry.get_asset(result["asset_id"])
    assert asset["type"] == "video"


def test_plan_extracts_scene_generation_jobs():
    from media.generate.plan import extract_generation_jobs

    production = {
        "production_id": "prod-1",
        "project": "demo",
        "format": "linkedin_short",
        "linked_assets": ["asset-1"],
        "asset_plan": {
            "scenes": [
                {
                    "scene_id": "s1",
                    "needs_generation": True,
                    "visual_brief": {"prompt": "clean office visual"},
                },
                {
                    "scene_id": "s2",
                    "voice_brief": {"text": "Narrate this section", "voice_model": "voice.onnx"},
                },
            ],
        },
    }

    jobs = extract_generation_jobs(production)

    assert [job["capability"] for job in jobs] == ["image", "voice"]
    assert jobs[0]["brief"]["scene_id"] == "s1"
    assert jobs[0]["source_assets"] == ["asset-1"]
