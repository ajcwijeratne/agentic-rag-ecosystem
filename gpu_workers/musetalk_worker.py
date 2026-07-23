"""
MuseTalk avatar worker: lip-sync onto real footage of you.

A drop-in replacement for avatar_worker.py (SadTalker). It answers the same
/generate contract on the same MUSETALK_URL the orchestrator already calls, so
nothing upstream changes. Where SadTalker animates a single photo, MuseTalk
lip-syncs a short reference video of you to the narration, which lifts quality
from animated-photo to lip-synced-video.

Run on the GPU PC (replaces the SadTalker worker on the same port):

    python -m gpu_workers.musetalk_worker        # port 7861

Env:
  MUSETALK_DIR         path to the cloned MuseTalk repo (with its scripts/)
  MUSETALK_REF_VIDEO   your 2-minute reference clip (the face to lip-sync)
  MUSETALK_INFERENCE   module run with -m (default: scripts.inference)
  MUSETALK_PYTHON      interpreter for the MuseTalk venv (default: this one)
  AVATAR_OUT_DIR       output folder reachable by both machines (shared with
                       the SadTalker worker so downstream paths are stable)
  AVATAR_WORKER_PORT   default 7861 (same port; this is a swap, not an addition)

Setup once: clone https://github.com/TMElyralab/MuseTalk, install its
requirements into a venv, download its model weights, record a 2-minute clip of
yourself talking to camera, set MUSETALK_DIR and MUSETALK_REF_VIDEO. Until then
/health reports degraded and /generate returns 503, exactly like the SadTalker
worker before its checkpoints exist.

Free, local, no keys. Every output is clone output; the orchestrator marks the
asset clone=True and governance holds it at the clone_output gate.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

PORT = int(os.getenv("AVATAR_WORKER_PORT", os.getenv("MUSETALK_WORKER_PORT", "7861")))
MUSETALK_DIR = Path(os.getenv("MUSETALK_DIR", str(Path.home() / "MuseTalk")))
REF_VIDEO = os.getenv("MUSETALK_REF_VIDEO", "")
INFERENCE_MODULE = os.getenv("MUSETALK_INFERENCE", "scripts.inference")
PYTHON = os.getenv("MUSETALK_PYTHON", sys.executable)
OUT_DIR = Path(os.getenv("AVATAR_OUT_DIR", str(Path(__file__).parent / "output")))

app = FastAPI(title="MuseTalk avatar worker")


class GenerateRequest(BaseModel):
    audio_path: str
    reference_video: str | None = None   # override the default clip per request
    portrait_path: str | None = None     # ignored by MuseTalk; accepted for parity
    filename: str | None = None
    model_config = {"extra": "allow"}


def _ready() -> tuple[bool, str]:
    if not MUSETALK_DIR.is_dir():
        return False, f"MUSETALK_DIR not found: {MUSETALK_DIR}"
    if not (MUSETALK_DIR / "scripts").is_dir():
        return False, "scripts/ missing; is this a MuseTalk checkout?"
    ref = REF_VIDEO
    if not ref or not Path(ref).is_file():
        return False, "MUSETALK_REF_VIDEO missing; record and set your reference clip"
    return True, "ok"


@app.get("/health")
def health() -> dict:
    ok, detail = _ready()
    return {
        "status": "ok" if ok else "degraded",
        "engine": "musetalk",
        "detail": detail,
        "reference_video": bool(REF_VIDEO and Path(REF_VIDEO).is_file()),
        "out_dir": str(OUT_DIR),
    }


@app.post("/generate")
def generate(req: GenerateRequest) -> dict:
    ok, detail = _ready()
    reference = req.reference_video or REF_VIDEO
    if not ok and not (req.reference_video and Path(req.reference_video).is_file()):
        raise HTTPException(status_code=503, detail=detail)
    if not reference or not Path(reference).is_file():
        raise HTTPException(status_code=400, detail="reference video missing")
    if not req.audio_path or not Path(req.audio_path).is_file():
        raise HTTPException(status_code=400, detail=f"audio not found: {req.audio_path}")

    run_id = uuid.uuid4().hex[:12]
    result_dir = OUT_DIR / f"muse-{run_id}"
    result_dir.mkdir(parents=True, exist_ok=True)

    # MuseTalk reads a small YAML mapping each output to a video+audio pair.
    # Written by hand to keep this worker dependency-free.
    cfg_path = result_dir / "inference.yaml"
    cfg_path.write_text(
        "task_0:\n"
        f"  video_path: {str(reference)!r}\n"
        f"  audio_path: {str(req.audio_path)!r}\n",
        encoding="utf-8",
    )

    cmd = [
        PYTHON, "-m", INFERENCE_MODULE,
        "--inference_config", str(cfg_path),
        "--result_dir", str(result_dir),
    ]
    try:
        result = subprocess.run(cmd, cwd=str(MUSETALK_DIR), capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=float(os.getenv("AVATAR_TIMEOUT", "1800")))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="MuseTalk render timed out")
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=(result.stderr or result.stdout or "")[-800:])

    videos = sorted(result_dir.rglob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not videos:
        raise HTTPException(status_code=500, detail="MuseTalk produced no mp4")
    final = videos[-1]
    if req.filename:
        target = OUT_DIR / (req.filename if req.filename.endswith(".mp4") else req.filename + ".mp4")
        final.replace(target)
        final = target

    return {"path": str(final), "engine": "musetalk", "clone": True,
            "reference": str(reference)}


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("AVATAR_WORKER_HOST", "0.0.0.0"), port=PORT)
