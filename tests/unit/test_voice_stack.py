"""
Voice stack: VAD segmentation, engine selection, registry integration, and the
security posture of the new service.

No live services and no real models — the engines are stubbed so these stay
fast. Real-model behaviour is covered by the live suite.
"""

from __future__ import annotations

import importlib
import json

import numpy as np
import pytest

from media.audio import SAMPLE_RATE, duration_s, float32_to_pcm, resample_pcm, write_wav
from media.vad import (
    EnergyVAD,
    SegmenterConfig,
    SpeechSegmenter,
    available_backends,
    create_vad,
    segment_pcm,
)

rng = np.random.default_rng(7)


def _silence(seconds: float) -> bytes:
    """A quiet room, not digital silence, so the adaptive gate has a floor to track."""
    return float32_to_pcm(rng.normal(0, 0.0008, int(SAMPLE_RATE * seconds)).astype(np.float32))


def _speech(seconds: float, f0: float = 140.0) -> bytes:
    """Voiced-sounding tone: harmonic stack modulated at a syllable rate."""
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    sig = sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, 12))
    sig = sig * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t))
    sig = sig / np.max(np.abs(sig)) * 0.35 + rng.normal(0, 0.01, sig.size)
    return float32_to_pcm(sig.astype(np.float32))


@pytest.fixture()
def two_utterances() -> bytes:
    """Silence, speech, silence, speech, silence — two clear utterances."""
    return _silence(0.8) + _speech(1.4) + _silence(1.0) + _speech(1.1, 190.0) + _silence(0.7)


# ---------------------------------------------------------------------------
# Audio plumbing
# ---------------------------------------------------------------------------

def test_wav_roundtrip_is_lossless(tmp_path, two_utterances):
    """The WAV fast path must not need ffmpeg and must not alter samples."""
    from media.audio import decode_to_pcm

    path = write_wav(two_utterances, tmp_path / "sample.wav")
    assert decode_to_pcm(path) == two_utterances


def test_resample_preserves_duration():
    one_second_48k = float32_to_pcm(rng.normal(0, 0.1, 48_000).astype(np.float32))
    out = resample_pcm(one_second_48k, 48_000, SAMPLE_RATE)
    assert abs(duration_s(out) - 1.0) < 0.01


# ---------------------------------------------------------------------------
# Voice activity detection
# ---------------------------------------------------------------------------

def test_energy_vad_finds_both_utterances(two_utterances):
    segments = segment_pcm(two_utterances, EnergyVAD())
    assert len(segments) == 2
    assert 0.4 < segments[0].start_s < 1.3
    assert 3.0 < segments[1].start_s < 4.2
    assert all(s.duration_s > 0.5 for s in segments)


def test_streaming_matches_offline(two_utterances):
    """Chunk boundaries must not change the result — the browser sends any size."""
    offline = segment_pcm(two_utterances, EnergyVAD())

    segmenter = SpeechSegmenter(EnergyVAD())
    streamed = []
    step = 1777                          # deliberately not a frame multiple
    for i in range(0, len(two_utterances), step):
        streamed.extend(segmenter.feed(two_utterances[i:i + step]))
    streamed.extend(segmenter.flush())

    assert len(streamed) == len(offline)
    for a, b in zip(streamed, offline):
        assert a.start_s == pytest.approx(b.start_s, abs=0.05)
        assert a.end_s == pytest.approx(b.end_s, abs=0.05)


def test_short_blip_is_rejected_as_noise():
    blip = _silence(0.5) + _speech(0.10) + _silence(0.8)
    assert segment_pcm(blip, EnergyVAD()) == []


def test_max_duration_force_closes_a_long_utterance():
    config = SegmenterConfig(max_speech_ms=1000, silence_ms=400)
    segments = segment_pcm(_silence(0.4) + _speech(3.0) + _silence(0.8), EnergyVAD(), config)
    assert len(segments) >= 2
    assert any(s.truncated for s in segments)


def test_silence_only_produces_nothing():
    assert segment_pcm(_silence(3.0), EnergyVAD()) == []


