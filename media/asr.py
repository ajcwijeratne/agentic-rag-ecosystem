"""
Unified ASR layer — Whisper, VOSK, and the hybrid of the two
=============================================================
One interface over both recognisers so callers pick an engine by name and
never touch engine-specific APIs.

  whisper — Faster-Whisper. Highest accuracy, punctuation, 90+ languages,
            auto language detection. Batch by nature: it needs a complete
            utterance before it produces anything.
  vosk    — Offline streaming. Emits partial hypotheses in tens of ms. Lower
            accuracy, no punctuation, one language per model.
  hybrid  — VOSK drives the live caption while the user is still speaking;
            the moment VAD closes the utterance, Whisper re-transcribes that
            same audio and its text replaces the VOSK draft.

VAD sits in front of all three. Silence never reaches an engine, which is what
makes streaming Whisper viable at all: instead of transcribing on a fixed
timer, each utterance is transcribed exactly once, when it ends.

  # batch
  result = transcribe_file("meeting.m4a", engine="hybrid")

  # live
  session = LiveSession(engine="hybrid")
  for event in session.feed(pcm_chunk):
      ...                                   # speech_start / partial / final
  for event in session.finish():
      ...
"""

from __future__ import annotations

import collections
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .audio import SAMPLE_RATE, SAMPLE_WIDTH, decode_to_pcm, duration_s, pcm_to_float32
from .vad import (
    SegmenterConfig,
    SpeechSegment,
    SpeechSegmenter,
    create_vad,
    speech_ratio,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ASR_ENGINE:      str = os.getenv("ASR_ENGINE", "whisper")       # whisper | vosk | hybrid
WHISPER_MODEL:   str = os.getenv("WHISPER_MODEL", "base")
WHISPER_DEVICE:  str = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE: str = os.getenv("WHISPER_COMPUTE", "int8")
WHISPER_BEAM:    int = int(os.getenv("WHISPER_BEAM_SIZE", "5"))
WHISPER_LANG:    str = os.getenv("WHISPER_LANGUAGE", "")        # "" = auto-detect

ENGINES = ("whisper", "vosk", "hybrid")

# Audio replayed after the wake word fires, covering detector lag only.
WAKE_PREROLL_S: float = float(os.getenv("WAKE_PREROLL_S", "0.6"))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class TranscriptSegment:
    text:    str
    start_s: float
    end_s:   float
    confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "text":       self.text,
            "start_s":    round(self.start_s, 3),
            "end_s":      round(self.end_s, 3),
            "confidence": round(self.confidence, 3),
        }


@dataclass
class Transcript:
    text:       str
    engine:     str
    segments:   list = field(default_factory=list)
    language:   str = ""
    duration_s: float = 0.0
    speech_s:   float = 0.0
    elapsed_s:  float = 0.0

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    @property
    def realtime_factor(self) -> float:
        """Audio seconds processed per wall-clock second. Above 1.0 is faster than real time."""
        return (self.duration_s / self.elapsed_s) if self.elapsed_s > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "text":       self.text,
            "engine":     self.engine,
            "language":   self.language,
            "duration_s": round(self.duration_s, 2),
            "speech_s":   round(self.speech_s, 2),
            "elapsed_s":  round(self.elapsed_s, 2),
            "realtime_factor": round(self.realtime_factor, 2),
            "word_count": self.word_count,
            "segments":   [s.to_dict() for s in self.segments],
        }


# ---------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------

_whisper_cache: dict = {}


def load_whisper(
    model_size: str = WHISPER_MODEL,
    device: str = WHISPER_DEVICE,
    compute: str = WHISPER_COMPUTE,
):
    """Load (and cache) a Faster-Whisper model. First call downloads weights."""
    from faster_whisper import WhisperModel  # type: ignore

    key = (model_size, device, compute)
    if key not in _whisper_cache:
        print(f"[whisper] Loading model {model_size!r} on {device} ({compute})...")
        _whisper_cache[key] = WhisperModel(model_size, device=device, compute_type=compute)
        print("[whisper] Model loaded.")
    return _whisper_cache[key]


