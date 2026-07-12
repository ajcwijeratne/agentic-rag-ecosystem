"""
Audio ingestion worker.

Transcribes an audio asset to timestamped segments and writes them to the
registry's transcripts table, then sets duration and flips the asset to
`ready`. Embedding the transcript chunks into Qdrant is Phase 2.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import registry


def enrich(asset_id: str, path: str, *, language: str | None = None) -> dict[str, Any]:
    audio_path = Path(path)
    if not audio_path.exists():
        registry.set_status(asset_id, "failed")
        return {"status": "failed", "detail": "file not found"}

    try:
        from ..whisper_pipeline import transcribe_segments
    except Exception as exc:                       # faster-whisper missing
        registry.set_status(asset_id, "failed")
        return {"status": "failed", "detail": f"transcriber unavailable: {exc}"}

    result = transcribe_segments(audio_path, language=language)
    if result.get("status") != "ok":
        registry.set_status(asset_id, "failed")
        return {"status": "failed", "detail": result.get("message", "transcription failed")}

    registry.add_transcript(
        asset_id,
        language=result.get("language"),
        segments=result.get("segments", []),
        text=result.get("text", ""),
    )
    registry.update_asset(asset_id, duration=result.get("duration"), status="ready")
    return {
        "status":   "ready",
        "language": result.get("language"),
        "duration": result.get("duration"),
        "segments": len(result.get("segments", [])),
    }