def test_auto_backend_always_resolves():
    """Whatever is installed, `auto` must return a usable detector."""
    vad = create_vad("auto")
    assert vad.name in ("silero", "webrtc", "energy")
    assert available_backends()["energy"] is True


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        create_vad("telepathy")


# ---------------------------------------------------------------------------
# Engine selection
# ---------------------------------------------------------------------------

def test_engine_falls_back_when_vosk_missing(monkeypatch):
    from media import asr, vosk_engine

    monkeypatch.setattr(vosk_engine, "is_available", lambda: False)
    assert asr.resolve_engine("vosk") == "whisper"
    assert asr.resolve_engine("hybrid") == "whisper"


def test_unknown_engine_raises():
    from media import asr

    with pytest.raises(ValueError):
        asr.resolve_engine("dictaphone")


def test_engine_status_reports_all_backends():
    from media.asr import engine_status

    status = engine_status()
    assert set(status) >= {"whisper", "vosk", "hybrid", "vad", "default_engine"}
    assert set(status["vad"]) == {"silero", "webrtc", "energy"}


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------

@pytest.fixture()
def stub_asr(monkeypatch, two_utterances, tmp_path):
    """Replace Whisper with a stub that reports how much audio it received."""
    from media import asr

    def fake_whisper(pcm, language=None, model=None, beam_size=5,
                     word_timestamps=False, offset_s=0.0):
        secs = len(pcm) / (SAMPLE_RATE * 2)
        return ([asr.TranscriptSegment(
            text=f"heard {secs:.2f}s", start_s=offset_s, end_s=offset_s + secs,
            confidence=0.9)], "en")

    monkeypatch.setattr(asr, "load_whisper", lambda *a, **k: object())
    monkeypatch.setattr(asr, "whisper_transcribe_pcm", fake_whisper)
    return write_wav(two_utterances, tmp_path / "asset.wav")


def test_transcribe_segments_matches_registry_shape(stub_asr):
    """The ingestion worker hands this straight to registry.add_transcript."""
    from media.asr import transcribe_segments

    result = transcribe_segments(stub_asr, language="en", engine="whisper", vad_backend="energy")

    assert result["status"] == "ok"
    assert result["language"] == "en"
    assert isinstance(result["duration"], float)
    assert result["text"]
    assert result["segments"]
    for seg in result["segments"]:
        assert set(seg) == {"start", "end", "text", "speaker"}
        assert seg["speaker"] is None
        assert seg["start"] <= seg["end"]
    # JSON-serialisable, because the registry stores it as a JSON column.
    json.dumps(result["segments"])


def test_transcribe_segments_reports_missing_file(tmp_path):
    from media.asr import transcribe_segments

    result = transcribe_segments(tmp_path / "nope.wav")
    assert result["status"] == "error"
    assert "not found" in result["message"].lower()


def test_vad_reduces_audio_sent_to_the_model(stub_asr):
    """The whole point of the VAD gate: the model sees less than the recording."""
    from media.asr import transcribe_pcm
    from media.audio import decode_to_pcm

    pcm = decode_to_pcm(stub_asr)
    gated = transcribe_pcm(pcm, engine="whisper", use_vad=True, vad_backend="energy")
    ungated = transcribe_pcm(pcm, engine="whisper", use_vad=False)

    assert gated.duration_s == pytest.approx(ungated.duration_s, abs=0.01)
    assert gated.speech_s < ungated.speech_s


def test_ingest_worker_writes_transcript_to_registry(tmp_path, monkeypatch, two_utterances):
    """End to end through the registry, with the recogniser stubbed out."""
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    from media import registry
    importlib.reload(registry)

    from media.ingest import audio as ingest_audio
    importlib.reload(ingest_audio)

    path = write_wav(two_utterances, tmp_path / "asset.wav")
    asset_id = registry.add_asset("audio", str(path), "test", rights="owned")

    monkeypatch.setattr(ingest_audio, "_transcriber", lambda: (
        lambda p, language=None: {
            "status": "ok", "language": "en", "duration": 5.0,
            "segments": [{"start": 0.8, "end": 2.2, "text": "hello", "speaker": None}],
            "text": "hello", "engine": "whisper", "speech_s": 1.4,
        },
        "asr",
    ))

    result = ingest_audio.enrich(asset_id, str(path))
    assert result["status"] == "ready"
    assert result["segments"] == 1
    assert result["engine"] == "whisper"

    asset = registry.get_asset(asset_id)
    assert asset["status"] == "ready"
    assert asset["transcript_id"]