def whisper_transcribe_pcm(
    pcm: bytes,
    language: str | None = None,
    model=None,
    beam_size: int = WHISPER_BEAM,
    word_timestamps: bool = False,
    offset_s: float = 0.0,
) -> tuple:
    """
    Transcribe canonical PCM with Whisper. Returns (segments, detected_language).

    The audio goes in as a float32 array, so no temp WAV is written — this is
    called once per VAD utterance during live streaming and the file I/O would
    dominate.
    """
    model = model or load_whisper()
    samples = pcm_to_float32(pcm)
    if samples.size == 0:
        return [], ""

    raw_segments, info = model.transcribe(
        samples,
        language=language or (WHISPER_LANG or None),
        beam_size=beam_size,
        word_timestamps=word_timestamps,
        # Our own VAD already removed the silence; a second pass here would
        # only re-trim audio that is by construction all speech.
        vad_filter=False,
    )

    out = [
        TranscriptSegment(
            text=seg.text.strip(),
            start_s=offset_s + seg.start,
            end_s=offset_s + seg.end,
            # avg_logprob is a log-probability; map it onto a rough 0..1 score
            # so confidences are comparable with VOSK's.
            confidence=_logprob_to_confidence(getattr(seg, "avg_logprob", None)),
        )
        for seg in raw_segments
        if seg.text.strip()
    ]
    return out, getattr(info, "language", "") or ""


def _logprob_to_confidence(avg_logprob) -> float:
    """Map Whisper's mean token log-probability onto a 0..1 confidence."""
    if avg_logprob is None:
        return 0.0
    import math

    return round(min(1.0, max(0.0, math.exp(float(avg_logprob)))), 3)


# ---------------------------------------------------------------------------
# Engine availability
# ---------------------------------------------------------------------------

def engine_status() -> dict:
    """What the machine can actually run right now — powers GET /voice/engines."""
    from . import vosk_engine
    from .vad import available_backends

    # find_spec, not import: engine_status runs on every /engines call and
    # importing faster_whisper (and torch behind it) costs tens of seconds cold.
    from importlib.util import find_spec

    try:
        whisper_ok = find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        whisper_ok = False

    vosk_state = vosk_engine.status()
    return {
        "whisper": {
            "available": whisper_ok,
            "model":     WHISPER_MODEL,
            "device":    WHISPER_DEVICE,
            "compute":   WHISPER_COMPUTE,
        },
        "vosk":   vosk_state,
        "hybrid": {"available": whisper_ok and vosk_state["available"]},
        "vad":    available_backends(),
        "default_engine": ASR_ENGINE,
    }


def resolve_engine(engine: str | None) -> str:
    """
    Validate an engine name and fall back when its dependencies are missing,
    so a request for `hybrid` on a machine with no VOSK model still returns a
    transcript instead of a 500.
    """
    engine = (engine or ASR_ENGINE).lower()
    if engine not in ENGINES:
        raise ValueError(f"Unknown ASR engine {engine!r} (expected one of {', '.join(ENGINES)})")

    from . import vosk_engine

    if engine in ("vosk", "hybrid") and not vosk_engine.is_available():
        print(f"[asr] {engine!r} requested but VOSK is unavailable — using whisper")
        return "whisper"
    return engine


# ---------------------------------------------------------------------------
# Batch transcription
# ---------------------------------------------------------------------------

def transcribe_pcm(
    pcm: bytes,
    engine: str | None = None,
    language: str | None = None,
    use_vad: bool = True,
    vad_backend: str | None = None,
    segmenter_config: SegmenterConfig | None = None,
) -> Transcript:
    """
    Transcribe a complete PCM buffer.

    With `use_vad` (the default) the buffer is first split into utterances and
    each is transcribed separately. Timestamps stay anchored to the original
    stream, and long silences cost nothing.
    """
    started = time.perf_counter()
    engine = resolve_engine(engine)
    total_s = duration_s(pcm)

    if use_vad:
        detector = create_vad(vad_backend or "auto")
        from .vad import segment_pcm

        segments = segment_pcm(pcm, detector, segmenter_config)
    else:
        segments = [SpeechSegment(pcm=pcm, start_s=0.0, end_s=total_s, index=0)]

    speech_s = sum(s.duration_s for s in segments)

    out_segments: list = []
    language_detected = ""

    if engine in ("whisper", "hybrid"):
        # Hybrid's VOSK half only matters live; for a finished buffer there is
        # no latency to hide, so the accurate engine does the whole job.
        model = load_whisper()
        for seg in segments:
            parts, lang = whisper_transcribe_pcm(
                seg.pcm, language=language, model=model, offset_s=seg.start_s
            )
            out_segments.extend(parts)
            language_detected = language_detected or lang
    else:
        from . import vosk_engine

        model = vosk_engine.load_model()
        for seg in segments:
            result = vosk_engine.transcribe_pcm(seg.pcm, model=model)
            if result.text:
                out_segments.append(
                    TranscriptSegment(
                        text=result.text,
                        start_s=seg.start_s,
                        end_s=seg.end_s,
                        confidence=result.confidence,
                    )
                )
        language_detected = os.getenv("VOSK_LANGUAGE", "en")

    return Transcript(
        text=" ".join(s.text for s in out_segments).strip(),
        engine=engine,
        segments=out_segments,
        language=language_detected,
        duration_s=total_s,
        speech_s=speech_s,
        elapsed_s=time.perf_counter() - started,
    )


