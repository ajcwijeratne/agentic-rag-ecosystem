"""
Apex speech output: text hygiene, engine fallback, the clone-voice guard, the
HTTP routes, and the voice console verbs.

No models and no network: engines are stubbed. Real Kokoro timing is measured
with `python -m media.tts --say ...` on the machine that will run it.
"""

from __future__ import annotations

import asyncio
import io
import wave

import pytest

from media import tts


def _fake_speech(engine="kokoro", seconds=0.5):
    return tts.Speech(pcm=b"\x01\x00" * int(tts.SAMPLE_RATE * seconds), sample_rate=tts.SAMPLE_RATE,
                      engine=engine, voice="bf_emma", synth_ms=12.0)


# ---------------------------------------------------------------------------
# Text hygiene
# ---------------------------------------------------------------------------

def test_speakable_strips_markdown_links_code_and_emoji():
    raw = ("## Summary\n- **Three** approvals are waiting 🎉\n"
           "See [the queue](https://x.io/q) or https://example.com/a.\n```py\nprint(1)\n```")
    out = tts.speakable(raw)
    assert "#" not in out and "**" not in out and "```" not in out and "print" not in out
    assert "the queue" in out and "https" not in out and "the link on screen" in out
    assert "🎉" not in out
    assert out.startswith("Summary Three approvals")


def test_speakable_keeps_numbers_and_sentences():
    assert tts.speakable("It rose 3.5 per cent. Then it fell.") == "It rose 3.5 per cent. Then it fell."


def test_speakable_drops_stage_directions():
    assert tts.speakable("Opening it now <<glance left>> for you.") == "Opening it now for you."


# ---------------------------------------------------------------------------
# Engines and fallback
# ---------------------------------------------------------------------------

def test_wav_container_is_valid():
    s = _fake_speech(seconds=0.25)
    with wave.open(io.BytesIO(s.to_wav())) as w:
        assert w.getframerate() == tts.SAMPLE_RATE and w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getnframes() == int(tts.SAMPLE_RATE * 0.25)
    assert abs(s.duration_s - 0.25) < 1e-6


def test_auto_never_spends_on_elevenlabs_unless_it_is_the_default(monkeypatch):
    calls = []
    monkeypatch.setattr(tts, "TTS_ENGINE", "auto")
    monkeypatch.setattr(tts, "_eleven_synth", lambda *a, **k: calls.append("el") or _fake_speech("elevenlabs"))
    monkeypatch.setattr(tts._kokoro, "synth", lambda *a, **k: _fake_speech("kokoro"))
    s = tts.synthesize("Hello there.")
    assert s.engine == "kokoro" and calls == []


def test_cloud_failure_falls_back_to_kokoro_and_says_so(monkeypatch):
    def boom(*a, **k):
        raise tts.TTSUnavailable("ElevenLabs returned 401")

    monkeypatch.setattr(tts, "_eleven_synth", boom)
    monkeypatch.setattr(tts._kokoro, "synth", lambda *a, **k: _fake_speech("kokoro"))
    s = tts.synthesize("Hello there.", engine="elevenlabs")
    assert s.engine == "kokoro"
    assert s.fallback_from == "elevenlabs"
    assert "401" in s.meta["fallback_reason"]