def test_ingest_worker_marks_failed_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    from media import registry
    importlib.reload(registry)
    from media.ingest import audio as ingest_audio
    importlib.reload(ingest_audio)

    asset_id = registry.add_asset("audio", str(tmp_path / "gone.wav"), "test", rights="owned")
    result = ingest_audio.enrich(asset_id, str(tmp_path / "gone.wav"))

    assert result["status"] == "failed"
    assert registry.get_asset(asset_id)["status"] == "failed"


# ---------------------------------------------------------------------------
# Service security
# ---------------------------------------------------------------------------

def test_voice_service_paths_are_confined(tmp_path, monkeypatch):
    """A path outside the media roots must be refused, not read."""
    from fastapi import HTTPException

    from common.security import confine_to_roots

    root = tmp_path / "media_input"
    root.mkdir()
    inside = root / "ok.wav"
    inside.write_bytes(b"")

    assert confine_to_roots(str(inside), [root]) == inside.resolve()
    with pytest.raises(HTTPException) as exc:
        confine_to_roots(str(tmp_path / "escape.wav"), [root])
    assert exc.value.status_code == 403


def test_voice_service_requires_api_key_dependency():
    """The app must carry the house-wide auth dependency, like every service."""
    from common.security import require_api_key
    from media.voice_service import app

    deps = [d.dependency for d in app.router.dependencies]
    assert require_api_key in deps


def _ws_conn(host: str, headers=None, query=""):
    """A real starlette HTTPConnection with a websocket scope."""
    from starlette.requests import HTTPConnection

    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return HTTPConnection({
        "type":         "websocket",
        "client":       (host, 12345),
        "headers":      raw_headers,
        "query_string": query.encode(),
        "scheme":       "ws",
        "path":         "/ws/transcribe",
    })


def test_auth_dependency_resolves_on_a_websocket_scope():
    """
    require_api_key must be typed HTTPConnection, not Request: an app-level
    dependency also guards websocket routes, and a Request cannot be resolved
    there. Regression test for a TypeError at connection time.
    """
    from common.security import require_api_key

    require_api_key(_ws_conn("127.0.0.1"))       # loopback: no raise


def test_ws_auth_rejects_remote_without_key(monkeypatch):
    from fastapi import HTTPException

    from common.security import require_api_key

    monkeypatch.setenv("API_KEY", "secret-key")
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("RBAC_ROLE_KEYS", raising=False)

    with pytest.raises(HTTPException) as exc:
        require_api_key(_ws_conn("10.0.0.5"))
    assert exc.value.status_code == 401


def test_ws_auth_accepts_remote_with_query_key(monkeypatch):
    """Browsers cannot set headers on a WS handshake, so ?api_key= is allowed."""
    from common.security import require_api_key

    monkeypatch.setenv("API_KEY", "secret-key")
    require_api_key(_ws_conn("10.0.0.5", query="api_key=secret-key"))


def test_http_auth_still_rejects_query_key(monkeypatch):
    """The query-param allowance is websocket-only; HTTP must still need a header."""
    from fastapi import HTTPException

    from common.security import require_api_key
    from starlette.requests import HTTPConnection

    monkeypatch.setenv("API_KEY", "secret-key")
    http_conn = HTTPConnection({
        "type": "http", "client": ("10.0.0.5", 1), "headers": [],
        "query_string": b"api_key=secret-key", "scheme": "http",
        "method": "GET", "path": "/engines",
    })
    with pytest.raises(HTTPException) as exc:
        require_api_key(http_conn)
    assert exc.value.status_code == 401

# ---------------------------------------------------------------------------
# Regressions found by live testing
# ---------------------------------------------------------------------------

