"""
Voice endpoints for the orchestrator
=====================================
Where speech meets the agent graph. The heavy lifting (VAD, Whisper, VOSK)
lives in the voice service on port 8009; this module is the bridge that turns
what someone said into a routed, answered query.

  GET  /voice/engines        engine + VAD availability (proxied)
  POST /voice/transcribe     upload audio -> transcript
  POST /voice/ask            upload audio -> transcript -> full hybrid answer
  WS   /voice/ws             live mic -> live transcript -> answer on stop

Two execution modes, chosen automatically:

  proxy      — forward to the voice service (default). Keeps the several-hundred-MB
               Whisper and VOSK models out of the orchestrator process.
  in-process — used when the voice service is unreachable but the `media`
               package imports cleanly, so a single-process dev run still works.

The websocket is a transparent proxy to the voice service socket, with one
addition: when `auto_query` is set, every completed utterance is run through
/hybrid as it lands and the answer is pushed back down the same socket. That is
what makes the loop hands-free — speak, get an answer, keep talking — rather
than one-shot. Queries are serialised, and utterances too short to be a real
question are dropped before they cost anything.

Auth follows the house rules. The HTTP routes inherit the app-level
require_api_key dependency; /voice/ask additionally requires the `operator`
role because it spends model budget. The websocket is guarded by that same
app-level dependency.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, WebSocket, WebSocketDisconnect

from common.rbac import require_role

router = APIRouter(prefix="/voice", tags=["voice"])

VOICE_SERVICE_URL: str = os.getenv("VOICE_SERVICE_URL", "http://localhost:8009")
VOICE_ENGINE:      str = os.getenv("ASR_ENGINE", "whisper")
VOICE_TIMEOUT:     float = float(os.getenv("VOICE_TIMEOUT_S", "300"))
VOICE_ALLOW_LOCAL: bool = os.getenv("VOICE_ALLOW_LOCAL", "true").lower() in ("1", "true", "yes")

# Hands-free: utterances shorter than this never reach a model. Filler words and
# a cough that clears VAD would otherwise each cost a paid call.
AUTO_QUERY_MIN_CHARS: int = int(os.getenv("VOICE_AUTO_QUERY_MIN_CHARS", "8"))

# Persona for spoken conversation. The name is cosmetic; the length instruction
# is not. An answer that reads well on screen is unbearable aloud — nobody wants
# 400 words and a bulleted list read at them, and the listener cannot skim. This
# is applied only to voice turns, so typed chat keeps its normal fuller answers.
VOICE_ASSISTANT_NAME: str = os.getenv("VOICE_ASSISTANT_NAME", "Jarvis")
VOICE_MAX_SENTENCES: int = int(os.getenv("VOICE_MAX_SENTENCES", "3"))
VOICE_PERSONA: str = os.getenv("VOICE_PERSONA", "").strip()


def voice_system_prompt() -> str:
    """The framing sent with every spoken turn."""
    if VOICE_PERSONA:
        return VOICE_PERSONA
    return (
        f"You are {VOICE_ASSISTANT_NAME}, Aaron's voice assistant. This answer "
        f"will be read aloud, so reply in at most {VOICE_MAX_SENTENCES} short "
        "sentences of plain spoken English. No markdown, no bullet points, no "
        "headings, no code blocks, no URLs — they are unreadable aloud. Lead "
        "with the answer itself rather than restating the question. If the "
        "honest answer is that you do not know, say so briefly. If the full "
        "answer genuinely needs more detail, give the short version and say it "
        "is on screen."
    )

_ws_url = VOICE_SERVICE_URL.replace("https://", "wss://").replace("http://", "ws://")
VOICE_WS_URL: str = f"{_ws_url.rstrip('/')}/ws/transcribe"


def _service_headers() -> dict:
    """Forward our API key so a non-loopback voice service still answers."""
    key = os.getenv("API_KEY", "").strip()
    return {"X-API-Key": key} if key else {}


# ---------------------------------------------------------------------------
# Service reachability
# ---------------------------------------------------------------------------

async def _service_up() -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(f"{VOICE_SERVICE_URL}/health", headers=_service_headers())
            return r.status_code == 200
    except Exception:
        return False


def _local_available() -> bool:
    """True when the media package can run recognition in this process."""
    if not VOICE_ALLOW_LOCAL:
        return False
    try:
        from media import asr  # noqa: F401

        return True
    except Exception:
        return False


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=(
            f"Voice service unreachable at {VOICE_SERVICE_URL} and the local media "
            "package could not be loaded. Start it with "
            "`python -m media.voice_service --serve`."
        ),
    )


# ---------------------------------------------------------------------------
# Engine status
# ---------------------------------------------------------------------------

@router.get("/engines")
async def voice_engines() -> dict:
    """What the voice stack can run — the UI greys out modes that are missing."""
    if await _service_up():
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{VOICE_SERVICE_URL}/engines", headers=_service_headers())
            return {"mode": "proxy", "service": VOICE_SERVICE_URL, **r.json()}

    if _local_available():
        from media.asr import engine_status

        return {"mode": "in-process", "service": None, **engine_status()}

    return {
        "mode":      "unavailable",
        "service":   VOICE_SERVICE_URL,
        "available": False,
        "detail":    "Voice service is not running and media dependencies are missing.",
    }


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

async def _transcribe_upload(
    filename: str,
    data: bytes,
    engine: str,
    language: str,
    vad_backend: str,
    use_vad: bool,
) -> dict:
    """Transcribe uploaded bytes via the service, falling back to in-process."""
    if await _service_up():
        async with httpx.AsyncClient(timeout=VOICE_TIMEOUT) as client:
            response = await client.post(
                f"{VOICE_SERVICE_URL}/transcribe/upload",
                headers=_service_headers(),
                files={"file": (filename, data, "application/octet-stream")},
                data={
                    "engine":      engine,
                    "language":    language,
                    "use_vad":     str(use_vad).lower(),
                    "vad_backend": vad_backend,
                    "write_file":  "false",
                },
            )
        if response.status_code >= 400:
            raise HTTPException(status_code=response.status_code, detail=response.text[:500])
        return response.json()

    if not _local_available():
        raise _unavailable()

    from media.asr import transcribe_pcm
    from media.audio import decode_bytes_to_pcm

    try:
        pcm = await asyncio.to_thread(decode_bytes_to_pcm, data)
        transcript = await asyncio.to_thread(
            transcribe_pcm, pcm, engine, language or None, use_vad, vad_backend
        )
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"status": "ok", "filename": filename, **transcript.to_dict()}


@router.post("/transcribe")
async def voice_transcribe(
    file:        UploadFile = File(...),
    engine:      str = Form(VOICE_ENGINE),
    language:    str = Form(""),
    vad_backend: str = Form("auto"),
    use_vad:     bool = Form(True),
):
    """Upload a recording, get the text back. No agent involvement."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty audio upload")
    return await _transcribe_upload(
        file.filename or "recording.webm", data, engine, language, vad_backend, use_vad
    )