def transcribe_file(
    path: str | Path,
    engine: str | None = None,
    language: str | None = None,
    use_vad: bool = True,
    vad_backend: str | None = None,
) -> Transcript:
    """Decode any audio or video file and transcribe it."""
    return transcribe_pcm(
        decode_to_pcm(path),
        engine=engine,
        language=language,
        use_vad=use_vad,
        vad_backend=vad_backend,
    )


def analyse_audio(pcm: bytes, vad_backend: str | None = None) -> dict:
    """
    VAD-only pass: where the speech is, without paying for transcription.
    Used by the /voice/vad endpoint and as a pre-flight check on uploads.
    """
    from .vad import segment_pcm

    detector = create_vad(vad_backend or "auto")
    segments = segment_pcm(pcm, detector)
    total = duration_s(pcm)
    speech = sum(s.duration_s for s in segments)
    return {
        "backend":       getattr(detector, "name", "unknown"),
        "duration_s":    round(total, 2),
        "speech_s":      round(speech, 2),
        "silence_s":     round(max(0.0, total - speech), 2),
        "speech_ratio":  round(speech / total, 3) if total else 0.0,
        "segment_count": len(segments),
        "segments":      [s.meta() for s in segments],
    }


# ---------------------------------------------------------------------------
# Media Asset Registry integration
# ---------------------------------------------------------------------------

def transcribe_segments(
    audio_path: str | Path,
    language: str | None = None,
    engine: str | None = None,
    use_vad: bool = True,
    vad_backend: str | None = None,
) -> dict:
    """
    Transcribe to the exact shape `media.registry.add_transcript` stores, so the
    ingestion worker can hand the result straight over.

    This is the VAD-gated, engine-selectable counterpart to
    `media.whisper_pipeline.transcribe_segments` and returns the same keys:
    status, language, duration, segments[{start,end,text,speaker}], text.

    `speaker` stays a diarization placeholder (None) until a diariser is added.
    """
    audio_path = Path(audio_path)
    if not audio_path.exists():
        return {"status": "error", "message": f"File not found: {audio_path}"}

    try:
        result = transcribe_file(
            audio_path,
            engine=engine,
            language=language,
            use_vad=use_vad,
            vad_backend=vad_backend,
        )
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        return {"status": "error", "message": str(exc)}

    return {
        "status":   "ok",
        "language": result.language,
        "duration": round(result.duration_s, 2),
        "segments": [
            {
                "start":   round(s.start_s, 2),
                "end":     round(s.end_s, 2),
                "text":    s.text,
                "speaker": None,
            }
            for s in result.segments
        ],
        "text":     result.text,
        # Extras the registry ignores, but callers and logs find useful.
        "engine":   result.engine,
        "speech_s": round(result.speech_s, 2),
    }


# ---------------------------------------------------------------------------
# Transcript persistence
# ---------------------------------------------------------------------------

