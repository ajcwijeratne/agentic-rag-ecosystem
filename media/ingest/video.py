"""
Video ingestion worker.

Four steps, each best-effort and degrading cleanly if ffmpeg is absent:
  1. probe duration and dimensions (ffprobe)
  2. extract the audio track and transcribe it (reuses the Whisper worker),
     writing a timestamped transcript onto the video asset
  3. sample keyframes (ffmpeg scene-change filter, with an interval fallback),
     registering each as its own image asset linked keyframe_of the video
  4. record scene-boundary timestamps and the keyframe count in asset.meta

Returns the child keyframe asset ids so the dispatcher can index them. If ffmpeg
is not installed, the asset still registers with whatever metadata is available
and is marked ready; keyframes and transcript simply do not appear.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .. import registry

DERIVED_ROOT = Path(os.getenv("MEDIA_DERIVED_ROOT", "./media_derived"))
SCENE_THRESHOLD = float(os.getenv("VIDEO_SCENE_THRESHOLD", "0.3"))
KEYFRAME_INTERVAL = int(os.getenv("VIDEO_KEYFRAME_INTERVAL_S", "30"))
MAX_KEYFRAMES = int(os.getenv("VIDEO_MAX_KEYFRAMES", "40"))
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def _run(cmd: list[str], timeout: int = 600) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stderr or "") + (proc.stdout or "")
    except FileNotFoundError:
        return 127, "binary not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def _probe(path: Path) -> tuple[float | None, str | None]:
    rc, out = _run(["ffprobe", "-v", "error", "-show_entries",
                    "format=duration", "-of", "default=nk=1:nw=1", str(path)], timeout=60)
    duration = None
    if rc == 0:
        try:
            duration = round(float(out.strip().splitlines()[0]), 2)
        except Exception:
            duration = None
    rc, out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)], timeout=60)
    dims = out.strip().splitlines()[0].strip() if rc == 0 and out.strip() else None
    if dims and "x" not in dims:
        dims = None
    return duration, dims


def _extract_audio(path: Path, out_wav: Path) -> bool:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    rc, _ = _run(["ffmpeg", "-hide_banner", "-y", "-i", str(path),
                  "-vn", "-ac", "1", "-ar", "16000", str(out_wav)], timeout=900)
    return rc == 0 and out_wav.exists()


def _keyframes(path: Path, out_dir: Path) -> tuple[list[Path], list[float]]:
    """Scene-change keyframes with timestamps. Falls back to fixed intervals when
    the scene filter yields nothing. Returns (frame_paths, scene_times)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = path.stem
    pattern = str(out_dir / f"{stem}_kf%03d.jpg")
    rc, log = _run(["ffmpeg", "-hide_banner", "-y", "-i", str(path),
                    "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
                    "-vsync", "vfr", "-frames:v", str(MAX_KEYFRAMES), pattern], timeout=900)
    frames = sorted(out_dir.glob(f"{stem}_kf*.jpg"))
    scene_times = [round(float(t), 2) for t in _PTS_RE.findall(log)]
    if not frames:  # interval fallback
        rc, _ = _run(["ffmpeg", "-hide_banner", "-y", "-i", str(path),
                      "-vf", f"fps=1/{KEYFRAME_INTERVAL}",
                      "-frames:v", str(MAX_KEYFRAMES), pattern], timeout=900)
        frames = sorted(out_dir.glob(f"{stem}_kf*.jpg"))
    if not frames:  # guarantee at least the first frame, even for short clips
        _run(["ffmpeg", "-hide_banner", "-y", "-i", str(path),
              "-frames:v", "1", str(out_dir / f"{stem}_kf001.jpg")], timeout=120)
        frames = sorted(out_dir.glob(f"{stem}_kf*.jpg"))
    return frames, scene_times


def enrich(asset_id: str, path: str, *, language: str | None = None) -> dict[str, Any]:
    video_path = Path(path)
    if not video_path.exists():
        registry.set_status(asset_id, "failed")
        return {"status": "failed", "detail": "file not found"}

    asset = registry.get_asset(asset_id, with_relations=False) or {}
    project = asset.get("project")
    rights = asset.get("rights", "unknown")

    duration, dims = _probe(video_path)
    notes: list[str] = []

    # Transcribe the audio track.
    segments = 0
    out_wav = DERIVED_ROOT / f"{video_path.stem}.wav"
    if _extract_audio(video_path, out_wav):
        try:
            from ..whisper_pipeline import transcribe_segments
            result = transcribe_segments(out_wav, language=language)
            if result.get("status") == "ok":
                registry.add_transcript(
                    asset_id,
                    language=result.get("language"),
                    segments=result.get("segments", []),
                    text=result.get("text", ""),
                )
                transcript_segments = result.get("segments", [])
                segments = len(transcript_segments)
                for i, seg in enumerate(transcript_segments, start=1):
                    registry.add_moment(
                        asset_id,
                        kind="transcript",
                        label=f"Transcript moment {i}",
                        t_start=seg.get("start"),
                        t_end=seg.get("end"),
                        text=(seg.get("text") or "").strip(),
                        meta={
                            "speaker": seg.get("speaker"),
                            "language": result.get("language"),
                            "segment_index": i,
                        },
                    )
                if duration is None:
                    duration = result.get("duration")
            else:
                notes.append("transcription failed")
        except Exception as exc:
            notes.append(f"transcriber unavailable: {exc}")
    else:
        notes.append("audio extraction skipped (ffmpeg missing or failed)")

    # Keyframes as child image assets.
    children: list[str] = []
    scene_times: list[float] = []
    frames, scene_times = _keyframes(video_path, DERIVED_ROOT)
    for i, f in enumerate(frames, start=1):
        timestamp = scene_times[i - 1] if i <= len(scene_times) else round((i - 1) * KEYFRAME_INTERVAL, 2)
        kid = registry.add_asset(
            "image", path=str(f), source="derived",
            rights=rights, status="ready", project=project,
            meta={"parent_asset_id": asset_id, "frame_index": i, "t_start": timestamp},
        )
        registry.add_link(kid, asset_id, "keyframe_of")
        registry.add_moment(
            asset_id,
            kind="keyframe",
            label=f"Keyframe {i}",
            t_start=timestamp,
            thumbnail_path=str(f),
            child_asset_id=kid,
            meta={"frame_index": i, "scene_detected": i <= len(scene_times)},
        )
        children.append(kid)
    if not frames:
        notes.append("keyframes skipped (ffmpeg missing or no frames)")

    fields: dict[str, Any] = {
        "status": "ready",
        "meta": {"scenes": scene_times, "keyframes": len(children)},
    }
    if duration is not None:
        fields["duration"] = duration
    if dims:
        fields["dimensions"] = dims
    registry.update_asset(asset_id, **fields)

    return {
        "status":    "ready",
        "duration":  duration,
        "segments":  segments,
        "keyframes": len(children),
        "scenes":    len(scene_times),
        "children":  children,
        "notes":     notes,
    }
