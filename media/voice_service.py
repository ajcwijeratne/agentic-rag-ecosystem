"""
Voice Service — VAD + Whisper + VOSK over REST and WebSocket
=============================================================
The speech front door for the ecosystem, on port 8009.

  GET  /health                 liveness + dependency report
  GET  /engines                which engines/VAD backends this machine can run
  POST /vad                    speech segments of a file, no transcription
  POST /transcribe             transcribe a file already on disk
  POST /transcribe/upload      transcribe an uploaded file (multipart)
  WS   /ws/transcribe          live microphone streaming

Security follows the same rules as every other service here: loopback is
trusted, remote callers need X-API-Key, CORS is restricted to ALLOWED_ORIGINS,
the bind address comes from HOST, and every filesystem path is confined to the
media roots. The same require_api_key dependency guards the websocket route -
it is typed HTTPConnection, the shared base of Request and WebSocket - and it
accepts `?api_key=` there because browsers cannot set headers on a WebSocket
handshake.

WebSocket protocol:

  1. Client connects and sends one JSON config frame:
       {"engine": "hybrid", "sample_rate": 48000, "language": "en",
        "vad_backend": "auto", "silence_ms": 600}
     Server replies {"type": "ready", ...}.
  2. Client sends raw binary int16 mono PCM at the declared sample rate.
     Chunks may be any size; the server resamples to 16 kHz and frames them.
  3. Server streams JSON events: speech_start, partial, final, speech_end.
  4. Client sends {"type": "stop"} (or closes) -> server flushes and sends a
     final `transcript` event with the full session text.

Recognition runs in a worker thread, so a slow Whisper pass on one utterance
never stops the socket accepting the next.

Run:
  python -m media.voice_service --serve
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import uvicorn
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from common.security import (
    bind_host,
    confine_to_roots,
    cors_kwargs,
    require_api_key,
)

from .asr import (
    ASR_ENGINE,
    LiveConfig,
    LiveSession,
    analyse_audio,
    engine_status,
    transcribe_file,
    transcribe_pcm,
    write_markdown,
)
from .audio import SAMPLE_RATE, decode_bytes_to_pcm, decode_to_pcm, resample_pcm
from .vad import SegmenterConfig

PORT: int = int(os.getenv("VOICE_PORT", "8009"))
OUTPUT_DIR: Path = Path(os.getenv("TRANSCRIPT_OUTPUT_DIR", "./transcripts"))
MAX_UPLOAD_MB: int = int(os.getenv("VOICE_MAX_UPLOAD_MB", "100"))

# Same confinement policy as whisper_pipeline: audio may be read from the media
# input root or from previously produced files under the transcript root;
# transcripts may only be written under the transcript root.
INPUT_ROOT: Path = Path(os.getenv("MEDIA_INPUT_ROOT", "./media_input"))
_INPUT_ROOTS = [INPUT_ROOT, OUTPUT_DIR]
_OUTPUT_ROOTS = [OUTPUT_DIR]

app = FastAPI(
    title="Voice Service",
    version="1.0.0",
    description="Voice activity detection and speech recognition (Whisper + VOSK)",
    dependencies=[Depends(require_api_key)],
)
app.add_middleware(CORSMiddleware, **cors_kwargs())


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class TranscribeRequest(BaseModel):
    audio_path:  str
    output_dir:  str = str(OUTPUT_DIR)
    engine:      str = Field(default=ASR_ENGINE, description="whisper | vosk | hybrid")
    language:    str | None = None
    use_vad:     bool = True
    vad_backend: str = "auto"
    write_file:  bool = True


class VADRequest(BaseModel):
    audio_path:  str
    vad_backend: str = "auto"


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status":         "ok",
        "service":        "voice-service",
        "port":           PORT,
        "default_engine": ASR_ENGINE,
    }


@app.get("/engines")
def engines():
    """What is installed and loadable — the UI uses this to enable/disable modes."""
    return engine_status()


@app.post("/vad")
async def vad_endpoint(req: VADRequest):
    """Speech segments of a file, without transcribing it."""
    audio = confine_to_roots(req.audio_path, _INPUT_ROOTS)
    try:
        pcm = await asyncio.to_thread(decode_to_pcm, audio)
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return await asyncio.to_thread(analyse_audio, pcm, req.vad_backend)


@app.post("/transcribe")
async def transcribe_endpoint(req: TranscribeRequest):
    """Transcribe a file already on disk (the ingestion / orchestrator path)."""
    audio = confine_to_roots(req.audio_path, _INPUT_ROOTS)
    out_dir = confine_to_roots(req.output_dir, _OUTPUT_ROOTS)
    try:
        transcript = await asyncio.to_thread(
            transcribe_file, audio, req.engine, req.language, req.use_vad, req.vad_backend
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = transcript.to_dict()
    payload["status"] = "ok"
    payload["input_file"] = str(audio)

    if req.write_file:
        out_file = await asyncio.to_thread(write_markdown, transcript, audio.name, out_dir)
        payload["output_file"] = str(out_file)

    return payload


@app.post("/transcribe/upload")
async def transcribe_upload(
    file:        UploadFile = File(...),
    engine:      str = Form(ASR_ENGINE),
    language:    str = Form(""),
    use_vad:     bool = Form(True),
    vad_backend: str = Form("auto"),
    write_file:  bool = Form(False),
):
    """Transcribe an uploaded recording — the Command Centre mic-capture path."""
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Audio exceeds {MAX_UPLOAD_MB} MB limit")
    if not data:
        raise HTTPException(status_code=422, detail="Empty upload")

    try:
        pcm = await asyncio.to_thread(decode_bytes_to_pcm, data)
        transcript = await asyncio.to_thread(
            transcribe_pcm, pcm, engine, language or None, use_vad, vad_backend
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    payload = transcript.to_dict()
    payload["status"] = "ok"
    payload["filename"] = file.filename

    if write_file:
        out_file = await asyncio.to_thread(
            write_markdown, transcript, file.filename or "upload", OUTPUT_DIR
        )
        payload["output_file"] = str(out_file)

    return payload


# ---------------------------------------------------------------------------
# WebSocket streaming
# ---------------------------------------------------------------------------

def _build_session(config: dict) -> tuple:
    """Turn the client config frame into a LiveSession plus its input sample rate."""
    seg_config = SegmenterConfig()
    for key in ("silence_ms", "min_speech_ms", "max_speech_ms", "padding_ms"):
        if config.get(key) is not None:
            setattr(seg_config, key, int(config[key]))

    live = LiveConfig(
        engine=config.get("engine") or ASR_ENGINE,
        language=config.get("language") or None,
        vad_backend=config.get("vad_backend") or "auto",
        emit_partials=bool(config.get("partials", True)),
        segmenter=seg_config,
        wake_word=bool(config.get("wake_word", False)),
        wake_phrases=list(config.get("wake_phrases") or []),
        wake_timeout_s=float(config.get("wake_timeout_s", 12.0)),
    )
    return LiveSession(live), int(config.get("sample_rate") or SAMPLE_RATE)


def _prepare(chunk: bytes, input_rate: int) -> bytes:
    """Browser audio arrives at 44.1/48 kHz; everything downstream wants 16 kHz."""
    if input_rate == SAMPLE_RATE:
        return chunk
    return resample_pcm(chunk, input_rate, SAMPLE_RATE)


@app.websocket("/ws/transcribe")
async def ws_transcribe(ws: WebSocket):
    # Auth already ran: the app-level require_api_key dependency guards this
    # route too (it is typed HTTPConnection), and rejects before we get here.
    await ws.accept()

    session = None
    input_rate = SAMPLE_RATE

    try:
        # ---- handshake ----------------------------------------------------
        first = await ws.receive()
        raw = first.get("text")
        config = {}
        if raw:
            try:
                config = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "First frame must be JSON config"})
                await ws.close()
                return

        try:
            session, input_rate = await asyncio.to_thread(_build_session, config)
        except Exception as exc:  # model load / missing dependency
            await ws.send_json({"type": "error", "message": f"Could not start session: {exc}"})
            await ws.close()
            return

        await ws.send_json(
            {
                "type":        "ready",
                "engine":      session.config.engine,
                "vad":         getattr(session.segmenter.vad, "name", "unknown"),
                "sample_rate": input_rate,
                "frame_ms":    session.segmenter.frame_ms,
                "partials":    session.config.emit_partials,
                "wake_word":   session.config.wake_word,
                "asleep":      session.asleep,
                "barge_in":    session.barge_in_available,
            }
        )

        # If the handshake frame arrived as binary, it was audio, not config.
        if first.get("bytes"):
            for event in await asyncio.to_thread(session.feed, _prepare(first["bytes"], input_rate)):
                await ws.send_json(event)

        # ---- audio loop ---------------------------------------------------
        while True:
            message = await ws.receive()

            if message.get("type") == "websocket.disconnect":
                break

            if message.get("bytes") is not None:
                pcm = _prepare(message["bytes"], input_rate)
                if not pcm:
                    continue
                for event in await asyncio.to_thread(session.feed, pcm):
                    await ws.send_json(event)
                continue

            text = message.get("text")
            if not text:
                continue
            try:
                command = json.loads(text)
            except json.JSONDecodeError:
                continue

            action = command.get("type")
            if action in ("stop", "flush", "eof"):
                for event in await asyncio.to_thread(session.finish):
                    await ws.send_json(event)
                if action == "stop":
                    break
            elif action == "reset":
                await asyncio.to_thread(session.reset)
                await ws.send_json({"type": "reset"})
            elif action in ("speaking_start", "speaking_end"):
                # The client tells us when the assistant is talking so audio can
                # be routed to the barge-in detector instead of transcription.
                await asyncio.to_thread(session.set_speaking, action == "speaking_start")
                await ws.send_json({"type": "speaking", "value": session.assistant_speaking})
            elif action == "sleep":
                await asyncio.to_thread(session.sleep)
                await ws.send_json({"type": "sleep", "requested": True})
            elif action == "ping":
                await ws.send_json({"type": "pong", "speaking": session.speaking})

    except WebSocketDisconnect:
        # Client hung up mid-utterance: flush so the models release state, but
        # there is nobody left to send the result to.
        if session is not None:
            await asyncio.to_thread(session.finish)
        return
    except Exception as exc:  # noqa: BLE001 — report, do not kill the server
        try:
            await ws.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass

    try:
        await ws.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Voice service — VAD + Whisper + VOSK")
    parser.add_argument("--serve", action="store_true", help="Run the FastAPI service")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--status", action="store_true", help="Print engine availability and exit")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(engine_status(), indent=2))
    else:
        uvicorn.run("media.voice_service:app", host=bind_host(), port=args.port, reload=False)
