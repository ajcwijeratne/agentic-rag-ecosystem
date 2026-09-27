"""
Spoken-reply audio for the Command Centre
=========================================

  GET  /voice/tts/status   what can speak, and how fast (proxied)
  POST /voice/tts          one reply fragment -> audio/wav

The browser asks for each sentence as the answer streams in, fetching the next
while the current one plays, so speech stays ahead of the listener. Models stay
in the voice service (8009); this router is a thin proxy, with the same
in-process fallback the transcription routes use when that service is down.

ElevenLabs costs money per character, so asking for it needs the operator role,
the same gate as the other paid voice route (/voice/ask). Kokoro is free and
runs under the app-level key check only.
"""

from __future__ import annotations

import asyncio
import os

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from common.rbac import ROLE_RANK, role_for_request

router = APIRouter(prefix="/voice", tags=["voice"])

VOICE_SERVICE_URL: str = os.getenv("VOICE_SERVICE_URL", "http://localhost:8009")
VOICE_ALLOW_LOCAL: bool = os.getenv("VOICE_ALLOW_LOCAL", "true").lower() in ("1", "true", "yes")
TTS_PROXY_TIMEOUT: float = float(os.getenv("TTS_PROXY_TIMEOUT_S", "45"))

_PASS_HEADERS = ("x-tts-engine", "x-tts-voice", "x-tts-synth-ms", "x-tts-audio-s",
                 "x-tts-fallback-from", "x-tts-rtf", "access-control-expose-headers")


class TTSRequest(BaseModel):
    text:   str = Field(..., max_length=4000)
    engine: str = ""
    voice:  str = ""
    speed:  float | None = None


def _service_headers() -> dict:
    key = os.getenv("API_KEY", "").strip()
    return {"X-API-Key": key} if key else {}


def _wants_paid_engine(engine: str) -> bool:
    e = (engine or "").strip().lower()
    if e == "elevenlabs":
        return True
    return e in ("", "auto") and os.getenv("TTS_ENGINE", "auto").strip().lower() == "elevenlabs"


def _local_tts():
    """The media.tts module when it can run here, else None."""
    if not VOICE_ALLOW_LOCAL:
        return None
    try:
        from media import tts

        return tts
    except Exception:
        return None


@router.get("/tts/status")
async def voice_tts_status() -> dict:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{VOICE_SERVICE_URL}/tts/status", headers=_service_headers())
        if r.status_code == 200:
            return {"mode": "proxy", **r.json()}
    except Exception:
        pass
    tts = _local_tts()
    if tts is not None:
        return {"mode": "in-process", **tts.status()}
    return {"mode": "unavailable", "engines": {}, "detail": "No text-to-speech here; the browser voice is used."}


@router.post("/tts")
async def voice_tts(req: TTSRequest, request: Request):
    if _wants_paid_engine(req.engine):
        if ROLE_RANK.get(role_for_request(request), -1) < ROLE_RANK["operator"]:
            raise HTTPException(status_code=403, detail="operator role required for ElevenLabs speech")

    payload = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    try:
        async with httpx.AsyncClient(timeout=TTS_PROXY_TIMEOUT) as client:
            r = await client.post(f"{VOICE_SERVICE_URL}/tts", json=payload, headers=_service_headers())
        if r.status_code == 200:
            headers = {k: v for k, v in r.headers.items() if k.lower() in _PASS_HEADERS}
            return Response(content=r.content, media_type="audio/wav", headers=headers)
        if r.status_code == 503:
            return JSONResponse(status_code=503, content=r.json())
        if r.status_code != 404:
            # 404 means an older voice service without /tts: fall through to local.
            raise HTTPException(status_code=r.status_code, detail=r.text[:300])
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout):
        pass

    tts = _local_tts()
    if tts is None:
        return JSONResponse(status_code=503, content={"detail": "voice service unreachable", "fallback": "browser"})
    try:
        speech = await asyncio.to_thread(tts.synthesize, req.text, req.engine, req.voice, req.speed)
    except tts.TTSUnavailable as exc:
        return JSONResponse(status_code=503, content={"detail": str(exc), "fallback": "browser"})
    from media.tts_routes import speech_headers

    return Response(content=speech.to_wav(), media_type="audio/wav", headers=speech_headers(speech))
