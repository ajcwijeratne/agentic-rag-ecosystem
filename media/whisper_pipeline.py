"""
Audio Transcription Pipeline — Faster-Whisper
===============================================
Transcribes audio files to Markdown text and writes output
into the local folder hierarchy.

Features:
  • Automatic language detection
  • Speaker diarization placeholder (timestamp segments)
  • Outputs to /transcripts/<original_stem>.md
  • REST endpoint for the orchestrator to trigger transcription jobs

Dependencies:
  pip install faster-whisper

Usage:
  # CLI
  python -m media.whisper_pipeline --file audio.mp3 --output ./transcripts

  # As service
  python -m media.whisper_pipeline --serve
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Literal

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from common.security import require_api_key, cors_kwargs, bind_host, confine_to_roots

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WHISPER_MODEL_SIZE: str = os.getenv("WHISPER_MODEL", "base")   # tiny, base, small, medium, large-v2
WHISPER_DEVICE: str     = os.getenv("WHISPER_DEVICE", "cpu")   # cpu | cuda | mps
WHISPER_COMPUTE: str    = os.getenv("WHISPER_COMPUTE", "int8") # int8 | float16 | float32
OUTPUT_DIR: Path        = Path(os.getenv("TRANSCRIPT_OUTPUT_DIR", "./transcripts"))
PORT: int               = int(os.getenv("WHISPER_PORT", "8007"))

# Transcription is confined to these roots. Audio may come from the media input
# root or a produced file under the transcript output root; transcripts must be
# written under the output root. Anything else is rejected (403).
INPUT_ROOT: Path  = Path(os.getenv("MEDIA_INPUT_ROOT", "./media_input"))
_INPUT_ROOTS  = [INPUT_ROOT, OUTPUT_DIR]
_OUTPUT_ROOTS = [OUTPUT_DIR]


# ---------------------------------------------------------------------------
# Lazy model loader
# ---------------------------------------------------------------------------

_model = None

def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        print(f"[whisper] Loading model '{WHISPER_MODEL_SIZE}' on {WHISPER_DEVICE} ({WHISPER_COMPUTE})...")
        _model = WhisperModel(
            WHISPER_MODEL_SIZE,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE,
        )
        print("[whisper] Model loaded.")
    return _model


# ---------------------------------------------------------------------------
# Core transcription function
# ---------------------------------------------------------------------------

def transcribe(
    audio_path: Path,
    output_dir: Path = OUTPUT_DIR,
    language: str | None = None,
) -> dict[str, Any]:
    """
    Transcribe an audio file.
    Returns metadata + writes a Markdown transcript file.
    """
    if not audio_path.exists():
        return {"status": "error", "message": f"File not found: {audio_path}"}

    output_dir.mkdir(parents=True, exist_ok=True)
    model = get_model()

    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=5,
        word_timestamps=True,
    )

    lines: list[str] = [
        f"# Transcript: {audio_path.name}",
        f"",
        f"**Language detected:** {info.language} (probability: {info.language_probability:.2f})",
        f"**Duration:** {info.duration:.1f}s",
        f"",
        f"---",
        f"",
    ]

    full_text_parts: list[str] = []

    for seg in segments:
        start  = f"{seg.start:07.2f}s"
        end    = f"{seg.end:07.2f}s"
        text   = seg.text.strip()
        lines.append(f"**[{start} → {end}]** {text}")
        lines.append("")
        full_text_parts.append(text)

    full_text = " ".join(full_text_parts)

    out_file = output_dir / f"{audio_path.stem}.md"
    out_file.write_text("\n".join(lines), encoding="utf-8")

    print(f"[whisper] Transcript saved: {out_file}")
    return {
        "status":          "ok",
        "input_file":      str(audio_path),
        "output_file":     str(out_file),
        "language":        info.language,
        "duration_s":      round(info.duration, 2),
        "word_count":      len(full_text.split()),
        "transcript_preview": full_text[:500],
    }


# ---------------------------------------------------------------------------
# Structured transcription (for the Media Asset Registry)
# ---------------------------------------------------------------------------

def transcribe_segments(
    audio_path: Path,
    language: str | None = None,
) -> dict[str, Any]:
    """
    Transcribe to a structured result the registry can store directly:
    language, duration, full text, and per-segment timestamps.

    `speaker` is a diarization placeholder (None) until a diariser is added.
    Unlike transcribe(), this writes no Markdown file; the ingestion worker
    owns persistence.
    """
    if not audio_path.exists():
        return {"status": "error", "message": f"File not found: {audio_path}"}

    model = get_model()
    segments, info = model.transcribe(
        str(audio_path),
        language=language,
        beam_size=5,
        word_timestamps=True,
    )

    seg_list: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for seg in segments:
        t = seg.text.strip()
        seg_list.append(dict(start=round(float(seg.start), 2), end=round(float(seg.end), 2), text=t, speaker=None))
        text_parts.append(t)

    return dict(status="ok", language=info.language, duration=round(float(info.duration), 2), segments=seg_list, text=" ".join(text_parts))


# ---------------------------------------------------------------------------
# FastAPI service
# ---------------------------------------------------------------------------

app = FastAPI(title="Whisper Transcription Service", dependencies=[Depends(require_api_key)])
app.add_middleware(CORSMiddleware, **cors_kwargs())


class TranscribeRequest(BaseModel):
    audio_path: str
    output_dir: str = str(OUTPUT_DIR)
    language: str | None = None


@app.post("/transcribe")
def transcribe_endpoint(req: TranscribeRequest):
    audio = confine_to_roots(req.audio_path, _INPUT_ROOTS)
    out_dir = confine_to_roots(req.output_dir, _OUTPUT_ROOTS)
    result = transcribe(audio, out_dir, req.language)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@app.get("/health")
def health():
    return {"status": "ok", "service": "whisper-pipeline", "model": WHISPER_MODEL_SIZE}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Faster-Whisper Transcription Pipeline")
    parser.add_argument("--file",   help="Audio file to transcribe")
    parser.add_argument("--output", default=str(OUTPUT_DIR), help="Output directory for transcripts")
    parser.add_argument("--lang",   default=None, help="Force language (e.g. 'en')")
    parser.add_argument("--serve",  action="store_true", help="Run as FastAPI service")
    args = parser.parse_args()

    if args.serve:
        uvicorn.run("media.whisper_pipeline:app", host=bind_host(), port=PORT, reload=False)
    elif args.file:
        result = transcribe(Path(args.file), Path(args.output), args.lang)
        print(result)
    else:
        parser.print_help()