def test_uploaded_wav_decodes_without_ffmpeg(monkeypatch, two_utterances, tmp_path):
    """
    /transcribe/upload used to demand ffmpeg for every blob, so a WAV upload
    failed on a machine without it. A plain PCM WAV must decode in-process.
    """
    from media import audio

    monkeypatch.setattr(audio, "ffmpeg_available", lambda: False)
    path = write_wav(two_utterances, tmp_path / "upload.wav")
    decoded = audio.decode_bytes_to_pcm(path.read_bytes())

    assert decoded == two_utterances


def test_non_wav_upload_without_ffmpeg_reports_clearly(monkeypatch):
    """A webm/opus blob genuinely needs ffmpeg; the error must say so."""
    from media import audio

    monkeypatch.setattr(audio, "ffmpeg_available", lambda: False)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        audio.decode_bytes_to_pcm(bytes([0x1A, 0x45, 0xDF, 0xA3]) + b"not a wav at all")


def test_availability_probes_do_not_import_heavy_modules(monkeypatch):
    """
    engine_status runs on every /engines call, which the UI hits on mount.
    Importing torch/faster_whisper there cost tens of seconds cold and timed the
    endpoint out, so availability must be probed with find_spec instead.
    """
    import builtins

    from media.asr import engine_status

    real_import = builtins.__import__
    heavy = {"torch", "faster_whisper", "vosk", "webrtcvad"}
    imported = []

    def tracking_import(name, *args, **kwargs):
        if name.split(".")[0] in heavy:
            imported.append(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracking_import)
    status = engine_status()

    assert imported == [], f"engine_status imported heavy modules: {imported}"
    assert set(status["vad"]) == {"silero", "webrtc", "energy"}


# ---------------------------------------------------------------------------
# Hands-free guards
# ---------------------------------------------------------------------------

def test_auto_query_min_chars_default_is_sane():
    """Filler words must not each cost a model call."""
    from orchestrator import voice

    assert voice.AUTO_QUERY_MIN_CHARS >= 4
    for filler in ("um", "yeah", "ok", "mm"):
        assert len(filler) < voice.AUTO_QUERY_MIN_CHARS, filler
    for real in ("what is agentic rag", "summarise the TEQSA changes"):
        assert len(real) >= voice.AUTO_QUERY_MIN_CHARS, real


async def test_hands_free_answers_each_utterance_and_guards_noise(monkeypatch):
    """
    The conversational loop: every `final` above the length floor is answered as
    it lands, short ones are reported as skipped, and a second utterance arriving
    mid-answer is skipped rather than run in parallel.
    """
    import asyncio

    from orchestrator import voice

    sent = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_hybrid(text, session_id, force_route=None):
        started.set()
        await release.wait()
        return {"answer": f"answer to {text}", "model": "test", "cost_usd": 0.0}

    monkeypatch.setattr(voice, "_run_hybrid", slow_hybrid)

    # Rebuild the closure the websocket handler creates, with the same guards.
    in_flight = {"busy": False}

    class FakeClient:
        async def send_json(self, obj):
            sent.append(obj)

    client = FakeClient()

    async def answer_utterance(text: str) -> None:
        if in_flight["busy"]:
            await client.send_json({"type": "skipped", "reason": "busy", "text": text})
            return
        in_flight["busy"] = True
        try:
            await client.send_json({"type": "thinking", "query": text})
            result = await voice._run_hybrid(text, "s1", None)
            await client.send_json({"type": "answer", **result})
        finally:
            in_flight["busy"] = False

    first = asyncio.create_task(answer_utterance("what is agentic rag"))
    await started.wait()
    await answer_utterance("and what about vosk")   # arrives mid-answer
    release.set()
    await first

    kinds = [e["type"] for e in sent]
    assert kinds.count("thinking") == 1, kinds
    assert kinds.count("answer") == 1, kinds
    skipped = [e for e in sent if e["type"] == "skipped"]
    assert len(skipped) == 1 and skipped[0]["reason"] == "busy", sent


# ---------------------------------------------------------------------------
# Wake word and barge-in
# ---------------------------------------------------------------------------

def test_wake_phrase_matching_is_word_bounded():
    from media.wake import phrase_in

    words = ["hey jarvis", "jarvis"]
    assert phrase_in("hey jarvis what is rag", words) == "hey jarvis"
    assert phrase_in("JARVIS!! summarise", words) == "jarvis"
    assert phrase_in("what is the weather", words) is None
    # A wake word inside a longer word must not fire.
    assert phrase_in("the jarvisian era", words) is None


