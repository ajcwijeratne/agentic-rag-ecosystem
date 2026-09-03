"""
Audio ingestion worker.

Transcribes an audio asset to timestamped segments and writes them to the
registry's transcripts table, then sets duration and flips the asset to
`ready`. Embedding the transcript chunks into Qdrant is Phase 2.

Transcription goes through `media.asr`, which gates the audio on voice activity
detection before it reaches a model: silence is dropped, so a recording with
long gaps costs a fraction of the runtime, and the engine is selectable
(whisper | vosk | hybrid). If that layer cannot load — numpy or the VAD
backends missing, say — it falls back to the original whisper_pipeline path, so
ingestion degrades rather than failing.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import registry

# Engine used for ingestion. Whisper by default: ingestion is a batch job, so
# accuracy matters more than the live latency VOSK buys.
INGEST_ASR_ENGINE: str = os.getenv("INGEST_ASR_ENGINE", os.getenv("ASR_ENGINE", "whisper"))
INGEST_USE_VAD: bool = os.getenv("INGEST_USE_VAD", "true").lower() in ("1", "true", "yes")


def _transcriber():
    """
    Return (fn, kind) for the best available transcriber. `fn` takes
    (path, language=...) and returns the registry-shaped dict.
    """
    try:
        from ..asr import transcribe_segments as asr_transcribe

        def _run(path: Path, language: str | None = None) -> dict[str, Any]:
            return asr_transcribe(
                path,
                language=language,
                engine=INGEST_ASR_ENGINE,
                use_vad=INGEST_USE_VAD,
            )

        return _run, "asr"
    except Exception:
        # media.asr unavailable — fall back to the plain Whisper pipeline.
        from ..whisper_pipeline import transcribe_segments as whisper_transcribe

        def _run(path: Path, language: str | None = None) -> dict[str, Any]:
            return whisper_transcribe(path, language=language)

        return _run, "whisper_pipeline"


def enrich(asset_id: str, path: str, *, language: str | None = None) -> dict[str, Any]:
    audio_path = Path(path)
    if not audio_path.exists():
        registry.set_status(asset_id, "failed")
        return {"status": "failed", "detail": "file not found"}

    try:
        transcribe, kind = _transcriber()
    except Exception as exc:                       # faster-whisper missing
        registry.set_status(asset_id, "failed")
        return {"status": "failed", "detail": f"transcriber unavailable: {exc}"}

    try:
        result = transcribe(audio_path, language=language)
    except Exception as exc:                       # model load / decode failure
        registry.set_status(asset_id, "failed")
        return {"status": "failed", "detail": f"transcription failed: {exc}"}

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
        # What actually ran, and how much of the audio was speech.
        "engine":   result.get("engine", kind),
        "speech_s": result.get("speech_s"),
    }
