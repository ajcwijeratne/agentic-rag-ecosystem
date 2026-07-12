from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    from media import tool_registry

    importlib.reload(tool_registry)
    return tool_registry


def test_default_tools_bootstrap(registry):
    tools = registry.list_tools()
    names = {tool["name"] for tool in tools}

    assert {"ffmpeg", "comfyui", "piper", "musetalk", "manim", "blender"} <= names
    assert registry.default_tool_for("video")["name"] == "ffmpeg"
    assert registry.default_tool_for("image")["name"] == "comfyui"


def test_env_overrides_endpoint_and_enabled(registry, monkeypatch):
    monkeypatch.setenv("MEDIA_TOOL_COMFYUI_ENDPOINT", "http://127.0.0.1:8188")
    monkeypatch.setenv("MEDIA_TOOL_COMFYUI_ENABLED", "0")

    tool = registry.get_tool("comfyui")

    assert tool["endpoint"] == "http://127.0.0.1:8188"
    assert tool["enabled"] is False
    assert tool["available"] is True


def test_require_tool_blocks_disabled_and_non_commercial(registry):
    assert registry.set_tool_enabled("ffmpeg", False) is True

    with pytest.raises(RuntimeError, match="disabled"):
        registry.require_tool("video")

    with pytest.raises(RuntimeError, match="license approval"):
        registry.require_tool("image", require_commercial_safe=True)


def test_gateway_consults_registry(registry, monkeypatch):
    from media.adapters import gateway

    importlib.reload(gateway)
    registry.set_tool_enabled("ffmpeg", False)

    with pytest.raises(RuntimeError, match="disabled"):
        gateway.select("video")


def test_generation_contract_round_trip():
    from media.generate import GenerationJob, GenerationResult

    job = GenerationJob(
        capability="image",
        production_id="prod-1",
        brief={"prompt": "clean product mockup"},
        source_assets=["asset-1"],
    )
    loaded = GenerationJob.from_dict(job.to_dict())
    result = GenerationResult.completed(
        loaded,
        tool="comfyui",
        asset_id="asset-2",
        path="media_derived/prod-1/image.png",
        license_status="pending_review",
    )

    assert loaded.job_id == job.job_id
    assert result.to_dict()["status"] == "completed"
    assert result.to_dict()["asset_id"] == "asset-2"
