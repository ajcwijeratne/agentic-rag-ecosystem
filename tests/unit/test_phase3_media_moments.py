from __future__ import annotations

import importlib
from pathlib import Path

from fastapi.testclient import TestClient


def _reload_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("MEDIA_DERIVED_ROOT", str(tmp_path / "derived"))
    from media import registry

    return importlib.reload(registry)


def test_video_ingestion_records_transcript_and_keyframe_moments(tmp_path, monkeypatch):
    registry = _reload_registry(tmp_path, monkeypatch)
    from media.ingest import video
    from media import whisper_pipeline

    video = importlib.reload(video)
    media_file = tmp_path / "clip.mp4"
    media_file.write_bytes(b"fake video")
    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    frame1 = frame_dir / "clip_kf001.jpg"
    frame2 = frame_dir / "clip_kf002.jpg"
    frame1.write_bytes(b"jpg")
    frame2.write_bytes(b"jpg")

    monkeypatch.setattr(video, "_probe", lambda path: (12.0, "1920x1080"))
    monkeypatch.setattr(video, "_extract_audio", lambda path, out_wav: True)
    monkeypatch.setattr(video, "_keyframes", lambda path, out_dir: ([frame1, frame2], [1.25, 8.5]))
    monkeypatch.setattr(
        whisper_pipeline,
        "transcribe_segments",
        lambda path, language=None: {
            "status": "ok",
            "language": "en",
            "duration": 12.0,
            "text": "Opening idea Closing proof",
            "segments": [
                {"start": 0.0, "end": 3.0, "text": "Opening idea", "speaker": "A"},
                {"start": 7.0, "end": 10.0, "text": "Closing proof", "speaker": "A"},
            ],
        },
    )

    asset_id = registry.add_asset("video", str(media_file), "upload", rights="owned", status="ingesting")
    result = video.enrich(asset_id, str(media_file), language="en")

    assert result["status"] == "ready"
    assert result["segments"] == 2
    assert result["keyframes"] == 2

    moments = registry.list_moments(asset_id)
    assert [m["kind"] for m in moments] == ["transcript", "keyframe", "transcript", "keyframe"]
    assert moments[0]["text"] == "Opening idea"
    assert moments[1]["thumbnail_path"] == str(frame1)
    assert moments[3]["child_asset_id"] in result["children"]


def test_slide_ingestion_records_slide_moments(tmp_path, monkeypatch):
    registry = _reload_registry(tmp_path, monkeypatch)
    from media.ingest import slides

    slides = importlib.reload(slides)
    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"fake deck")
    monkeypatch.setattr(
        slides,
        "_extract_pptx",
        lambda path: [
            {"no": 1, "text": "Market signal", "notes": "Use the recent survey"},
            {"no": 2, "text": "Recommendation", "notes": ""},
        ],
    )

    asset_id = registry.add_asset("slide_deck", str(deck), "upload", rights="licensed", status="ingesting")
    result = slides.enrich(asset_id, str(deck))

    assert result["slide_count"] == 2
    moments = registry.list_moments(asset_id, kind="slide")
    assert [m["label"] for m in moments] == ["Slide 1", "Slide 2"]
    assert "Market signal" in moments[0]["text"]
    assert moments[0]["meta"]["slide_no"] == 1


def test_asset_moments_route_and_asset_search(tmp_path, monkeypatch):
    registry = _reload_registry(tmp_path, monkeypatch)
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    import orchestrator.main as main

    main = importlib.reload(main)
    asset_id = registry.add_asset("web_page", "https://example.test", "web", rights="owned", status="ready")
    registry.add_moment(asset_id, kind="page", label="Example page", text="A useful accreditation checklist")
    client = TestClient(main.app)

    response = client.get(f"/assets/{asset_id}/moments", headers={"x-api-key": "test-key"})
    assert response.status_code == 200
    assert response.json()["items"][0]["kind"] == "page"

    searched = client.get("/assets?q=accreditation", headers={"x-api-key": "test-key"})
    assert searched.status_code == 200
    assert searched.json()["items"][0]["asset_id"] == asset_id
