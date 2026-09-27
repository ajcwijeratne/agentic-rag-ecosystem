"""
Text-to-speech routes for the voice service (port 8009)
======================================================

  GET  /tts/status   engines, measured Kokoro speed, suggested voices
  POST /tts          {"text", "engine"?, "voice"?, "speed"?} -> audio/wav

Kept in its own module so the voice service gains speech output with two lines,
and so the models load in this process, never in the orchestrator's.

A 503 from POST /tts is a normal answer, not a fault: it means "no engine here
can speak this", and the browser speaks with its own voice instead.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from . import tts

router = APIRouter(tags=["tts"])

EXPOSE = "X-TTS-Engine, X-TTS-Voice, X-TTS-Synth-Ms, X-TTS-Audio-S, X-TTS-Fallback-From, X-TTS-RTF"


class TTSRequest(BaseModel):
    text:   str = Field(..., max_length=4000)
    engine: str = ""
    voice:  str = ""
    speed:  float | None = None


def speech_headers(speech: tts.Speech) -> dict:
    rtf = tts._kokoro.rtf if speech.engine == "kokoro" else None
    return {
        "X-TTS-Engine": speech.engine,
        "X-TTS-Voice": speech.voice,
        "X-TTS-Synth-Ms": str(round(speech.synth_ms)),
        "X-TTS-Audio-S": f"{speech.duration_s:.2f}",
        "X-TTS-Fallback-From": speech.fallback_from,
        "X-TTS-RTF": "" if rtf is None else f"{rtf:.3f}",
        "Access-Control-Expose-Headers": EXPOSE,
        "Cache-Control": "no-store",
    }


@router.get("/tts/status")
def tts_status():
    return tts.status()


@router.post("/tts")
async def tts_speak(req: TTSRequest):
    try:
        speech = await asyncio.to_thread(
            tts.synthesize, req.text, req.engine, req.voice, req.speed
        )
    except tts.TTSUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc), "fallback": "browser"})
    return Response(content=speech.to_wav(), media_type="audio/wav", headers=speech_headers(speech))