def test_network_errors_are_a_fallback_not_a_crash(monkeypatch):
    monkeypatch.setattr(tts, "_eleven_synth", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    monkeypatch.setattr(tts._kokoro, "synth", lambda *a, **k: _fake_speech("kokoro"))
    assert tts.synthesize("Hi.", engine="elevenlabs").engine == "kokoro"


def test_nothing_available_raises_for_the_browser_fallback(monkeypatch):
    def nope(*a, **k):
        raise tts.TTSUnavailable("kokoro-onnx is not installed")

    monkeypatch.setattr(tts._kokoro, "synth", nope)
    with pytest.raises(tts.TTSUnavailable):
        tts.synthesize("Hello.", engine="kokoro")


def test_empty_text_is_unavailable_not_silence(monkeypatch):
    monkeypatch.setattr(tts._kokoro, "synth", lambda *a, **k: _fake_speech())
    with pytest.raises(tts.TTSUnavailable):
        tts.synthesize("```code only```")


def test_text_is_capped_per_request(monkeypatch):
    seen = {}
    monkeypatch.setattr(tts, "TTS_MAX_CHARS", 50)
    monkeypatch.setattr(tts._kokoro, "synth", lambda text, v, s: seen.setdefault("t", text) and _fake_speech())
    tts.synthesize("word " * 100)
    assert len(seen["t"]) <= 50


# ---------------------------------------------------------------------------
# The clone guard: Apex must never speak in Aaron's cloned voice
# ---------------------------------------------------------------------------

def test_elevenlabs_needs_apex_own_voice_not_the_clone(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "clone-123")
    monkeypatch.delenv("APEX_ELEVENLABS_VOICE_ID", raising=False)
    ok, why = tts.elevenlabs_ready()
    assert not ok and "APEX_ELEVENLABS_VOICE_ID" in why


def test_elevenlabs_refuses_when_apex_voice_is_the_clone(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "clone-123")
    monkeypatch.setenv("APEX_ELEVENLABS_VOICE_ID", "clone-123")
    ok, why = tts.elevenlabs_ready()
    assert not ok and "clone" in why


def test_elevenlabs_ready_with_a_library_voice(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k")
    monkeypatch.setenv("ELEVENLABS_VOICE_ID", "clone-123")
    monkeypatch.setenv("APEX_ELEVENLABS_VOICE_ID", "library-456")
    assert tts.elevenlabs_ready() == (True, "")


def test_status_reports_engines_without_loading_models(monkeypatch):
    monkeypatch.setattr(tts._kokoro, "installed", lambda: False)
    st = tts.status()
    assert set(st["engines"]) == {"kokoro", "elevenlabs"}
    assert st["engines"]["kokoro"]["available"] is False
    assert st["voices"] and st["sample_rate"] == tts.SAMPLE_RATE


def test_model_dir_is_outside_the_repo(monkeypatch, tmp_path):
    monkeypatch.delenv("KOKORO_MODEL_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert tts.model_dir() == tmp_path / "wijerco" / "kokoro"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def _voice_service_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from media.tts_routes import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_tts_route_returns_wav_with_engine_headers(monkeypatch):
    monkeypatch.setattr(tts, "synthesize", lambda *a, **k: _fake_speech("kokoro", 0.3))
    r = _voice_service_client(monkeypatch).post("/tts", json={"text": "Hello."})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    assert r.content[:4] == b"RIFF"
    assert r.headers["x-tts-engine"] == "kokoro"
    assert "X-TTS-Engine" in r.headers["access-control-expose-headers"]


def test_tts_route_503_means_use_the_browser_voice(monkeypatch):
    def nope(*a, **k):
        raise tts.TTSUnavailable("no engine")

    monkeypatch.setattr(tts, "synthesize", nope)
    r = _voice_service_client(monkeypatch).post("/tts", json={"text": "Hello."})
    assert r.status_code == 503 and r.json()["fallback"] == "browser"


def _orchestrator_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from orchestrator.voice_tts import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_orchestrator_gates_the_paid_engine_on_operator(monkeypatch):
    monkeypatch.delenv("RBAC_ROLE_KEYS", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    r = _orchestrator_client().post("/voice/tts", json={"text": "Hi.", "engine": "elevenlabs"})
    assert r.status_code == 403


def test_orchestrator_falls_back_in_process_when_service_is_down(monkeypatch):
    import orchestrator.voice_tts as vt

    monkeypatch.setattr(vt, "VOICE_SERVICE_URL", "http://127.0.0.1:9")   # nothing listens here
    monkeypatch.setattr(vt, "VOICE_ALLOW_LOCAL", True)
    monkeypatch.setattr(tts, "synthesize", lambda *a, **k: _fake_speech("kokoro", 0.2))
    r = _orchestrator_client().post("/voice/tts", json={"text": "Hi.", "engine": "kokoro"})
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    assert r.headers["x-tts-engine"] == "kokoro"


def test_orchestrator_status_degrades_to_unavailable(monkeypatch):
    import orchestrator.voice_tts as vt

    monkeypatch.setattr(vt, "VOICE_SERVICE_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(vt, "VOICE_ALLOW_LOCAL", False)
    r = _orchestrator_client().get("/voice/tts/status")
    assert r.status_code == 200 and r.json()["mode"] == "unavailable"


# ---------------------------------------------------------------------------
# Voice console
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("said, verb", [
    ("Go hands free.", "mic_open"),
    ("hey apex, go hands free please", "mic_open"),
    ("Push-to-talk mode", "mic_ptt"),
    ("use the natural voice", "voice_natural"),
    ("Use the local voice.", "voice_local"),
    ("Apex, speak slower.", "slow_down"),
    ("show your face", "stage_open"),
])
def test_console_phrases_said_alone(said, verb):
    from orchestrator.voice_commands import interpret

    cmd = interpret(said)
    assert cmd is not None and cmd.kind == "voice" and cmd.target == verb


@pytest.mark.parametrize("said", [
    "Should I go hands free in lectures?",
    "what does push to talk mean for accessibility",
    "faster",
])
def test_console_never_fires_inside_a_sentence(said):
    from orchestrator.voice_commands import interpret

    cmd = interpret(said)
    assert cmd is None or cmd.kind != "voice"


def test_console_execute_hands_settings_to_the_browser():
    from orchestrator.voice_commands import execute, interpret

    out = asyncio.run(execute(interpret("go hands free")))
    assert out["ui"]["voice"] == {"mic_mode": "open"}
    assert out["cost_usd"] == 0.0 and out["model"] == "" and out["answer"]


def test_console_leaves_navigation_alone():
    from orchestrator.voice_commands import interpret

    assert interpret("show me deliverables").kind == "navigate"


# ---------------------------------------------------------------------------
# Talk-key wake: holding the key addresses Apex without the wake phrase
# ---------------------------------------------------------------------------

def test_talk_key_wakes_a_sleeping_session_without_the_phrase():
    import collections

    from media.asr import LiveSession

    class _Det:
        resets = 0

        def reset(self):
            _Det.resets += 1

    s = LiveSession.__new__(LiveSession)
    s.asleep = True
    s._wake = _Det()
    s._preroll = collections.deque([b"\x00\x00"])
    s._preroll_bytes = 2
    s._last_voice_at = 0.0
    assert s.wake() is True
    assert s.asleep is False and not s._preroll and s._preroll_bytes == 0 and _Det.resets == 1
    assert s._last_voice_at > 0
    assert s.wake() is False            # already awake: nothing to announce


# ---------------------------------------------------------------------------
# Installed but will not load (wijerco, 27 Sep 2026): say so, and keep hearing
# ---------------------------------------------------------------------------

def test_native_load_failure_names_the_windows_runtime(monkeypatch):
    monkeypatch.setattr(tts.os, "name", "nt")
    exc = FileNotFoundError("Could not find module 'C:\\x\\ctranslate2.dll' (or one of its dependencies).")
    msg = tts.native_load_hint("ctranslate2", exc)
    assert "will not load" in msg and "Visual C++ Redistributable" in msg


def test_kokoro_reports_a_native_failure_not_a_missing_package(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "kokoro_onnx":
            raise ImportError("DLL load failed while importing onnxruntime_pybind11_state")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    engine = tts._KokoroEngine()
    assert engine.installed() is False
    assert "will not load" in engine.error and "not installed" not in engine.error


def test_hearing_falls_back_to_vosk_when_whisper_cannot_load(monkeypatch):
    from media import asr, vosk_engine

    monkeypatch.setattr(asr, "_whisper_probe", (False, "ctranslate2 is installed but will not load"))
    monkeypatch.setattr(vosk_engine, "is_available", lambda: True)
    assert asr.resolve_engine("hybrid") == "vosk"
    assert asr.resolve_engine("whisper") == "vosk"


def test_whisper_that_cannot_load_is_not_advertised(monkeypatch):
    from importlib import util

    from media import asr

    monkeypatch.setattr(asr, "_whisper_probe", (False, "ctranslate2 is installed but will not load"))
    real_find_spec = util.find_spec
    monkeypatch.setattr(util, "find_spec", lambda n, *a: object() if n == "faster_whisper" else real_find_spec(n, *a))
    st = asr.engine_status()
    assert st["whisper"]["available"] is False
    assert "will not load" in st["whisper"]["error"]
    assert st["hybrid"]["available"] is False
