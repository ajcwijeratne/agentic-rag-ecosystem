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
VOICE_ASSISTANT_NAME: str = os.getenv("VOICE_ASSISTANT_NAME", "Apex")
VOICE_MAX_SENTENCES: int = int(os.getenv("VOICE_MAX_SENTENCES", "3"))
VOICE_PERSONA: str = os.getenv("VOICE_PERSONA", "").strip()

# Model used for spoken turns. Providers differ enormously in when they emit the
# first token, which is the only thing that matters once answers are streamed
# into speech. Measured on this stack with a short prompt:
#
#   anthropic/claude-sonnet-4-6   first token 1.45s of 3.09s   34 chunks
#   openai/gpt-4o                 first token 1.78s of 2.07s   39 chunks
#   openai/gpt-4o-mini            first token 2.57s of 3.01s   54 chunks
#   google/gemini-2.5-flash-lite  first token 1.38s of 1.43s    3 chunks
#
# Gemini buffers server-side and delivers in a couple of bursts, so streaming
# buys nothing there however fast the total is. Blank falls back to the normal
# router, which optimises for cost rather than time-to-first-word.
VOICE_MODEL_KEY: str = os.getenv("VOICE_MODEL_KEY", "anthropic/claude-sonnet-4-6").strip()

# Context chunks kept for a spoken answer. Retrieval returns a dozen, which is
# right for a written reply that can cite them all and wrong for three spoken
# sentences: the extra chunks are pure prompt-processing latency before the
# first word is heard. Written chat is unaffected.
VOICE_CONTEXT_CHUNKS: int = int(os.getenv("VOICE_CONTEXT_CHUNKS", "4"))

# Send a compact prompt for spoken turns instead of the full written brief. The
# written one is about 40,000 characters before any context is added; see
# spoken_system_prompt for what that costs a listener.
VOICE_LEAN_PROMPT: bool = os.getenv("VOICE_LEAN_PROMPT", "true").lower() in ("1", "true", "yes")

# Hang guard on retrieval, not a latency optimisation.
#
# It is tempting to cut this short: the three agents run in parallel but finish
# unevenly, local_data returning useful chunks in about 1.0s while the cloud
# agent takes about 3.9s and returns nothing. But rag_node gathers all three, so
# it is all-or-nothing — measured, a 1.5s deadline produced zero chunks rather
# than local_data's twelve, and spoken answers regressed to "I don't have any
# information about that in my knowledge base". Trading vault answers for
# latency is the wrong trade.
#
# Shortening this safely means per-agent deadlines inside rag_node, so a slow
# agent is dropped while the fast ones still count. Until then this only stops a
# wedged agent hanging the turn forever.
VOICE_RETRIEVAL_TIMEOUT_S: float = float(os.getenv("VOICE_RETRIEVAL_TIMEOUT_S", "8.0"))


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

async def _direct_answer(query: str, session_id: str, force_route: str | None = None):
    """
    Answers that need no model and so cannot be streamed: Command Centre
    control, and questions about what is on screen. Returns None to fall
    through to the streaming agent path.
    """
    from .screen import answer_about_screen
    from .voice_commands import (
        execute as run_command, interpret, resolve_pending, set_pending,
    )

    approved, reply = resolve_pending(session_id, query)
    if approved is not None:
        return await run_command(approved)
    if reply is not None:
        return {"answer": reply, "route": "ui", "model": "", "cost_usd": 0.0}

    cmd = interpret(query)
    if cmd is not None:
        if cmd.confirm:
            set_pending(session_id, cmd)
            return {"answer": cmd.confirm, "route": "ui", "model": "", "cost_usd": 0.0,
                    "ui": {"pending": cmd.target, "kind": cmd.kind}}
        return await run_command(cmd)

    seen = await answer_about_screen(query, spoken=True)
    if seen is not None:
        return seen
    return None


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

    # Command Centre control comes first: it is deterministic and free, and
    # "show me deliverables" must feel like pressing the button rather than
    # waiting on a model.
    if spoken:
        from .voice_commands import (
            execute as run_command, interpret, resolve_pending, set_pending,
        )

        # Is this a yes/no to something we just offered to do?
        approved, reply = resolve_pending(session_id, query)
        if approved is not None:
            return {"session_id": session_id, "query": query, **(await run_command(approved))}
        if reply is not None:
            return {"session_id": session_id, "query": query, "answer": reply,
                    "route": "ui", "model": "", "cost_usd": 0.0}

        cmd = interpret(query)
        if cmd is not None:
            if cmd.confirm:
                # Costly or state-changing: say what it would do and wait for a
                # yes rather than acting on a possible mishearing.
                set_pending(session_id, cmd)
                return {"session_id": session_id, "query": query,
                        "answer": cmd.confirm, "route": "ui", "model": "",
                        "cost_usd": 0.0,
                        "ui": {"pending": cmd.target, "kind": cmd.kind}}
            return {"session_id": session_id, "query": query, **(await run_command(cmd))}

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


