"""
Free avatar worker: SadTalker behind the /generate contract.

Runs on the Windows GPU PC (~6GB VRAM):

    python -m gpu_workers.avatar_worker         # port 7861

The orchestrator's avatar worker POSTs {portrait_path, audio_path, ...} to
/generate and expects {path} back. SadTalker animates a single portrait photo
of you to speak the supplied narration, head motion included.

Env:
  SADTALKER_DIR      path to the cloned SadTalker repo (with checkpoints/)
  AVATAR_PORTRAIT    default portrait photo of you (override per request)
  AVATAR_OUT_DIR     output folder reachable by both machines
  AVATAR_WORKER_PORT default 7861

Setup on the PC (once): clone https://github.com/OpenTalker/SadTalker,
install its requirements into a venv, download checkpoints with
scripts/download_models.sh (or the Windows equivalent), set SADTALKER_DIR.
deploy/gpu-worker-setup.ps1 automates this.

Free, local, no keys. For lip-sync onto real video of you (closer to the
HeyGen look), MuseTalk can replace SadTalker later behind this same
contract; the orchestrator does not care which engine answers.
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

PORT = int(os.getenv("AVATAR_WORKER_PORT", "7861"))
SADTALKER_DIR = Path(os.getenv("SADTALKER_DIR", str(Path.home() / "SadTalker")))
OUT_DIR = Path(os.getenv("AVATAR_OUT_DIR", str(Path(__file__).parent / "output")))
DEFAULT_PORTRAIT = os.getenv("AVATAR_PORTRAIT", "")

app = FastAPI(title="Free avatar worker (SadTalker)")


class GenerateRequest(BaseModel):
    audio_path: str
    portrait_path: str | None = None
    filename: str | None = None
    still: bool = False             # less head motion, more stable framing
    model_config = {"extra": "allow"}


def _ready() -> tuple[bool, str]:
    if not SADTALKER_DIR.is_dir():
        return False, f"SADTALKER_DIR not found: {SADTALKER_DIR}"
    if not (SADTALKER_DIR / "inference.py").is_file():
        return False, "inference.py missing; is this a SadTalker checkout?"
    if not (SADTALKER_DIR / "checkpoints").is_dir():
        return False, "checkpoints/ missing; run the model download script"
    return True, "ok"


@app.get("/health")
def health() -> dict:
    ok, detail = _ready()
    return {
        "status": "ok" if ok and (not DEFAULT_PORTRAIT or Path(DEFAULT_PORTRAIT).is_file()) else "degraded",
        "engine": "sadtalker",
        "detail": detail,
        "default_portrait": bool(DEFAULT_PORTRAIT and Path(DEFAULT_PORTRAIT).is_file()),
        "out_dir": str(OUT_DIR),
    }


@app.post("/generate")
def generate(req: GenerateRequest) -> dict:
    ok, detail = _ready()
    if not ok:
        raise HTTPException(status_code=503, detail=detail)
    portrait = req.portrait_path or DEFAULT_PORTRAIT
    if not portrait or not Path(portrait).is_file():
        raise HTTPException(status_code=400, detail="portrait missing; set AVATAR_PORTRAIT or pass portrait_path")
    if not req.audio_path or not Path(req.audio_path).is_file():
        raise HTTPException(status_code=400, detail=f"audio not found: {req.audio_path}")

    run_id = uuid.uuid4().hex[:12]
    result_dir = OUT_DIR / f"run-{run_id}"
    result_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable, "inference.py",
        "--driven_audio", str(req.audio_path),
        "--source_image", str(portrait),
        "--result_dir", str(result_dir),
        "--preprocess", "full",
        "--enhancer", "gfpgan",
    ]
    if req.still:
        cmd.append("--still")

    try:
        result = subprocess.run(cmd, cwd=str(SADTALKER_DIR), capture_output=True,
                                text=True, encoding="utf-8", errors="replace",
                                timeout=float(os.getenv("AVATAR_TIMEOUT", "1800")))
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="SadTalker render timed out")
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=(result.stderr or result.stdout or "")[-800:])

    videos = sorted(result_dir.rglob("*.mp4"), key=lambda p: p.stat().st_mtime)
    if not videos:
        raise HTTPException(status_code=500, detail="SadTalker produced no mp4")
    final = videos[-1]
    if req.filename:
        target = OUT_DIR / (req.filename if req.filename.endswith(".mp4") else req.filename + ".mp4")
        final.replace(target)
        final = target

    return {"path": str(final), "engine": "sadtalker", "clone": True,
            "portrait": str(portrait)}


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("AVATAR_WORKER_HOST", "0.0.0.0"), port=PORT)
