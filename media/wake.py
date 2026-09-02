"""
Wake-word detection
===================
The always-on half of a voice assistant: audio is listened to continuously, but
nothing reaches a model until someone actually addresses it.

Built on VOSK in grammar mode. A constrained grammar restricts the recogniser to
a handful of phrases plus `[unk]`, which makes it both far more accurate on
those phrases and cheap enough to run permanently — the small English model
decodes faster than real time on one CPU core. Nothing leaves the machine, and
no paid call happens until the wake phrase lands.

Why not Whisper: it has no streaming mode, so wake detection would mean
transcribing every few seconds of room noise in full. Grammar-mode VOSK only has
to decide between a few phrases and "something else".

    detector = WakeWordDetector(["hey jarvis", "jarvis"])
    if detector.feed(pcm_chunk):
        ...   # addressed

Set WAKE_WORDS to change the phrases. Keep them to two or three syllables and
avoid common words: "jarvis" is a good wake word precisely because it almost
never occurs in ordinary speech, whereas "computer" would fire constantly.
"""

from __future__ import annotations

import os
import re
import time

from .audio import SAMPLE_RATE

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WAKE_WORDS: list = [
    w.strip().lower()
    for w in os.getenv("WAKE_WORDS", "hey jarvis,jarvis,okay jarvis").split(",")
    if w.strip()
]
WAKE_ENABLED: bool = os.getenv("WAKE_ENABLED", "false").lower() in ("1", "true", "yes")
# How long the assistant stays awake with no speech before it needs the wake
# phrase again.
WAKE_TIMEOUT_S: float = float(os.getenv("WAKE_TIMEOUT_S", "12"))
# Phrases that interrupt a spoken reply. Deliberately short and unlike anything
# the assistant says about itself, so its own voice cannot trigger them.
BARGE_WORDS: list = [
    w.strip().lower()
    for w in os.getenv("BARGE_WORDS", "stop,wait,cancel,shut up,quiet").split(",")
    if w.strip()
]


def _normalise(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())).strip()


def phrase_in(text: str, phrases: list) -> str | None:
    """
    Return the phrase found in `text`, or None.

    Substring rather than equality: VOSK often returns the wake word with
    whatever was said after it in the same result ("hey jarvis what is"), and an
    exact match would miss every one of those.
    """
    clean = _normalise(text)
    if not clean:
        return None
    for phrase in phrases:
        p = _normalise(phrase)
        if p and re.search(rf"(?:^|\s){re.escape(p)}(?:\s|$)", clean):
            return p
    return None


def strip_wake_prefix(text: str, phrases: list) -> str:
    """
    Remove the wake phrase from the front of an utterance.

    "hey jarvis what is agentic rag" -> "what is agentic rag", so the wake word
    is never carried into the question itself.
    """
    clean = _normalise(text)
    for phrase in sorted((_normalise(p) for p in phrases), key=len, reverse=True):
        if phrase and clean.startswith(phrase):
            return clean[len(phrase):].strip(" ,.")
    return text.strip()


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------

class WakeWordDetector:
    """
    Streaming wake-word detector.

    Feed it audio continuously; `feed` returns the matched phrase the moment one
    is heard, otherwise None. The recogniser is reset after every hit so the
    phrase cannot be reported twice from the same tail of audio.

    Falls back to `available == False` when VOSK or its model is missing, so a
    caller can degrade to push-to-talk instead of failing.
    """

    def __init__(
        self,
        phrases: list | None = None,
        sample_rate: int = SAMPLE_RATE,
        model=None,
    ):
        self.phrases = [p.lower() for p in (phrases or WAKE_WORDS)]
        self.sample_rate = sample_rate
        self.available = False
        self._stream = None
        self._last_hit = 0.0

        if not self.phrases:
            return

        try:
            from . import vosk_engine

            if not vosk_engine.is_available():
                return
            # Grammar mode: the decoder may only produce these phrases or
            # "[unk]", which is what makes it cheap and accurate.
            self._stream = vosk_engine.VoskStream(
                model=model or vosk_engine.load_model(),
                sample_rate=sample_rate,
                words=False,
                grammar=list(self.phrases),
            )
            self.available = True
        except Exception as exc:  # noqa: BLE001 — caller degrades to push-to-talk
            print(f"[wake] detector unavailable: {exc}")

    def feed(self, pcm: bytes, debounce_s: float = 1.5) -> str | None:
        """
        Push audio. Returns the matched wake phrase, or None.

        `debounce_s` suppresses a second hit immediately after the first, which
        otherwise happens when the phrase straddles two decoder results.
        """
        if not self.available or not pcm:
            return None

        for result in self._stream.accept(pcm):
            hit = phrase_in(result.text, self.phrases)
            if not hit:
                continue
            now = time.monotonic()
            if now - self._last_hit < debounce_s:
                continue
            self._last_hit = now
            self._stream.reset()
            return hit
        return None

    def reset(self) -> None:
        if self._stream is not None:
            self._stream.reset()


def detect_in_text(text: str, phrases: list | None = None) -> str | None:
    """
    Wake-word match against already-transcribed text.

    Used when a full recogniser is already running, so a second decoder for the
    wake word would be wasted work.
    """
    return phrase_in(text, phrases or WAKE_WORDS)


def is_barge_in(text: str) -> str | None:
    """Did the speaker ask the assistant to stop talking?"""
    return phrase_in(text, BARGE_WORDS)