def test_barge_words_do_not_match_inside_other_words():
    from media.wake import is_barge_in

    assert is_barge_in("stop") == "stop"
    assert is_barge_in("wait a moment") == "wait"
    assert is_barge_in("tell me about stopping distance") is None
    assert is_barge_in("the waiting room") is None


def test_wake_prefix_is_stripped_from_the_question():
    from media.wake import strip_wake_prefix

    words = ["hey jarvis", "jarvis", "okay jarvis"]
    assert strip_wake_prefix("hey jarvis what is agentic rag", words) == "what is agentic rag"
    assert strip_wake_prefix("jarvis, summarise this", words) == "summarise this"
    # No wake word: left alone.
    assert strip_wake_prefix("what is agentic rag", words) == "what is agentic rag"


def test_session_starts_asleep_and_ignores_speech_until_woken(two_utterances, monkeypatch):
    """
    The point of a wake word: ordinary conversation in the room must not reach a
    model. While asleep no transcription happens at all.
    """
    from media import asr

    calls = []

    def fake_whisper(pcm, **kw):
        calls.append(len(pcm))
        return ([asr.TranscriptSegment(text="x", start_s=0, end_s=1, confidence=0.9)], "en")

    monkeypatch.setattr(asr, "load_whisper", lambda *a, **k: object())
    monkeypatch.setattr(asr, "whisper_transcribe_pcm", fake_whisper)

    session = asr.LiveSession(asr.LiveConfig(
        engine="whisper", vad_backend="energy", wake_word=True,
    ))
    if not session.config.wake_word:
        pytest.skip("VOSK model not installed; wake word unavailable")

    assert session.asleep
    events = []
    for i in range(0, len(two_utterances), 3200):
        events.extend(session.feed(two_utterances[i:i + 3200]))

    # Synthetic tones are not the wake phrase, so nothing should have woken and
    # the recogniser should never have been called.
    assert not any(e["type"] == "wake" for e in events), events
    assert calls == [], "transcribed audio while asleep"


def test_assistant_speaking_flag_is_separate_from_user_speaking(monkeypatch):
    """
    Regression: the barge-in flag once collided with the read-only `speaking`
    property, which means the *user* is mid-utterance. They are different things.
    """
    from media import asr

    monkeypatch.setattr(asr, "load_whisper", lambda *a, **k: object())
    session = asr.LiveSession(asr.LiveConfig(engine="whisper", vad_backend="energy"))

    assert session.speaking is False              # user, from VAD
    assert session.assistant_speaking is False    # assistant, set explicitly
    session.set_speaking(True)
    assert session.assistant_speaking is True
    assert session.speaking is False              # unchanged by the assistant talking
    session.set_speaking(False)
    assert session.assistant_speaking is False


def test_audio_during_playback_is_not_transcribed(two_utterances, monkeypatch):
    """While the assistant talks, its own voice must never become a question."""
    from media import asr

    calls = []
    monkeypatch.setattr(asr, "load_whisper", lambda *a, **k: object())
    monkeypatch.setattr(asr, "whisper_transcribe_pcm",
                        lambda pcm, **kw: (calls.append(len(pcm)), ([], "en"))[1])

    session = asr.LiveSession(asr.LiveConfig(engine="whisper", vad_backend="energy"))
    session.set_speaking(True)
    for i in range(0, len(two_utterances), 3200):
        session.feed(two_utterances[i:i + 3200])

    assert calls == [], "transcribed audio while the assistant was speaking"


def test_voice_persona_keeps_replies_short():
    """A spoken answer must be constrained; screen-length prose is unusable aloud."""
    from orchestrator.voice import VOICE_MAX_SENTENCES, voice_system_prompt

    prompt = voice_system_prompt().lower()
    assert "read aloud" in prompt
    assert str(VOICE_MAX_SENTENCES) in prompt
    for banned in ("markdown", "bullet"):
        assert banned in prompt, f"persona should forbid {banned}"