def spoken_system_prompt(rag_context: list) -> str:
    """
    A compact system prompt for spoken turns.

    The written path builds about 40,000 characters — roughly 10,000 tokens of
    department brief, ABOUT ME and knowledge base — before any retrieved context
    is added. That is right for a written answer that must hold the whole
    business in mind, and wrong for three spoken sentences: measured, it pushed
    time-to-first-token from about 1.5s to about 6s, and the listener hears all
    of that as silence.

    A spoken turn therefore gets identity, the retrieved context and the brevity
    rules, and nothing else. VOICE_LEAN_PROMPT=false restores the full brief.
    """
    if not VOICE_LEAN_PROMPT:
        from .wijerco_agent import _build_system_prompt

        full = _build_system_prompt("research_intelligence", rag_context)
        return full + "\n\n---\n\n" + voice_system_prompt()

    parts = [
        "You are " + VOICE_ASSISTANT_NAME + ", the voice of Aaron Wijeratne's "
        "Command Centre. Aaron is an Academic Director in Australian higher "
        "education; answer in that context."
    ]

    lines = []
    for chunk in rag_context or []:
        if isinstance(chunk, dict):
            text = (chunk.get("text") or "").strip()
            source = chunk.get("file") or chunk.get("source") or ""
        else:
            text, source = str(chunk).strip(), ""
        if text:
            lines.append("- " + text[:700] + ("  [" + str(source) + "]" if source else ""))
    if lines:
        parts.append(
            "Answer from this retrieved context where it is relevant:\n"
            + "\n".join(lines)
        )

    parts.append(voice_system_prompt())
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Streaming spoken answers
# ---------------------------------------------------------------------------

