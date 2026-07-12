"""Offline tests for the free clone pipeline: F5-TTS + SadTalker workers."""

from __future__ import annotations

import pytest


@pytest.fixture()
def iso_media(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("MEDIA_DERIVED_ROOT", str(tmp_path / "derived"))
    yield tmp_path


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_f5_tts_tool_available_with_url(iso_media, monkeypatch):
    from media import tool_registry
    monkeypatch.delenv("F5_TTS_URL", raising=False)
    tool = tool_registry.get_tool("f5-tts")
    assert tool is not None
    assert tool["capability"] == "voice"
    assert tool["commercial_safe"] is True
    assert tool["available"] is False

    monkeypatch.setenv("F5_TTS_URL", "http://gpu-pc:8020")
    tool = tool_registry.get_tool("f5-tts")
    assert tool["available"] is True
    assert tool["endpoint"] == "http://gpu-pc:8020"


def test_voice_http_branch_marks_clone(iso_media, monkeypatch, tmp_path):
    monkeypatch.setenv("F5_TTS_URL", "http://gpu-pc:8020")
    out = tmp_path / "voice-x.wav"
    out.write_bytes(b"wav")

    import httpx

    def fake_post(url, json=None, timeout=None):
        assert url == "http://gpu-pc:8020/generate"
        assert json["text"] == "Hello in my own voice"
        return FakeResponse({"path": str(out), "engine": "f5-tts", "clone": True})

    monkeypatch.setattr(httpx, "post", fake_post)

    from media.generate.dispatcher import generate
    result = generate({
        "job_id": "jf1", "capability": "voice", "tool": "f5-tts",
        "brief": {"text": "Hello in my own voice"}, "meta": {},
    })
    d = result.to_dict()
    assert d["status"] == "completed"
    assert d["meta"]["clone"] is True
    assert d["meta"]["engine"] == "f5-tts"

    from media import registry
    asset = registry.get_asset(d["asset_id"], with_relations=False)
    assert asset["meta"]["clone"] is True
    assert asset["meta"]["provider"] == "f5-tts"


def test_voice_http_branch_fails_without_path(iso_media, monkeypatch):
    monkeypatch.setenv("F5_TTS_URL", "http://gpu-pc:8020")
    import httpx
    monkeypatch.setattr(httpx, "post",
                        lambda url, json=None, timeout=None: FakeResponse({"error": "no gpu"}))

    from media.generate.dispatcher import generate
    result = generate({
        "job_id": "jf2", "capability": "voice", "tool": "f5-tts",
        "brief": {"text": "hello"}, "meta": {},
    })
    assert result.to_dict()["status"] == "failed"


def test_sadtalker_http_output_marks_clone(iso_media, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_TOOL_SADTALKER_ENDPOINT", "http://gpu-pc:7861")
    portrait = tmp_path / "aaron.jpg"
    portrait.write_bytes(b"jpg")
    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"wav")
    video = tmp_path / "avatar.mp4"
    video.write_bytes(b"mp4")

    import httpx
    monkeypatch.setattr(
        httpx, "post",
        lambda url, json=None, timeout=None: FakeResponse(
            {"path": str(video), "engine": "sadtalker", "clone": True}),
    )

    from media.generate.dispatcher import generate
    result = generate({
        "job_id": "jf3", "capability": "avatar", "tool": "sadtalker",
        "brief": {"portrait_path": str(portrait), "audio_path": str(audio)},
        "meta": {},
    })
    d = result.to_dict()
    assert d["status"] == "completed"

    from media import registry
    asset = registry.get_asset(d["asset_id"], with_relations=False)
    assert asset["meta"]["clone"] is True
    assert asset["meta"]["provider"] == "sadtalker"


def test_free_clone_assets_trigger_clone_gate(iso_media, monkeypatch, tmp_path):
    """The clone gate is engine-independent: free outputs gate the same way."""
    from media import registry
    from orchestrator import production as prod_store
    from orchestrator import governance

    pid = prod_store.create_production("Free clone gate", "test", "talking_head_clip")
    asset_id = registry.add_asset("video", str(tmp_path / "v.mp4"), "derived",
                                  status="ready",
                                  meta={"clone": True, "provider": "sadtalker"})
    prod_store.update_production(pid, linked_assets=[asset_id])
    prod = prod_store.get_production(pid)

    assert "clone_output" in governance.required_for_transition(prod, "review")