def write_markdown(
    transcript: Transcript,
    source_name: str,
    output_dir: Path | str = os.getenv("TRANSCRIPT_OUTPUT_DIR", "./transcripts"),
) -> Path:
    """
    Write a transcript to Markdown in the shape the vault indexer expects, so a
    transcription becomes retrievable through RAG as soon as it lands.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(source_name).stem or "transcript"
    silence = max(0.0, transcript.duration_s - transcript.speech_s)

    lines = [
        f"# Transcript: {source_name}",
        "",
        f"**Engine:** {transcript.engine}",
        f"**Language:** {transcript.language or 'unknown'}",
        f"**Duration:** {transcript.duration_s:.1f}s "
        f"({transcript.speech_s:.1f}s speech, {silence:.1f}s silence removed)",
        f"**Processed in:** {transcript.elapsed_s:.1f}s "
        f"({transcript.realtime_factor:.1f}x realtime)",
        "",
        "---",
        "",
    ]
    for seg in transcript.segments:
        lines.append(f"**[{seg.start_s:07.2f}s -> {seg.end_s:07.2f}s]** {seg.text}")
        lines.append("")

    out_file = output_dir / f"{stem}.md"
    out_file.write_text("\n".join(lines), encoding="utf-8")
    return out_file


# ---------------------------------------------------------------------------
# Live streaming session
# ---------------------------------------------------------------------------

@dataclass
class LiveConfig:
    engine:        str = ASR_ENGINE
    language:      str | None = None
    vad_backend:   str = "auto"
    emit_partials: bool = True
    segmenter:     SegmenterConfig = field(default_factory=SegmenterConfig)
    # Wake word. With this on the session starts asleep: audio is listened to
    # continuously but only the cheap grammar-mode detector runs, so nothing
    # reaches Whisper or a model until someone says the phrase.
    wake_word:     bool = False
    wake_phrases:  list = field(default_factory=list)
    wake_timeout_s: float = 12.0


class LiveSession:
    """
    Real-time transcription over a PCM stream.

    Feed it audio as it arrives; it yields event dicts:

      {"type": "speech_start", "at_s": 1.83}
      {"type": "partial",  "text": "what is agentic",       "engine": "vosk"}
      {"type": "final",    "text": "What is agentic RAG?",  "engine": "whisper",
       "start_s": 1.83, "end_s": 4.20, "confidence": 0.91}
      {"type": "speech_end", "at_s": 4.20}

    Partials only ever come from VOSK — Whisper cannot produce them. In
    `whisper` mode you get finals only, one per utterance, which is the right
    trade when accuracy matters more than live feedback.

    This class is synchronous and CPU-bound; the websocket service runs `feed`
    in a worker thread so the event loop keeps accepting audio.
    """

    def __init__(self, config: LiveConfig | None = None, **kwargs):
        self.config = config or LiveConfig(**kwargs)
        self.config.engine = resolve_engine(self.config.engine)

        self.segmenter = SpeechSegmenter(
            create_vad(self.config.vad_backend), self.config.segmenter
        )
        self._was_speaking = False
        self._vosk_stream = None
        self._whisper = None
        self.transcript: list = []
        self.started_at = time.time()

        # Barge-in. While the assistant is speaking the client keeps sending
        # audio, but only a grammar-constrained detector listens, matching just
        # the interrupt words. The assistant's own voice cannot trigger it
        # unless it happens to say "stop" or "wait" in isolation, which the
        # grammar makes vanishingly unlikely — whereas full ASR would transcribe
        # its own reply and act on it.
        self.assistant_speaking = False
        self._barge = None

        # Wake state. `asleep` means only the wake detector is fed.
        self._wake = None
        self.asleep = False
        self._last_voice_at = time.monotonic()
        if self.config.wake_word:
            from .wake import WAKE_WORDS, WakeWordDetector

            phrases = self.config.wake_phrases or WAKE_WORDS
            self._wake = WakeWordDetector(phrases)
            if self._wake.available:
                self.asleep = True
                # Hold a little recent audio while asleep, so a question asked
                # in the same breath as the wake word ("hey jarvis, what is X")
                # is not lost in the gap between the detector firing and
                # transcription starting.
                #
                # Kept deliberately short. VOSK reports the phrase once it has
                # ended, and the question follows it, so only the detector's own
                # lag needs covering. Buffering longer drags in whatever was said
                # before the wake word — the tail of an unrelated conversation
                # gets transcribed and answered as though it were the question.
                self._preroll: collections.deque = collections.deque()
                self._preroll_bytes = 0
                self._preroll_limit = int(SAMPLE_RATE * SAMPLE_WIDTH * WAKE_PREROLL_S)
            else:
                print("[asr] wake word requested but unavailable — staying awake")
                self.config.wake_word = False

        if self.config.engine in ("vosk", "hybrid"):
            from . import vosk_engine

            self._vosk_stream = vosk_engine.VoskStream()
        if self.config.engine in ("whisper", "hybrid"):
            self._whisper = load_whisper()

    # -- properties ---------------------------------------------------------

    @property
    def full_text(self) -> str:
        """Everything finalised so far — what gets sent to the orchestrator."""
        return " ".join(s.text for s in self.transcript).strip()

    @property
    def speaking(self) -> bool:
        return self.segmenter.speaking

    # -- streaming ----------------------------------------------------------

    def feed(self, pcm_chunk: bytes) -> list:
        """Push a chunk of canonical PCM; return the events it produced."""
        events: list = []

        # ---- assistant is talking: listen only for an interrupt ----------
        if self.assistant_speaking:
            if self._barge is not None:
                hit = self._barge.feed(pcm_chunk)
                if hit:
                    events.append({"type": "barge_in", "phrase": hit})
                    self.set_speaking(False)
            return events

        # ---- asleep: only the wake detector runs -------------------------
        if self.asleep:
            # Byte-bounded, because chunk sizes vary by client.
            self._preroll.append(pcm_chunk)
            self._preroll_bytes += len(pcm_chunk)
            while self._preroll_bytes > self._preroll_limit and len(self._preroll) > 1:
                self._preroll_bytes -= len(self._preroll.popleft())

            hit = self._wake.feed(pcm_chunk)
            if hit is None:
                return []
            self.asleep = False
            self._last_voice_at = time.monotonic()
            events.append({"type": "wake", "phrase": hit})
            # Replay the buffered audio so the question that followed the wake
            # word is transcribed rather than dropped.
            buffered = b"".join(self._preroll)
            self._preroll.clear()
            self._preroll_bytes = 0
            events.extend(self._feed_awake(buffered))
            return events

        events.extend(self._feed_awake(pcm_chunk))

        # ---- awake but idle: go back to sleep ----------------------------
        if self.config.wake_word and not self.segmenter.speaking:
            idle = time.monotonic() - self._last_voice_at
            if idle >= self.config.wake_timeout_s:
                events.append({"type": "sleep", "after_idle_s": round(idle, 1)})
                self.sleep()
        return events

    def set_speaking(self, speaking: bool) -> None:
        """
        Tell the session the assistant has started or stopped talking.

        Note the distinction from the read-only `speaking` property, which means
        the *user* is mid-utterance according to VAD. This one is the assistant.

        While speaking, incoming audio is routed only to the barge-in detector,
        so the reply is never transcribed back as user speech. On stopping, the
        recognisers are reset so nothing captured during playback leaks into the
        next utterance.
        """
        speaking = bool(speaking)
        if speaking == self.assistant_speaking:
            return
        self.assistant_speaking = speaking

        if speaking:
            if self._barge is None:
                from .wake import BARGE_WORDS, WakeWordDetector

                self._barge = WakeWordDetector(BARGE_WORDS)
                if not self._barge.available:
                    self._barge = None      # no VOSK: fall back to a muted mic
            if self._barge is not None:
                self._barge.reset()
        else:
            self.segmenter.reset()
            if self._vosk_stream is not None:
                self._vosk_stream.reset()
            if self._wake is not None:
                self._wake.reset()
            self._was_speaking = False
            self._last_voice_at = time.monotonic()

    @property
    def barge_in_available(self) -> bool:
        """Whether interrupting by voice will work, or the mic must be muted."""
        if self._barge is not None:
            return True
        try:
            from . import vosk_engine

            return vosk_engine.is_available()
        except Exception:
            return False

    def sleep(self) -> None:
        """Return to waiting for the wake word."""
        if not self.config.wake_word or self._wake is None:
            return
        self.segmenter.reset()
        if self._vosk_stream is not None:
            self._vosk_stream.reset()
        self._wake.reset()
        self._preroll.clear()
        self._preroll_bytes = 0
        self._was_speaking = False
        self.asleep = True

    def _feed_awake(self, pcm_chunk: bytes) -> list:
        """The normal transcription path, once the session is awake."""
        events: list = []

        # VOSK runs on the raw stream, not on VAD segments: it has its own
        # endpointing and needs continuous audio to keep partials coherent.
        if self._vosk_stream is not None and self.config.emit_partials:
            for result in self._vosk_stream.accept(pcm_chunk):
                if result.partial:
                    events.append(
                        {"type": "partial", "text": result.text, "engine": "vosk"}
                    )
                elif self.config.engine == "vosk" and result.text:
                    # Pure VOSK mode: its own final is the final. Its word
                    # timings are relative to the start of the stream, which is
                    # the same clock the segmenter uses, so they carry over.
                    start, end = None, None
                    if result.words:
                        start, end = result.words[0].start, result.words[-1].end
                    events.append(
                        self._record(result.text, result.confidence, "vosk", start, end)
                    )

        completed = self.segmenter.feed(pcm_chunk)

        if self.segmenter.speaking:
            self._last_voice_at = time.monotonic()
        if self.segmenter.speaking and not self._was_speaking:
            events.insert(0, {"type": "speech_start", "at_s": round(self.segmenter.state.stream_pos_s, 3)})
        self._was_speaking = self.segmenter.speaking

        for segment in completed:
            events.extend(self._on_segment(segment))

        return events

    def finish(self) -> list:
        """
        Close the stream: transcribe whatever utterance is still open and emit
        a final `transcript` event with the whole session text.
        """
        events: list = []
        for segment in self.segmenter.flush():
            events.extend(self._on_segment(segment))

        if self._vosk_stream is not None:
            tail = self._vosk_stream.finish()
            if self.config.engine == "vosk" and tail.text:
                start, end = None, None
                if tail.words:
                    start, end = tail.words[0].start, tail.words[-1].end
                events.append(self._record(tail.text, tail.confidence, "vosk", start, end))

        if self._was_speaking:
            events.append({"type": "speech_end", "at_s": round(self.segmenter.state.stream_pos_s, 3)})
            self._was_speaking = False

        events.append(
            {
                "type":       "transcript",
                "text":       self.full_text,
                "engine":     self.config.engine,
                "segments":   [s.to_dict() for s in self.transcript],
                "duration_s": round(self.segmenter.state.stream_pos_s, 2),
            }
        )
        return events

    def reset(self) -> None:
        """Clear the session for a new recording, keeping the loaded models."""
        self.segmenter.reset()
        self.transcript = []
        self._was_speaking = False
        if self._vosk_stream is not None:
            self._vosk_stream.reset()

    # -- internals ----------------------------------------------------------

    def _on_segment(self, segment: SpeechSegment) -> list:
        """A VAD utterance closed — run the accurate engine over it."""
        events: list = [{"type": "speech_end", "at_s": round(segment.end_s, 3)}]

        if self.config.engine == "vosk":
            # VOSK already emitted its own final; the VAD boundary is only a
            # UI signal here.
            return events

        parts, _lang = whisper_transcribe_pcm(
            segment.pcm,
            language=self.config.language,
            model=self._whisper,
            offset_s=segment.start_s,
        )
        text = " ".join(p.text for p in parts).strip()
        if not text:
            return events

        confidence = (
            sum(p.confidence for p in parts) / len(parts) if parts else 0.0
        )
        events.append(
            self._record(text, confidence, "whisper", segment.start_s, segment.end_s)
        )
        return events

    def _record(
        self,
        text: str,
        confidence: float,
        engine: str,
        start_s: float | None = None,
        end_s: float | None = None,
    ) -> dict:
        """Append to the session transcript and build the `final` event."""
        start = start_s if start_s is not None else self.segmenter.state.stream_pos_s
        end = end_s if end_s is not None else self.segmenter.state.stream_pos_s
        self.transcript.append(
            TranscriptSegment(text=text, start_s=start, end_s=end, confidence=confidence)
        )
        return {
            "type":       "final",
            "text":       text,
            "engine":     engine,
            "start_s":    round(start, 3),
            "end_s":      round(end, 3),
            "confidence": round(confidence, 3),
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Unified ASR — Whisper / VOSK / hybrid")
    parser.add_argument("--file", help="Audio or video file to transcribe")
    parser.add_argument("--engine", default=ASR_ENGINE, choices=list(ENGINES))
    parser.add_argument("--lang", default=None, help="Force language (e.g. 'en')")
    parser.add_argument("--no-vad", action="store_true", help="Transcribe without VAD gating")
    parser.add_argument("--vad-backend", default="auto", help="silero | webrtc | energy | auto")
    parser.add_argument("--vad-only", action="store_true", help="Report speech segments only")
    parser.add_argument("--status", action="store_true", help="Show engine availability")
    args = parser.parse_args()

    if args.status:
        print(json.dumps(engine_status(), indent=2))
    elif args.vad_only and args.file:
        print(json.dumps(analyse_audio(decode_to_pcm(args.file), args.vad_backend), indent=2))
    elif args.file:
        result = transcribe_file(
            args.file,
            engine=args.engine,
            language=args.lang,
            use_vad=not args.no_vad,
            vad_backend=args.vad_backend,
        )
        print(json.dumps(result.to_dict(), indent=2))
    else:
        parser.print_help()
