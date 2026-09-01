"""
Voice Activity Detection
=========================
Decides which parts of an audio stream contain speech, so the ASR engines only
ever see audio worth transcribing. This cuts Whisper cost and latency (silence
is never sent to the model), stops VOSK emitting phantom partials during room
noise, and gives the live UI a clean "user started / stopped speaking" signal.

Three interchangeable backends, in quality order:

  silero  — Silero VAD, a small neural model. Best accuracy in noise. Runs on
            the torch install the project already has.
  webrtc  — The WebRTC VAD via `webrtcvad`. Microseconds per frame, good in
            clean audio, aggressive-mode tunable.
  energy  — Adaptive RMS gate with a rolling noise floor. Pure numpy, no extra
            dependency, always available so the pipeline never hard-fails.

`create_vad("auto")` picks the best backend actually installed.

Two ways to use it:

  detector = create_vad()                      # frame-by-frame decisions
  detector.is_speech(frame_pcm)

  segmenter = SpeechSegmenter(detector)        # streaming utterance detection
  for seg in segmenter.feed(pcm_chunk): ...    # emits complete utterances
  for seg in segmenter.flush(): ...            # emits whatever is still open
"""

from __future__ import annotations

import collections
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from .audio import (
    SAMPLE_RATE,
    Frame,
    duration_s,
    iter_frames,
    pcm_to_float32,
    rms_dbfs,
)

# ---------------------------------------------------------------------------
# Configuration defaults (override per-call or via .env)
# ---------------------------------------------------------------------------

VAD_BACKEND:          str   = os.getenv("VAD_BACKEND", "auto")           # auto|silero|webrtc|energy
VAD_AGGRESSIVENESS:   int   = int(os.getenv("VAD_AGGRESSIVENESS", "2"))  # webrtc: 0..3
VAD_THRESHOLD:        float = float(os.getenv("VAD_THRESHOLD", "0.5"))   # silero: speech probability
VAD_SILENCE_MS:       int   = int(os.getenv("VAD_SILENCE_MS", "600"))    # trailing silence closes an utterance
VAD_MIN_SPEECH_MS:    int   = int(os.getenv("VAD_MIN_SPEECH_MS", "250")) # shorter bursts are treated as noise
VAD_MAX_SPEECH_MS:    int   = int(os.getenv("VAD_MAX_SPEECH_MS", "30000"))
VAD_PADDING_MS:       int   = int(os.getenv("VAD_PADDING_MS", "300"))    # pre-roll kept before speech onset


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------

class VADBackend(Protocol):
    name: str
    preferred_frame_ms: int

    def speech_probability(self, frame_pcm: bytes) -> float: ...
    def is_speech(self, frame_pcm: bytes) -> bool: ...
    def reset(self) -> None: ...


# ---------------------------------------------------------------------------
# Silero
# ---------------------------------------------------------------------------

class SileroVAD:
    """
    Neural VAD. The model is strict about window size: exactly 512 samples at
    16 kHz. Frames of any length are buffered internally and the probability
    reported is the highest seen across the complete windows in that frame, so
    a brief onset inside a longer frame is not averaged away.
    """

    name = "silero"
    preferred_frame_ms = 32          # 512 samples @ 16 kHz

    WINDOW_SAMPLES = 512

    def __init__(self, threshold: float = VAD_THRESHOLD, sample_rate: int = SAMPLE_RATE):
        if sample_rate != SAMPLE_RATE:
            raise ValueError("Silero VAD is used here at 16 kHz only")
        self.threshold = threshold
        self.sample_rate = sample_rate
        self._buffer = np.zeros(0, dtype=np.float32)
        self._last_prob = 0.0
        self._model, self._torch = _load_silero_model()

    def speech_probability(self, frame_pcm: bytes) -> float:
        self._buffer = np.concatenate([self._buffer, pcm_to_float32(frame_pcm)])
        best = None
        while self._buffer.size >= self.WINDOW_SAMPLES:
            window = self._buffer[: self.WINDOW_SAMPLES]
            self._buffer = self._buffer[self.WINDOW_SAMPLES :]
            tensor = self._torch.from_numpy(window.copy())
            with self._torch.no_grad():
                prob = float(self._model(tensor, self.sample_rate).item())
            best = prob if best is None else max(best, prob)
        if best is not None:
            self._last_prob = best
        # No complete window yet: hold the previous decision rather than
        # flipping to silence on a short frame.
        return self._last_prob

    def is_speech(self, frame_pcm: bytes) -> bool:
        return self.speech_probability(frame_pcm) >= self.threshold

    def reset(self) -> None:
        self._buffer = np.zeros(0, dtype=np.float32)
        self._last_prob = 0.0
        reset_states = getattr(self._model, "reset_states", None)
        if callable(reset_states):
            reset_states()