async def stream_spoken_answer(query: str, session_id: str, force_route: str | None = None):
    """
    Answer a spoken question, yielding speakable fragments as they are generated.

    This exists because the blocking path made the assistant unusable by voice:
    measured end to end, it waited 12.7s for the complete answer before making
    any sound, which was 86% of the time from wake word to first audio. Here the
    first sentence is spoken as soon as it exists, so the wait collapses to
    roughly time-to-first-sentence while the rest generates behind it.

    Yields:
        {"type": "speak",  "text": "...", "index": 0}   one per fragment
        {"type": "answer", "answer": "...", "route": ..., "model": ...}

    Also fixes where the voice persona was being lost. run_hybrid builds the
    graph's initial_state from `query` alone, so conversation_history — which
    carried the brevity instruction — never reached the RAG route, and spoken
    replies ran to hundreds of characters. Here the system prompt is assembled
    directly, so the persona always applies, and cap_sentences enforces it
    afterwards rather than trusting the model to obey.
    """
    from .fallback_chain import stream_with_fallback
    from .main import graph
    from .session_store import get_history_for_llm
    from .speech_chunks import SentenceChunker, cap_sentences
    from .state import AgentState
    from .wijerco_router import classify_intent

    route = force_route or classify_intent(query).target
    department = None
    if route not in ("rag",):
        department = classify_intent(query).department or "research_intelligence"

    # Retrieval only — deliberately not the whole graph.
    #
    # graph.ainvoke runs route -> rag -> llm -> synthesize, so invoking it just
    # to collect context_chunks fires a complete model call whose answer is then
    # thrown away, and the real answer is generated a second time below. That
    # doubling is what made a streamed turn measure 62s against 12.7s for the
    # blocking path. rag_node is the retrieval step on its own.
    rag_context: list = []
    if route in ("rag", "hybrid"):
        try:
            from .graph import rag_node

            state: AgentState = {
                "messages": [], "query": query, "routing": None,
                "context_chunks": [], "output_payload": {},
                "agents_used": [], "errors": [], "finished": False,
            }
            try:
                retrieved = await asyncio.wait_for(
                    rag_node(state), timeout=VOICE_RETRIEVAL_TIMEOUT_S
                )
                rag_context = (retrieved.get("context_chunks", []) or [])[:VOICE_CONTEXT_CHUNKS]
            except asyncio.TimeoutError:
                # Answer from the model's own knowledge rather than make the
                # listener wait on a straggling agent.
                rag_context = []
        except Exception:
            pass

    system = spoken_system_prompt(rag_context)

    chunker = SentenceChunker()
    spoken_index = 0
    full = ""
    model_key = ""
    cost = 0.0

    async for event in stream_with_fallback(
        user_message=query, system_prompt=system,
        history=get_history_for_llm(session_id),
        force_model_key=VOICE_MODEL_KEY or None,
    ):
        token = event.get("token") or ""
        if token:
            full += token
            for fragment in chunker.push(token):
                yield {"type": "speak", "text": fragment, "index": spoken_index}
                spoken_index += 1
        if event.get("done"):
            model_key = event.get("model_key", "") or model_key
            cost = event.get("cost_usd", 0.0) or cost

    for fragment in chunker.flush():
        yield {"type": "speak", "text": fragment, "index": spoken_index}
        spoken_index += 1

    answer = cap_sentences(full.strip(), VOICE_MAX_SENTENCES)
    add_voice_turn(session_id, query, answer, model_key, cost)
    yield {
        "type": "answer", "answer": answer, "route": route,
        "department": department, "model": model_key, "cost_usd": cost,
        "spoken_fragments": spoken_index,
    }


def add_voice_turn(session_id: str, query: str, answer: str, model_key: str, cost: float) -> None:
    """Record a spoken turn so follow-up questions have context."""
    try:
        from .session_store import add_message

        add_message(session_id, "user", query, cost_usd=0.0)
        add_message(session_id, "assistant", answer, model_key=model_key, cost_usd=cost)
    except Exception:
        pass


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
        """Browser -> voice service, intercepting the commands we handle here."""
        while True:
            message = await client.receive()

            # A typed question routed through the spoken pipeline. Lets the
            # composer use the same streaming answer path as the microphone,
            # and makes that path testable without audio.
            text_frame = message.get("text")
            if text_frame:
                try:
                    parsed = json.loads(text_frame)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("type") == "ask":
                    asked = (parsed.get("text") or "").strip()
                    if asked:
                        asyncio.create_task(answer_utterance(asked))
                    continue
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

            # Commands and screen questions resolve immediately and have nothing
            # to stream, so they answer in one event as before.
            direct = await _direct_answer(text, session_id, force_route)
            if direct is not None:
                await client.send_json({"type": "answer", "session_id": session_id, **direct})
                return

            # Everything else streams: each finished sentence is sent the moment
            # it exists so the client can start speaking, instead of waiting for
            # the whole answer.
            async for event in stream_spoken_answer(text, session_id, force_route):
                await client.send_json({"session_id": session_id, **event})
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
            # in front: "hey apex what is X". Asking the agent that verbatim
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
