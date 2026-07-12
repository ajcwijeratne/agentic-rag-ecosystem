"""
Free voice-clone worker: F5-TTS behind the /generate contract.

Runs on the Windows GPU PC (or anywhere with an NVIDIA GPU, ~3GB VRAM):

    python -m gpu_workers.voice_worker          # port 8020

The orchestrator's voice worker POSTs {text, ...} to /generate and expects
{path} back. Cloning works from a single reference clip of your voice:

  VOICE_REF_AUDIO   path to a clean 5-15 second wav/mp3 of you speaking
  VOICE_REF_TEXT    exact transcript of that clip (improves fidelity)
  VOICE_OUT_DIR     where rendered audio lands (default gpu_workers/output)
                    Point this at a folder both machines can reach (the
                    OneDrive repo folder works) so the returned path is
                    valid for the orchestrator too.

Engine selection:
  F5-TTS (default): pip install f5-tts. MIT licence, commercial-safe,
  zero-shot clone from the reference clip. The worker shells out to the
  f5-tts_infer-cli command so it works regardless of API churn in the
  library; the CLI has been the stable surface.

No API keys, no per-minute charges. Quality with a good reference clip sits
close to the paid services for narration; record the reference in a quiet
room and it will sound like you.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

PORT = int(os.getenv("VOICE_WORKER_PORT", "8020"))
OUT_DIR = Path(os.getenv("VOICE_OUT_DIR", str(Path(__file__).parent / "output")))
REF_AUDIO = os.getenv("VOICE_REF_AUDIO", "")
REF_TEXT = os.getenv("VOICE_REF_TEXT", "")

app = FastAPI(title="Free voice-clone worker (F5-TTS)")


class GenerateRequest(BaseModel):
    text: str
    ref_audio: str | None = None    # override the default reference clip
    ref_text: str | None = None
    filename: str | None = None
    # Extra fields from the orchestrator's payload are accepted and ignored.
    model_config = {"extra": "allow"}


def _cli_available() -> bool:
    return shutil.which("f5-tts_infer-cli") is not None


@app.get("/health")
def health() -> dict:
    ref = REF_AUDIO and Path(REF_AUDIO).is_file()
    return {
        "status": "ok" if (_cli_available() and ref) else "degraded",
        "engine": "f5-tts",
        "cli": _cli_available(),
        "reference_audio": bool(ref),
        "out_dir": str(OUT_DIR),
    }


@app.post("/generate")
def generate(req: GenerateRequest) -> dict:
    if not _cli_available():
        raise HTTPException(status_code=503, detail="f5-tts_infer-cli not on PATH; pip install f5-tts")
    ref_audio = req.ref_audio or REF_AUDIO
    if not ref_audio or not Path(ref_audio).is_file():
        raise HTTPException(status_code=400, detail="reference audio missing; set VOICE_REF_AUDIO to a clip of your voice")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    name = req.filename or f"voice-{uuid.uuid4().hex[:12]}.wav"
    if not name.endswith(".wav"):
        name += ".wav"

    # f5-tts_infer-cli writes into an output dir; render into a temp dir and
    # move the result so concurrent requests cannot collide.
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [
            "f5-tts_infer-cli",
            "--model", os.getenv("F5_MODEL", "F5TTS_v1_Base"),
            "--ref_audio", str(ref_audio),
            "--gen_text", text,
            "--output_dir", tmp,
        ]
        ref_text = req.ref_text or REF_TEXT
        if ref_text:
            cmd += ["--ref_text", ref_text]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=float(os.getenv("VOICE_TIMEOUT", "600")))
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="F5-TTS render timed out")
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail=(result.stderr or result.stdout or "")[-800:])

        wavs = sorted(Path(tmp).rglob("*.wav"), key=lambda p: p.stat().st_mtime)
        if not wavs:
            raise HTTPException(status_code=500, detail="F5-TTS produced no wav output")
        out_path = OUT_DIR / name
        shutil.move(str(wavs[-1]), out_path)

    return {"path": str(out_path), "engine": "f5-tts", "clone": True,
            "ref_audio": str(ref_audio)}


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("VOICE_WORKER_HOST", "0.0.0.0"), port=PORT)
