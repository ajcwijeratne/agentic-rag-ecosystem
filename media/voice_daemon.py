"""
Voice daemon — the always-on assistant
======================================
Owns the microphone outside the browser, so the assistant answers whether or not
a Command Centre tab is open. Same pipeline as the browser path (VAD, wake word,
Whisper/VOSK, barge-in); the differences are that audio comes from `sounddevice`
instead of an AudioWorklet, and replies are spoken through the local Windows
voices instead of the browser synthesiser.

    idle ── "hey apex" ──▶ listening ──▶ thinking ──▶ speaking ──┐
      ▲                                                            │
      └──────────────── wake timeout / reply finished ─────────────┘

Nothing reaches a model until the wake phrase is heard, and nothing leaves the
machine except the question itself, which goes to the orchestrator on localhost.

Run:
    python -m media.voice_daemon                 # listen on the default mic
    python -m media.voice_daemon --list-devices
    python -m media.voice_daemon --device 2
    python -m media.voice_daemon --wav sample.wav   # replay a file instead of a mic
    python -m media.voice_daemon --no-speak         # transcribe only, stay silent
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

import httpx

from .asr import LiveConfig, LiveSession
from .audio import SAMPLE_RATE, decode_to_pcm
from .wake import WAKE_TIMEOUT_S, WAKE_WORDS, strip_wake_prefix

ORCHESTRATOR_URL: str = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000")
DAEMON_ENGINE: str = os.getenv("VOICE_DAEMON_ENGINE", "hybrid")
DAEMON_TIMEOUT: float = float(os.getenv("VOICE_DAEMON_TIMEOUT_S", "180"))
BLOCK_FRAMES: int = 1600          # 100 ms at 16 kHz


# ---------------------------------------------------------------------------
# Speech out
# ---------------------------------------------------------------------------

class Speaker:
    """
    Local text-to-speech, interruptible.

    Prefers pyttsx3 (a thin wrapper over Windows SAPI); falls back to driving
    System.Speech through PowerShell, which needs no extra dependency. Both are
    offline. `stop()` cuts the current utterance short, which is what makes
    barge-in feel immediate rather than queued behind a long answer.
    """

    def __init__(self, enabled: bool = True, voice_hint: str = ""):
        self.enabled = enabled
        self.voice_hint = voice_hint or os.getenv("VOICE_DAEMON_VOICE", "")
        self._engine = None
        self._proc: subprocess.Popen | None = None
        self.backend = "none"
        if not enabled:
            return
        try:
            import pyttsx3

            self._engine = pyttsx3.init()
            if self.voice_hint:
                for v in self._engine.getProperty("voices"):
                    if self.voice_hint.lower() in v.name.lower():
                        self._engine.setProperty("voice", v.id)
                        break
            self._engine.setProperty("rate", int(os.getenv("VOICE_DAEMON_RATE", "185")))
            self.backend = "pyttsx3"
        except Exception:
            self.backend = "powershell" if sys.platform == "win32" else "none"

    def say(self, text: str) -> None:
        """Speak, blocking until finished or stopped."""
        if not self.enabled or not text.strip():
            return
        if self.backend == "pyttsx3":
            try:
                self._engine.say(text)
                self._engine.runAndWait()
                return
            except Exception:
                self.backend = "powershell"     # fall through on a dead engine

        if self.backend == "powershell":
            # -Command with a here-string keeps quoting sane for arbitrary text.
            script = (
                "Add-Type -AssemblyName System.Speech;"
                "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                + (f"try{{$s.SelectVoice('{self.voice_hint}')}}catch{{}};" if self.voice_hint else "")
                + "$s.Rate=1;$s.Speak([Console]::In.ReadToEnd());"
            )
            try:
                self._proc = subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", script],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self._proc.communicate(text.encode("utf-8", "replace"), timeout=DAEMON_TIMEOUT)
            except Exception:
                pass
            finally:
                self._proc = None

    def stop(self) -> None:
        """Cut the current utterance short."""
        try:
            if self.backend == "pyttsx3" and self._engine is not None:
                self._engine.stop()
            if self._proc is not None:
                self._proc.kill()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Audio sources
# ---------------------------------------------------------------------------

def mic_stream(device=None):
    """Yield 16 kHz mono int16 chunks from the microphone."""
    import sounddevice as sd

    q: queue.Queue = queue.Queue()

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        if status:
            print(f"[daemon] audio status: {status}", file=sys.stderr)
        q.put(bytes(indata))

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE, blocksize=BLOCK_FRAMES, device=device,
        dtype="int16", channels=1, callback=callback,
    ):
        while True:
            yield q.get()


def wav_stream(path: str, realtime: bool = True):
    """Replay a file as if it were the microphone. For testing without a mic."""
    pcm = decode_to_pcm(path)
    step = BLOCK_FRAMES * 2
    for i in range(0, len(pcm), step):
        yield pcm[i:i + step]
        if realtime:
            time.sleep(BLOCK_FRAMES / SAMPLE_RATE)


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

class VoiceDaemon:
    def __init__(
        self,
        engine: str = DAEMON_ENGINE,
        speak: bool = True,
        wake: bool = True,
        voice_hint: str = "",
        session_id: str = "",
        on_event=None,
    ):
        self.speaker = Speaker(enabled=speak, voice_hint=voice_hint)
        self.session_id = session_id or f"daemon-{int(time.time())}"
        self.on_event = on_event or (lambda e: None)
        self.session = LiveSession(LiveConfig(
            engine=engine,
            vad_backend="auto",
            wake_word=wake,
            wake_phrases=WAKE_WORDS,
            wake_timeout_s=WAKE_TIMEOUT_S,
        ))
        self._speaking_thread: threading.Thread | None = None
        self.turns = 0

    # -- reporting ---------------------------------------------------------

    def _emit(self, kind: str, **fields) -> None:
        event = {"type": kind, **fields}
        self.on_event(event)
        detail = fields.get("text") or fields.get("phrase") or fields.get("answer") or ""
        print(f"[daemon] {kind}{': ' + str(detail)[:100] if detail else ''}", flush=True)

    # -- one turn ----------------------------------------------------------

    def ask(self, question: str) -> str:
        """Send a question to the orchestrator and return the spoken answer."""
        from orchestrator.voice import voice_system_prompt

        payload = {
            "query": question,
            "session_id": self.session_id,
            "conversation_history": [{"role": "system", "content": voice_system_prompt()}],
        }
        headers = {}
        key = os.getenv("API_KEY", "").strip()
        if key:
            headers["X-API-Key"] = key
        try:
            r = httpx.post(
                f"{ORCHESTRATOR_URL}/hybrid", json=payload, headers=headers,
                timeout=DAEMON_TIMEOUT,
            )
            if r.status_code >= 400:
                return f"The assistant returned an error: {r.status_code}."
            return (r.json().get("answer") or "").strip()
        except Exception as exc:  # noqa: BLE001
            return f"I could not reach the orchestrator. {type(exc).__name__}."

    def handle_utterance(self, text: str) -> None:
        question = strip_wake_prefix(text, WAKE_WORDS).strip()
        if len(question) < 8:
            self._emit("skipped", text=question)
            return

        self.turns += 1
        self._emit("thinking", text=question)
        answer = self.ask(question)
        if not answer:
            answer = "I did not get an answer back. The retrieval or model services may be offline."
        self._emit("answer", answer=answer)

        # Speak on a worker thread so the audio loop keeps feeding the barge-in
        # detector and can interrupt.
        self.session.set_speaking(True)
        self._speaking_thread = threading.Thread(
            target=self._speak_then_listen, args=(answer,), daemon=True
        )
        self._speaking_thread.start()

    def _speak_then_listen(self, answer: str) -> None:
        try:
            self.speaker.say(answer)
        finally:
            self.session.set_speaking(False)
            self._emit("listening")

    # -- main loop ---------------------------------------------------------

    def run(self, source) -> None:
        wake_note = (
            f"waiting for {' / '.join(WAKE_WORDS)}"
            if self.session.config.wake_word else "always listening"
        )
        print(f"[daemon] ready — {wake_note}; speech via {self.speaker.backend}", flush=True)
        self._emit("ready", text=wake_note)

        for chunk in source:
            for event in self.session.feed(chunk):
                kind = event["type"]
                if kind == "wake":
                    self._emit("wake", phrase=event["phrase"])
                elif kind == "sleep":
                    self._emit("sleep")
                elif kind == "barge_in":
                    self._emit("barge_in", phrase=event["phrase"])
                    self.speaker.stop()
                elif kind == "final":
                    self.handle_utterance(event.get("text", ""))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Always-on voice assistant daemon")
    parser.add_argument("--device", default=None, help="Input device index or name")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--wav", default=None, help="Replay a WAV instead of the microphone")
    parser.add_argument("--fast", action="store_true", help="With --wav, do not pace in real time")
    parser.add_argument("--engine", default=DAEMON_ENGINE, choices=("whisper", "vosk", "hybrid"))
    parser.add_argument("--no-speak", action="store_true", help="Transcribe but stay silent")
    parser.add_argument("--no-wake", action="store_true", help="Skip the wake word; always listening")
    parser.add_argument("--voice", default="", help="Substring of the TTS voice name to use")
    parser.add_argument("--max-turns", type=int, default=0, help="Exit after N answers (testing)")
    args = parser.parse_args()

    if args.list_devices:
        import sounddevice as sd

        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"  {i:2d}  {d['name']}")
        return

    turns = {"n": 0}
    daemon = VoiceDaemon(
        engine=args.engine, speak=not args.no_speak, wake=not args.no_wake,
        voice_hint=args.voice,
    )

    if args.max_turns:
        original = daemon.handle_utterance

        def limited(text: str) -> None:
            original(text)
            turns["n"] += 1
            if turns["n"] >= args.max_turns:
                if daemon._speaking_thread:
                    daemon._speaking_thread.join(timeout=DAEMON_TIMEOUT)
                print(json.dumps({"turns": turns["n"]}))
                os._exit(0)

        daemon.handle_utterance = limited

    device = args.device
    if device is not None and str(device).isdigit():
        device = int(device)

    source = wav_stream(args.wav, realtime=not args.fast) if args.wav else mic_stream(device)
    try:
        daemon.run(source)
    except KeyboardInterrupt:
        print("\n[daemon] stopped.")


if __name__ == "__main__":
    main()
