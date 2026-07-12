"""
Video Processing Pipeline — FFmpeg + MoviePy
=============================================
Automates video cuts, rendering, subtitle overlays, and compilation.

Features:
  • Trim a clip by start/end time
  • Concatenate multiple clips into one
  • Extract audio track to MP3 for Whisper
  • Overlay subtitle .srt file
  • Resize / re-encode for web delivery
  • REST endpoint for orchestrator-triggered jobs

Dependencies:
  pip install moviepy
  System: ffmpeg must be installed (apt/brew/choco install ffmpeg)

Usage:
  python -m media.video_pipeline --serve
  python -m media.video_pipeline --trim input.mp4 00:01:00 00:02:30 out.mp4
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from common.security import require_api_key, cors_kwargs, bind_host, confine_to_roots

OUTPUT_DIR: Path = Path(os.getenv("VIDEO_OUTPUT_DIR", "./video_output"))
PORT: int        = int(os.getenv("VIDEO_PIPELINE_PORT", "8008"))

# Media operations are confined to these roots. Inputs may come from the input
# root or a previously produced file under the output root; outputs must land in
# the output root. Paths outside both are rejected (403) before FFmpeg runs.
INPUT_ROOT: Path  = Path(os.getenv("MEDIA_INPUT_ROOT", "./media_input"))
_INPUT_ROOTS  = [INPUT_ROOT, OUTPUT_DIR]
_OUTPUT_ROOTS = [OUTPUT_DIR]


def _vin(path: str) -> Path:
    """Validate an input/source path against the permitted media roots."""
    return confine_to_roots(path, _INPUT_ROOTS)


def _vout(path: str) -> Path:
    """Validate an output path against the permitted output root."""
    return confine_to_roots(path, _OUTPUT_ROOTS)


# ---------------------------------------------------------------------------
# Helpers — thin wrappers around FFmpeg subprocess calls
# ---------------------------------------------------------------------------

def _run_ffmpeg(args: list[str]) -> dict[str, Any]:
    cmd = ["ffmpeg", "-y"] + args
    print(f"[video] Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return {"status": "error", "stderr": result.stderr[-2000:]}
    return {"status": "ok", "stderr": ""}


def _ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def trim_clip(
    input_path: Path,
    start: str,
    end: str,
    output_path: Path,
) -> dict[str, Any]:
    """
    Trim a video to [start, end] (HH:MM:SS or seconds).
    Re-encodes with H.264 + AAC for broad compatibility.
    """
    _ensure_dir(output_path)
    return _run_ffmpeg([
        "-i", str(input_path),
        "-ss", start,
        "-to", end,
        "-c:v", "libx264",
        "-c:a", "aac",
        "-strict", "experimental",
        str(output_path),
    ])


def concatenate_clips(
    input_paths: list[Path],
    output_path: Path,
) -> dict[str, Any]:
    """Concatenate a list of video clips using MoviePy."""
    _ensure_dir(output_path)
    try:
        from moviepy.editor import VideoFileClip, concatenate_videoclips
        clips = [VideoFileClip(str(p)) for p in input_paths]
        final = concatenate_videoclips(clips, method="compose")
        final.write_videofile(str(output_path), codec="libx264", audio_codec="aac", logger=None)
        for c in clips:
            c.close()
        final.close()
        return {"status": "ok", "output": str(output_path)}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


def extract_audio(
    input_path: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    """
    Extract audio from a video to an MP3 file.
    Default output path: same directory as input, .mp3 extension.
    """
    if output_path is None:
        output_path = input_path.with_suffix(".mp3")
    _ensure_dir(output_path)
    return _run_ffmpeg([
        "-i", str(input_path),
        "-vn",
        "-ar", "16000",   # 16kHz — optimal for Whisper
        "-ac", "1",       # mono
        "-ab", "96k",
        "-f", "mp3",
        str(output_path),
    ])


def overlay_subtitles(
    input_path: Path,
    srt_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """Burn subtitles from an SRT file into the video stream."""
    _ensure_dir(output_path)
    return _run_ffmpeg([
        "-i", str(input_path),
        "-vf", f"subtitles={srt_path}",
        "-c:a", "copy",
        str(output_path),
    ])


def resize_video(
    input_path: Path,
    width: int,
    height: int,
    output_path: Path,
) -> dict[str, Any]:
    """Resize video to given dimensions."""
    _ensure_dir(output_path)
    return _run_ffmpeg([
        "-i", str(input_path),
        "-vf", f"scale={width}:{height}",
        "-c:a", "copy",
        str(output_path),
    ])


# ---------------------------------------------------------------------------
# FastAPI service
# ---------------------------------------------------------------------------

app = FastAPI(title="Video Processing Pipeline", dependencies=[Depends(require_api_key)])
app.add_middleware(CORSMiddleware, **cors_kwargs())


class TrimRequest(BaseModel):
    input_path:  str
    start:       str           # HH:MM:SS
    end:         str           # HH:MM:SS
    output_path: str | None = None


class ConcatRequest(BaseModel):
    input_paths: list[str]
    output_path: str


class ExtractAudioRequest(BaseModel):
    input_path:  str
    output_path: str | None = None


class SubtitleRequest(BaseModel):
    input_path:  str
    srt_path:    str
    output_path: str


class ResizeRequest(BaseModel):
    input_path:  str
    width:       int
    height:      int
    output_path: str


@app.post("/trim")
def trim_endpoint(req: TrimRequest):
    inp = _vin(req.input_path)
    out = _vout(req.output_path) if req.output_path else OUTPUT_DIR / "trimmed.mp4"
    result = trim_clip(inp, req.start, req.end, out)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result)
    return {**result, "output": str(out)}


@app.post("/concat")
def concat_endpoint(req: ConcatRequest):
    inputs = [_vin(p) for p in req.input_paths]
    out = _vout(req.output_path)
    result = concatenate_clips(inputs, out)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/extract-audio")
def extract_audio_endpoint(req: ExtractAudioRequest):
    inp = _vin(req.input_path)
    out = _vout(req.output_path) if req.output_path else None
    result = extract_audio(inp, out)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/subtitles")
def subtitle_endpoint(req: SubtitleRequest):
    inp = _vin(req.input_path)
    srt = _vin(req.srt_path)
    out = _vout(req.output_path)
    result = overlay_subtitles(inp, srt, out)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result)
    return result


@app.post("/resize")
def resize_endpoint(req: ResizeRequest):
    inp = _vin(req.input_path)
    out = _vout(req.output_path)
    result = resize_video(inp, req.width, req.height, out)
    if result["status"] == "error":
        raise HTTPException(status_code=400, detail=result)
    return result


@app.get("/health")
def health():
    return {"status": "ok", "service": "video-pipeline"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Video Processing Pipeline")
    parser.add_argument("--serve",  action="store_true")
    parser.add_argument("--trim",   nargs=4, metavar=("INPUT", "START", "END", "OUTPUT"))
    parser.add_argument("--concat", nargs="+", metavar="FILE")
    parser.add_argument("--extract-audio", metavar="INPUT")
    args = parser.parse_args()

    if args.serve:
        uvicorn.run("media.video_pipeline:app", host=bind_host(), port=PORT, reload=False)
    elif args.trim:
        inp, start, end, out = args.trim
        print(trim_clip(Path(inp), start, end, Path(out)))
    elif args.concat:
        paths  = args.concat[:-1]
        output = args.concat[-1]
        print(concatenate_clips([Path(p) for p in paths], Path(output)))
    elif args.extract_audio:
        print(extract_audio(Path(args.extract_audio)))
