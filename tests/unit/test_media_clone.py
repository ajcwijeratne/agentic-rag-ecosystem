"""Offline tests for the clone pipeline: providers, workers, and the clone gate."""

from __future__ import annotations

import pytest


@pytest.fixture()
def iso_media(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("MEDIA_DERIVED_ROOT", str(tmp_path / "derived"))
    yield tmp_path


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

def test_api_tools_available_only_with_key(iso_media, monkeypatch):
    from media import tool_registry

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    tool = tool_registry.get_tool("elevenlabs")
    assert tool is not None
    assert tool["capability"] == "voice"
    assert tool["available"] is False

    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    assert tool_registry.get_tool("elevenlabs")["available"] is True

    monkeypatch.setenv("HEYGEN_API_KEY", "k")
    assert tool_registry.get_tool("heygen")["available"] is True


def test_default_tool_env_override(iso_media, monkeypatch):
    from media import tool_registry
    monkeypatch.setenv("MEDIA_TOOL_DEFAULT_VOICE", "elevenlabs")
    assert tool_registry.default_tool_for("voice")["name"] == "elevenlabs"
    monkeypatch.setenv("MEDIA_TOOL_DEFAULT_AVATAR", "heygen")
    assert tool_registry.default_tool_for("avatar")["name"] == "heygen"


# ---------------------------------------------------------------------------
# Voice worker with a faked provider
# ---------------------------------------------------------------------------

def test_voice_worker_elevenlabs_marks_clone(iso_media, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "v-aaron")

    from media.providers import elevenlabs

    def fake_synth(text, out_path, voice_id=None, model_id=None, timeout=120.0):
        from pathlib import Path
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"mp3")
        return {"path": str(p), "voice_id": "v-aaron", "model_id": "m", "bytes": 3, "clone": True}

    monkeypatch.setattr(elevenlabs, "synthesize", fake_synth)

    from media.generate.dispatcher import generate
    result = generate({
        "job_id": "j1", "capability": "voice", "tool": "elevenlabs",
        "brief": {"text": "Hello from the clone"}, "meta": {},
    })
    d = result.to_dict()
    assert d["status"] == "completed"
    assert d["meta"]["clone"] is True

    from media import registry
    asset = registry.get_asset(d["asset_id"], with_relations=False)
    assert asset["meta"]["clone"] is True
    assert asset["meta"]["provider"] == "elevenlabs"


def test_avatar_worker_heygen_marks_clone(iso_media, monkeypatch):
    monkeypatch.setenv("HEYGEN_API_KEY", "k")
    monkeypatch.setenv("HEYGEN_AVATAR_ID", "a-aaron")

    from media.providers import heygen

    def fake_pipeline(out_path, avatar_id=None, audio_path=None, text=None, poll_timeout=900.0):
        from pathlib import Path
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"mp4")
        return {"path": str(p), "video_id": "vid1", "clone": True, "avatar_id": "a-aaron"}

    monkeypatch.setattr(heygen, "generate_and_download", fake_pipeline)

    from media.generate.dispatcher import generate
    result = generate({
        "job_id": "j2", "capability": "avatar", "tool": "heygen",
        "brief": {"text": "Sixty seconds on TEQSA"}, "meta": {},
    })
    d = result.to_dict()
    assert d["status"] == "completed"
    assert d["meta"]["clone"] is True

    from media import registry
    asset = registry.get_asset(d["asset_id"], with_relations=False)
    assert asset["meta"]["clone"] is True
    assert asset["meta"]["heygen_video_id"] == "vid1"


# ---------------------------------------------------------------------------
# Clone gate
# ---------------------------------------------------------------------------

def _make_production_with_clone_asset(clone: bool):
    from media import registry
    from orchestrator import production as prod_store

    pid = prod_store.create_production("Clone gate test", "test", "talking_head_clip")
    meta = {"clone": True, "provider": "heygen"} if clone else {}
    asset_id = registry.add_asset("video", "/tmp/x.mp4", "derived",
                                  status="ready", meta=meta)
    prod_store.update_production(pid, linked_assets=[asset_id])
    return prod_store.get_production(pid)


def test_clone_gate_blocks_review_and_publish(iso_media):
    from orchestrator import governance
    prod = _make_production_with_clone_asset(clone=True)

    assert "clone_output" in governance.required_for_transition(prod, "review")
    assert "clone_output" in governance.required_for_transition(prod, "publish")
    assert "clone_output" not in governance.required_for_transition(prod, "render")

    pending = governance.pending_gates(prod, "review")
    gates = {p["gate"] for p in pending}
    assert "clone_output" in gates

    # Approval opens the gate.
    governance.approve("clone_output", prod["production_id"], actor="test")
    pending_after = governance.pending_gates(prod, "review")
    assert "clone_output" not in {p["gate"] for p in pending_after}


def test_no_clone_asset_no_clone_gate(iso_media):
    from orchestrator import governance
    prod = _make_production_with_clone_asset(clone=False)
    assert "clone_output" not in governance.required_for_transition(prod, "review")
    assert "clone_output" not in governance.required_for_transition(prod, "publish")