_silero_cache: tuple = ()


def _load_silero_model():
    """Load Silero once per process. Tries the pip package, then torch.hub."""
    global _silero_cache
    if _silero_cache:
        return _silero_cache

    import torch  # raises ImportError -> caller falls back to another backend

    try:
        from silero_vad import load_silero_vad  # type: ignore

        model = load_silero_vad()
    except Exception:
        # torch.hub needs network on first call, then uses its local cache.
        model, _utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad",
            model="silero_vad",
            force_reload=False,
            onnx=False,
            trust_repo=True,
        )

    model.eval()
    torch.set_num_threads(int(os.getenv("VAD_TORCH_THREADS", "1")))
    _silero_cache = (model, torch)
    return _silero_cache


# ---------------------------------------------------------------------------
# WebRTC
# ---------------------------------------------------------------------------

class WebRtcVAD:
    """
    The WebRTC GMM-based detector. Only accepts 10, 20 or 30 ms frames of
    16-bit mono PCM at 8/16/32/48 kHz — frames of any other length are
    rejected, so an ill-sized frame is reported as silence rather than raising
    mid-stream.
    """

    name = "webrtc"
    preferred_frame_ms = 30

    def __init__(self, aggressiveness: int = VAD_AGGRESSIVENESS, sample_rate: int = SAMPLE_RATE):
        import webrtcvad  # type: ignore

        self.sample_rate = sample_rate
        self.aggressiveness = max(0, min(3, aggressiveness))
        self._webrtcvad = webrtcvad
        self._vad = webrtcvad.Vad(self.aggressiveness)
        self._valid_sizes = {int(sample_rate * ms / 1000) * 2 for ms in (10, 20, 30)}

    def speech_probability(self, frame_pcm: bytes) -> float:
        return 1.0 if self.is_speech(frame_pcm) else 0.0

    def is_speech(self, frame_pcm: bytes) -> bool:
        if len(frame_pcm) not in self._valid_sizes:
            return False
        return bool(self._vad.is_speech(frame_pcm, self.sample_rate))

    def reset(self) -> None:
        self._vad = self._webrtcvad.Vad(self.aggressiveness)


# ---------------------------------------------------------------------------
# Energy (always available)
# ---------------------------------------------------------------------------