# ---------------------------------------------------------------------------
# Speak-to-answer
# ---------------------------------------------------------------------------

async def _run_hybrid(
    query: str,
    session_id: str,
    force_route: str | None = None,
    spoken: bool = False,
) -> dict:
    """
    Send a transcript through the normal hybrid routing pipeline. Imported
    lazily: main.py imports this router, so a module-level import would be
    circular.

    `spoken` prepends the voice persona as a system turn, which keeps the reply
    short enough to listen to. Typed chat never sees it.
    """
    from .main import HybridRequest, run_hybrid

    # "What's on my screen?" cannot be answered from the vault. Look instead.
    if spoken:
        from .screen import answer_about_screen

        seen = await answer_about_screen(query, spoken=True)
        if seen is not None:
            return {"session_id": session_id, "query": query, **seen}

    history = [{"role": "system", "content": voice_system_prompt()}] if spoken else []
    return await run_hybrid(
        HybridRequest(
            query=query,
            session_id=session_id,
            force_route=force_route,
            conversation_history=history,
        )
    )


@router.post("/ask", dependencies=[Depends(require_role("operator"))])
async def voice_ask(
    file:        UploadFile = File(...),
    engine:      str = Form(VOICE_ENGINE),
    language:    str = Form(""),
    vad_backend: str = Form("auto"),
    session_id:  str = Form(""),
    force_route: str = Form(""),
):
    """
    The whole loop in one call: audio in, routed answer out. The transcript is
    returned alongside the answer so the UI can show what was heard — a wrong
    transcription is otherwise indistinguishable from a wrong answer.

    Gated on the operator role: it spends model budget, like the other paid
    actions in this service.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty audio upload")

    session_id = session_id or str(uuid.uuid4())
    transcript = await _transcribe_upload(
        file.filename or "recording.webm", data, engine, language, vad_backend, True
    )

    text = (transcript.get("text") or "").strip()
    if not text:
        return {
            "session_id": session_id,
            "transcript": transcript,
            "answer":     "",
            "route":      None,
            "error":      "No speech detected in the recording.",
        }

    answer = await _run_hybrid(text, session_id, force_route or None)
    return {"session_id": session_id, "transcript": transcript, **answer}


# ---------------------------------------------------------------------------
# Live websocket
# ---------------------------------------------------------------------------

@router.websocket("/ws")
async def voice_ws(client: WebSocket):
    """
    Proxy the browser mic stream to the voice service and relay events back.

    Config frame (first message) is passed straight through, plus three keys
    this layer consumes:

        {"auto_query": true, "session_id": "...", "force_route": null}

    With `auto_query`, the final transcript is run through /hybrid and the
    answer arrives as {"type": "answer", ...} after the transcript event.
    """
    await client.accept()

    try:
        import websockets
    except ImportError:
        await client.send_json(
            {"type": "error", "message": "The `websockets` package is required for /voice/ws"}
        )
        await client.close()
        return

    try:
        first = await client.receive_text()
        config: dict[str, Any] = json.loads(first)
    except (WebSocketDisconnect, json.JSONDecodeError):
        await client.close()
        return

    auto_query  = bool(config.pop("auto_query", False))
    session_id  = str(config.pop("session_id", "") or uuid.uuid4())
    force_route = config.pop("force_route", None)
    auto_query_min_chars = int(config.pop("auto_query_min_chars", AUTO_QUERY_MIN_CHARS))
    wake_word   = bool(config.get("wake_word", False))
    wake_phrases = list(config.get("wake_phrases") or [])

    key = os.getenv("API_KEY", "").strip()
    upstream_url = f"{VOICE_WS_URL}?api_key={key}" if key else VOICE_WS_URL

    try:
        upstream = await websockets.connect(upstream_url, max_size=None)
    except Exception as exc:
        await client.send_json(
            {"type": "error", "message": f"Voice service unreachable at {VOICE_WS_URL}: {exc}"}
        )
        await client.close()
        return

    async def pump_up() -> None:
        """Browser -> voice service."""
        while True:
            message = await client.receive()
            if message.get("type") == "websocket.disconnect":
                await upstream.close()
                return
            if message.get("bytes") is not None:
                await upstream.send(message["bytes"])
            elif message.get("text") is not None:
                await upstream.send(message["text"])

    # Hands-free state. `in_flight` serialises queries: a second utterance that
    # lands while the agent is still answering is skipped rather than queued, so
    # a burst of speech cannot fan out into parallel paid model calls.
    in_flight = {"busy": False}

    async def answer_utterance(text: str) -> None:
        """Run one spoken utterance through the pipeline and return the answer."""
        if in_flight["busy"]:
            await client.send_json({
                "type": "skipped", "reason": "busy", "text": text,
                "detail": "Still answering the previous question.",
            })
            return

        in_flight["busy"] = True
        try:
            await client.send_json({"type": "thinking", "query": text, "session_id": session_id})
            answer = await _run_hybrid(text, session_id, force_route, spoken=True)
            await client.send_json({"type": "answer", "session_id": session_id, **answer})
        except Exception as exc:  # noqa: BLE001 — keep the socket alive
            await client.send_json({"type": "error", "message": f"Query failed: {exc}"})
        finally:
            in_flight["busy"] = False

    async def pump_down() -> None:
        """
        Voice service -> browser.

        In hands-free mode each completed utterance is answered as it lands,
        rather than waiting for the stream to end — that is what makes the loop
        conversational instead of one-shot. Utterances shorter than
        `auto_query_min_chars` are treated as noise ("um", "yeah", a cough that
        cleared VAD) and never reach a model.
        """
        async for raw in upstream:
            if isinstance(raw, bytes):
                continue
            await client.send_text(raw)

            if not auto_query:
                continue

            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if event.get("type") != "final":
                continue

            text = (event.get("text") or "").strip()
            # An utterance caught in the same breath as the wake word carries it
            # in front: "hey jarvis what is X". Asking the agent that verbatim
            # wastes tokens and confuses retrieval.
            if wake_word and text:
                from media.wake import strip_wake_prefix

                text = strip_wake_prefix(text, wake_phrases) or text

            if len(text) < auto_query_min_chars:
                await client.send_json({
                    "type": "skipped", "reason": "too_short", "text": text,
                    "detail": f"Under {auto_query_min_chars} characters; treated as noise.",
                })
                continue

            await answer_utterance(text)

    try:
        await client.send_json({"type": "connected", "session_id": session_id, "auto_query": auto_query})
        await upstream.send(json.dumps(config))

        up = asyncio.create_task(pump_up())
        down = asyncio.create_task(pump_down())
        done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, WebSocketDisconnect):
                raise exc

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        try:
            await client.send_json({"type": "error", "message": str(exc)})
        except Exception:
            pass
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await client.close()
        except Exception:
            pass
