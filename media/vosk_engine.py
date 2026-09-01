"""
VOSK — offline streaming speech recognition
============================================
VOSK is the low-latency half of the ASR stack. Unlike Whisper it is a true
streaming recogniser: you push PCM in as it arrives and it emits *partial*
hypotheses within tens of milliseconds, refining them until an utterance ends.
That is what makes the live transcript in the Command Centre feel responsive.

Trade-off against Whisper: VOSK is faster and runs comfortably on CPU with no
GPU and no network, but is less accurate on accents, jargon and punctuation.
The `hybrid` mode in `media.asr` plays to both — VOSK drives the live caption,
Whisper produces the final text that actually reaches the orchestrator.

Models are not bundled (the small English model is ~40 MB, large ~1.8 GB).
Fetch one with:

    python -m media.vosk_engine --download            # small English model
    python -m media.vosk_engine --download --model-name vosk-model-en-us-0.22

and point VOSK_MODEL_PATH at it, or drop it in ./models/vosk/ where this module
looks by default. Model list: https://alphacephei.com/vosk/models
"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .audio import SAMPLE_RATE, decode_to_pcm

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

VOSK_MODEL_PATH: str = os.getenv("VOSK_MODEL_PATH", "")
VOSK_MODEL_DIR:  Path = Path(os.getenv("VOSK_MODEL_DIR", "./models/vosk"))
VOSK_MODEL_NAME: str = os.getenv("VOSK_MODEL_NAME", "vosk-model-small-en-us-0.15")
VOSK_LOG_LEVEL:  int = int(os.getenv("VOSK_LOG_LEVEL", "-1"))   # -1 silences Kaldi chatter
VOSK_WORDS:      bool = os.getenv("VOSK_WORDS", "true").lower() in ("1", "true", "yes")

MODEL_BASE_URL = "https://alphacephei.com/vosk/models"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class Word:
    word:  str
    start: float
    end:   float
    conf:  float

    def to_dict(self) -> dict:
        return {
            "word":  self.word,
            "start": round(self.start, 3),
            "end":   round(self.end, 3),
            "conf":  round(self.conf, 3),
        }


@dataclass
class VoskResult:
    """One recogniser output. `partial=True` means it may still change."""

    text:    str
    partial: bool = False
    words:   list = field(default_factory=list)

    @property
    def confidence(self) -> float:
        """Mean word confidence, or 0.0 when word timings are disabled."""
        if not self.words:
            return 0.0
        return sum(w.conf for w in self.words) / len(self.words)

    def to_dict(self) -> dict:
        return {
            "text":       self.text,
            "partial":    self.partial,
            "confidence": round(self.confidence, 3),
            "words":      [w.to_dict() for w in self.words],
        }


def _parse(raw: str) -> VoskResult:
    """Turn a VOSK JSON blob into a VoskResult."""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return VoskResult(text="", partial=True)

    if "partial" in data:
        return VoskResult(text=(data.get("partial") or "").strip(), partial=True)

    words = [
        Word(
            word=w.get("word", ""),
            start=float(w.get("start", 0.0)),
            end=float(w.get("end", 0.0)),
            conf=float(w.get("conf", 0.0)),
        )
        for w in data.get("result", [])
    ]
    return VoskResult(text=(data.get("text") or "").strip(), partial=False, words=words)


# ---------------------------------------------------------------------------
# Model resolution & loading
# ---------------------------------------------------------------------------

def resolve_model_path(path: str | Path | None = None) -> Path | None:
    """
    Find a usable model directory, in order: the explicit argument, then
    VOSK_MODEL_PATH, then VOSK_MODEL_DIR/VOSK_MODEL_NAME, then any single
    model-looking directory inside VOSK_MODEL_DIR. Returns None if none exist,
    which callers report as "vosk unavailable" rather than crashing.
    """
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    if VOSK_MODEL_PATH:
        candidates.append(Path(VOSK_MODEL_PATH))
    candidates.append(VOSK_MODEL_DIR / VOSK_MODEL_NAME)

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    if VOSK_MODEL_DIR.is_dir():
        # A VOSK model directory always carries an `am` or `graph` subfolder.
        for child in sorted(VOSK_MODEL_DIR.iterdir()):
            if child.is_dir() and ((child / "am").exists() or (child / "graph").exists()):
                return child
    return None


_model_cache: dict = {}


def load_model(path: str | Path | None = None):
    """Load (and cache) a VOSK model. Loading is slow; do it once per path."""
    from vosk import Model, SetLogLevel  # type: ignore

    resolved = resolve_model_path(path)
    if resolved is None:
        raise FileNotFoundError(
            "No VOSK model found. Download one with "
            "`python -m media.vosk_engine --download`, or set VOSK_MODEL_PATH."
        )

    key = str(resolved.resolve())
    if key not in _model_cache:
        SetLogLevel(VOSK_LOG_LEVEL)
        print(f"[vosk] Loading model: {resolved}")
        _model_cache[key] = Model(key)
        print("[vosk] Model loaded.")
    return _model_cache[key]


def _package_installed() -> bool:
    """Is `vosk` importable? Checked with find_spec so we do not pay the import."""
    from importlib.util import find_spec

    try:
        return find_spec("vosk") is not None
    except (ImportError, ValueError):
        return False


def is_available() -> bool:
    """True when both the `vosk` package and a model are present."""
    return _package_installed() and resolve_model_path() is not None


def status() -> dict:
    """Diagnostic summary for the /engines endpoint."""
    installed = _package_installed()
    version = None
    if installed:
        # Read the version from package metadata rather than importing vosk.
        from importlib.metadata import PackageNotFoundError, version as pkg_version

        try:
            version = pkg_version("vosk")
        except PackageNotFoundError:
            version = "unknown"

    model = resolve_model_path()
    return {
        "package_installed": installed,
        "version":           version,
        "model_path":        str(model) if model else None,
        "available":         installed and model is not None,
        "search_dir":        str(VOSK_MODEL_DIR.resolve()),
    }


# ---------------------------------------------------------------------------
# Streaming recogniser
# ---------------------------------------------------------------------------

class VoskStream:
    """
    A single live recognition session. One per websocket connection — a
    KaldiRecognizer carries per-speaker decoding state and must not be shared
    across concurrent streams.

    Usage:
        stream = VoskStream()
        for result in stream.accept(pcm_chunk):   # partials and finals
            ...
        final = stream.finish()
    """

    def __init__(
        self,
        model=None,
        sample_rate: int = SAMPLE_RATE,
        words: bool = VOSK_WORDS,
        grammar: list | None = None,
    ):
        from vosk import KaldiRecognizer  # type: ignore

        self.sample_rate = sample_rate
        self._model = model if model is not None else load_model()

        if grammar:
            # Constrained vocabulary — dramatically improves accuracy for
            # command phrases. "[unk]" lets it still reject out-of-grammar audio.
            phrases = json.dumps(list(grammar) + ["[unk]"])
            self._rec = KaldiRecognizer(self._model, sample_rate, phrases)
        else:
            self._rec = KaldiRecognizer(self._model, sample_rate)

        self._rec.SetWords(words)
        self._last_partial = ""
        self.closed = False

    def accept(self, pcm: bytes) -> list:
        """
        Push audio. Returns the results produced by this chunk: a final
        VoskResult when VOSK decided an utterance ended, otherwise at most one
        partial — and only when the partial text actually changed, so callers
        are not spammed with identical frames.
        """
        if self.closed or not pcm:
            return []

        out: list = []
        if self._rec.AcceptWaveform(pcm):
            result = _parse(self._rec.Result())
            self._last_partial = ""
            if result.text:
                out.append(result)
        else:
            partial = _parse(self._rec.PartialResult())
            if partial.text and partial.text != self._last_partial:
                self._last_partial = partial.text
                out.append(partial)
        return out

    def finish(self) -> VoskResult:
        """Flush the decoder and return the last final result."""
        if self.closed:
            return VoskResult(text="", partial=False)
        self.closed = True
        self._last_partial = ""
        return _parse(self._rec.FinalResult())

    def reset(self) -> None:
        """Start a new utterance without reloading the model."""
        self._rec.Reset()
        self._last_partial = ""
        self.closed = False


# ---------------------------------------------------------------------------
# Batch transcription
# ---------------------------------------------------------------------------

def transcribe_pcm(
    pcm: bytes,
    model=None,
    sample_rate: int = SAMPLE_RATE,
    chunk_bytes: int = 8000,
    grammar: list | None = None,
) -> VoskResult:
    """
    Run a complete PCM buffer through VOSK and return one merged result.
    The buffer is still fed in chunks — VOSK expects a stream, and a single
    enormous AcceptWaveform call is markedly slower.
    """
    stream = VoskStream(model=model, sample_rate=sample_rate, grammar=grammar)
    texts: list = []
    words: list = []

    for i in range(0, len(pcm), chunk_bytes):
        for res in stream.accept(pcm[i : i + chunk_bytes]):
            if not res.partial and res.text:
                texts.append(res.text)
                words.extend(res.words)

    final = stream.finish()
    if final.text:
        texts.append(final.text)
        words.extend(final.words)

    return VoskResult(text=" ".join(texts).strip(), partial=False, words=words)


def transcribe_file(path: str | Path, model=None, grammar: list | None = None) -> VoskResult:
    """Decode any audio/video file and transcribe it with VOSK."""
    return transcribe_pcm(decode_to_pcm(path), model=model, grammar=grammar)


# ---------------------------------------------------------------------------
# Model download helper
# ---------------------------------------------------------------------------

def download_model(name: str = VOSK_MODEL_NAME, dest_dir: Path = VOSK_MODEL_DIR) -> Path:
    """
    Fetch and unpack a model from the VOSK model index. Returns the model
    directory. Existing downloads are reused.
    """
    import urllib.request

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / name
    if target.is_dir():
        print(f"[vosk] Model already present: {target}")
        return target

    url = f"{MODEL_BASE_URL}/{name}.zip"
    archive = dest_dir / f"{name}.zip"
    print(f"[vosk] Downloading {url}")

    # A terminal gets an updating one-liner; a log file gets one line per 10%
    # rather than thousands of carriage-returned fragments.
    tty = sys.stdout.isatty()
    last = [-1]

    def _progress(block: int, block_size: int, total: int) -> None:
        if total <= 0:
            return
        pct = min(100, block * block_size * 100 // total)
        step = 1 if tty else 10
        if pct // step == last[0] // step and pct != 100:
            return
        last[0] = pct
        if tty:
            print(f"\r[vosk] {pct}%", end="", flush=True)
        else:
            print(f"[vosk] {pct}%", flush=True)

    urllib.request.urlretrieve(url, archive, reporthook=_progress)
    if tty:
        print()

    print(f"[vosk] Extracting to {dest_dir}")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest_dir)
    archive.unlink(missing_ok=True)

    if not target.is_dir():
        raise RuntimeError(f"Archive did not contain the expected folder {name!r}")
    print(f"[vosk] Ready: {target}")
    return target


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VOSK offline speech recognition")
    parser.add_argument("--file", help="Audio file to transcribe")
    parser.add_argument("--download", action="store_true", help="Download a VOSK model")
    parser.add_argument("--model-name", default=VOSK_MODEL_NAME, help="Model name to download")
    parser.add_argument("--model-path", default=None, help="Path to an existing model directory")
    parser.add_argument("--status", action="store_true", help="Show VOSK availability")
    args = parser.parse_args()

    if args.download:
        download_model(args.model_name)
    elif args.status:
        print(json.dumps(status(), indent=2))
    elif args.file:
        result = transcribe_file(args.file, model=load_model(args.model_path))
        print(json.dumps(result.to_dict(), indent=2))
    else:
        parser.print_help()