class EnergyVAD:
    """
    Adaptive RMS gate. The noise floor is estimated from the quietest recent
    frames and tracked slowly, so the detector adjusts to a new room or mic
    without recalibration. A frame counts as speech when it sits `margin_db`
    above that floor and clears an absolute silence gate.
    """

    name = "energy"
    preferred_frame_ms = 30

    def __init__(
        self,
        margin_db: float = float(os.getenv("VAD_ENERGY_MARGIN_DB", "8.0")),
        floor_db:  float = float(os.getenv("VAD_ENERGY_FLOOR_DB", "-55.0")),
        adapt:     float = 0.05,
        history:   int = 100,
    ):
        self.margin_db = margin_db
        self.floor_db = floor_db
        self.adapt = adapt
        self._history: collections.deque = collections.deque(maxlen=history)
        self._noise_db = floor_db

    def speech_probability(self, frame_pcm: bytes) -> float:
        level = rms_dbfs(frame_pcm)
        self._history.append(level)

        # Re-estimate the floor from the quieter half of recent history so a
        # long stretch of speech cannot drag the floor up with it.
        if len(self._history) >= 10:
            quiet = sorted(self._history)[: max(1, len(self._history) // 2)]
            target = float(np.mean(quiet))
            self._noise_db = (1 - self.adapt) * self._noise_db + self.adapt * target

        threshold = max(self._noise_db + self.margin_db, self.floor_db)
        if level <= threshold:
            return 0.0
        # Map the headroom over threshold onto 0.5..1.0 so callers comparing
        # against a probability threshold behave sensibly.
        return float(min(1.0, 0.5 + (level - threshold) / (2 * self.margin_db)))

    def is_speech(self, frame_pcm: bytes) -> bool:
        return self.speech_probability(frame_pcm) >= 0.5

    def reset(self) -> None:
        self._history.clear()
        self._noise_db = self.floor_db


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def available_backends() -> dict:
    """
    Which VAD backends can be constructed on this machine.

    Probes with find_spec rather than importing: importing torch costs tens of
    seconds cold, and this runs on every /engines call, which the Command Centre
    hits on mount. Locating the module is enough to know it is installed.
    """
    from importlib.util import find_spec

    def installed(name: str) -> bool:
        try:
            return find_spec(name) is not None
        except (ImportError, ValueError):
            return False

    return {
        "energy": True,                     # pure numpy, always available
        "webrtc": installed("webrtcvad"),
        "silero": installed("torch"),
    }


def create_vad(backend: str = VAD_BACKEND, **kwargs) -> VADBackend:
    """
    Build a VAD. `backend="auto"` walks silero -> webrtc -> energy and returns
    the first that loads, so a missing optional dependency degrades quality
    instead of breaking the service.
    """
    backend = (backend or "auto").lower()

    if backend == "auto":
        for candidate in ("silero", "webrtc", "energy"):
            try:
                return create_vad(candidate, **kwargs)
            except Exception as exc:  # noqa: BLE001 — try the next backend
                print(f"[vad] backend {candidate!r} unavailable: {exc}")
        return EnergyVAD()

    if backend == "silero":
        return SileroVAD(threshold=kwargs.get("threshold", VAD_THRESHOLD))
    if backend == "webrtc":
        return WebRtcVAD(aggressiveness=kwargs.get("aggressiveness", VAD_AGGRESSIVENESS))
    if backend == "energy":
        return EnergyVAD()

    raise ValueError(f"Unknown VAD backend: {backend!r} (silero | webrtc | energy | auto)")


# ---------------------------------------------------------------------------
# Streaming segmenter
# ---------------------------------------------------------------------------

@dataclass
class SpeechSegment:
    """One detected utterance: contiguous speech bounded by silence."""

    pcm:       bytes
    start_s:   float
    end_s:     float
    index:     int
    truncated: bool = False        # closed by max-duration, not by silence

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def meta(self) -> dict:
        return {
            "index":      self.index,
            "start_s":    round(self.start_s, 3),
            "end_s":      round(self.end_s, 3),
            "duration_s": round(self.duration_s, 3),
            "truncated":  self.truncated,
        }


@dataclass
class SegmenterConfig:
    frame_ms:      int   = 0        # 0 = use the backend's preferred size
    silence_ms:    int   = VAD_SILENCE_MS
    min_speech_ms: int   = VAD_MIN_SPEECH_MS
    max_speech_ms: int   = VAD_MAX_SPEECH_MS
    padding_ms:    int   = VAD_PADDING_MS
    trigger_ratio: float = 0.6      # fraction of the pre-roll that must be speech to open
    sample_rate:   int   = SAMPLE_RATE


@dataclass
class SegmenterState:
    triggered:    bool  = False
    stream_pos_s: float = 0.0
    segments_out: int   = 0
    voiced:       list  = field(default_factory=list)


class SpeechSegmenter:
    """
    Turns a stream of PCM chunks into complete utterances.

    The state machine keeps a ring buffer of the last `padding_ms` of frames.
    While idle it opens a segment once `trigger_ratio` of that buffer is voiced,
    and emits the buffer with it — so the first syllable is never clipped.
    While open it closes on `silence_ms` of trailing silence, discarding
    segments shorter than `min_speech_ms` as noise, and force-closes at
    `max_speech_ms` so one continuous talker cannot grow an unbounded buffer.

    Chunks may be any size; internal framing is handled by the segmenter.
    """

    def __init__(self, vad: VADBackend | None = None, config: SegmenterConfig | None = None):
        self.vad = vad or create_vad()
        self.config = config or SegmenterConfig()
        self.frame_ms = self.config.frame_ms or getattr(self.vad, "preferred_frame_ms", 30)

        n_padding = max(1, self.config.padding_ms // self.frame_ms)
        self._ring: collections.deque = collections.deque(maxlen=n_padding)
        self._silence_frames_needed = max(1, self.config.silence_ms // self.frame_ms)
        self._silence_run = 0
        self._residue = b""
        self.state = SegmenterState()

    # -- introspection ------------------------------------------------------

    @property
    def speaking(self) -> bool:
        """True while an utterance is open — drives the live UI indicator."""
        return self.state.triggered

    # -- streaming ----------------------------------------------------------

    def feed(self, pcm_chunk: bytes) -> list:
        """Push audio in, get any utterances that completed within it."""
        buf = self._residue + pcm_chunk
        completed: list = []
        consumed = 0

        for frame in iter_frames(buf, self.frame_ms, self.config.sample_rate):
            consumed += len(frame.pcm)
            # Re-stamp against the absolute stream position; iter_frames times
            # each chunk from zero.
            absolute = Frame(
                pcm=frame.pcm,
                index=frame.index,
                start_s=self.state.stream_pos_s,
                end_s=self.state.stream_pos_s + self.frame_ms / 1000.0,
            )
            self.state.stream_pos_s = absolute.end_s
            seg = self._push_frame(absolute)
            if seg is not None:
                completed.append(seg)

        self._residue = buf[consumed:]
        return completed

    def flush(self) -> list:
        """
        End of stream: emit the open utterance if it is long enough. Call this
        when the websocket closes or the file ends, or the last thing the user
        said is lost.
        """
        out: list = []
        if self.state.triggered:
            seg = self._close_segment(truncated=False, trailing=self._silence_run)
            if seg is not None:
                out.append(seg)
        self._residue = b""
        return out

    def reset(self) -> None:
        self.vad.reset()
        self._ring.clear()
        self._silence_run = 0
        self._residue = b""
        self.state = SegmenterState()

    # -- internals ----------------------------------------------------------

    def _push_frame(self, frame: Frame):
        voiced = self.vad.is_speech(frame.pcm)

        if not self.state.triggered:
            self._ring.append((frame, voiced))
            n_voiced = sum(1 for _, v in self._ring if v)
            if n_voiced >= self.config.trigger_ratio * self._ring.maxlen:
                self.state.triggered = True
                self._silence_run = 0
                # Carry the pre-roll into the segment so the onset is intact.
                self.state.voiced = [f for f, _ in self._ring]
                self._ring.clear()
            return None

        self.state.voiced.append(frame)
        self._silence_run = 0 if voiced else self._silence_run + 1

        speech_ms = len(self.state.voiced) * self.frame_ms
        if speech_ms >= self.config.max_speech_ms:
            return self._close_segment(truncated=True, trailing=self._silence_run)
        if self._silence_run >= self._silence_frames_needed:
            return self._close_segment(truncated=False, trailing=self._silence_run)
        return None

    def _close_segment(self, truncated: bool, trailing: int = 0):
        frames = self.state.voiced
        self.state.voiced = []
        self.state.triggered = False
        self._silence_run = 0
        self._ring.clear()
        self.vad.reset()

        if not frames:
            return None

        # Everything after the last voiced frame is the silence that closed the
        # segment. Keep `padding_ms` of it as a natural tail and drop the rest,
        # so end timestamps track the speech and Whisper is not handed dead air.
        keep_tail = max(1, self.config.padding_ms // self.frame_ms)
        drop = max(0, trailing - keep_tail)
        if drop:
            frames = frames[: max(1, len(frames) - drop)]

        pcm = b"".join(f.pcm for f in frames)
        if duration_s(pcm, self.config.sample_rate) * 1000 < self.config.min_speech_ms:
            return None      # too short to be speech — drop it

        seg = SpeechSegment(
            pcm=pcm,
            start_s=frames[0].start_s,
            end_s=frames[-1].end_s,
            index=self.state.segments_out,
            truncated=truncated,
        )
        self.state.segments_out += 1
        return seg


# ---------------------------------------------------------------------------
# Offline helpers
# ---------------------------------------------------------------------------

def segment_pcm(
    pcm: bytes,
    vad: VADBackend | None = None,
    config: SegmenterConfig | None = None,
) -> list:
    """Run the segmenter over a complete buffer and return every utterance."""
    seg = SpeechSegmenter(vad, config)
    out = seg.feed(pcm)
    out.extend(seg.flush())
    return out


def segment_file(
    path: Path | str,
    backend: str = VAD_BACKEND,
    config: SegmenterConfig | None = None,
) -> list:
    """Decode a file to canonical PCM and segment it."""
    from .audio import decode_to_pcm

    return segment_pcm(decode_to_pcm(path), create_vad(backend), config)


def speech_ratio(pcm: bytes, vad: VADBackend | None = None, frame_ms: int = 30) -> float:
    """
    Fraction of a buffer that is speech. Useful as a cheap pre-flight: a
    recording under a few percent is almost certainly a dead mic, and there is
    no point paying for a Whisper pass on it.
    """
    detector = vad or create_vad()
    total = voiced = 0
    for frame in iter_frames(pcm, frame_ms):
        total += 1
        if detector.is_speech(frame.pcm):
            voiced += 1
    return (voiced / total) if total else 0.0


def strip_silence(
    pcm: bytes,
    vad: VADBackend | None = None,
    config: SegmenterConfig | None = None,
) -> bytes:
    """Concatenate only the speech of a buffer — what gets sent to Whisper."""
    return b"".join(s.pcm for s in segment_pcm(pcm, vad, config))
