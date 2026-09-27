"""
Text to speech for Apex's spoken replies
========================================
Turns one reply fragment (a sentence or two) into audio the browser can play
through Web Audio. Playing real audio, rather than handing text to the browser's
speechSynthesis, is what makes three things possible: a voice that is not the
Windows SAPI one, a waveform the Radial can react to, and a stop that lands
within one audio block.

Engines, in the order `auto` tries them:

  elevenlabs  A natural voice on the account key. Uses Apex's OWN voice id
              (APEX_ELEVENLABS_VOICE_ID), never ELEVENLABS_VOICE_ID: that one is
              Aaron's clone, and clone output is gated by governance. Apex
              speaking in Aaron's voice would bypass that gate on every reply.
              Only tried when TTS_ENGINE is "elevenlabs" or a request asks for it.
  kokoro      Local and offline: Kokoro-82M (Apache 2.0) through kokoro-onnx,
              no torch needed. Free, no key, no network.
  (none)      Nothing usable. Callers get TTSUnavailable and the browser falls
              back to its own voice, so the assistant degrades instead of going
              mute.

A cloud failure falls back to Kokoro and says so (`fallback_from`), so a dead
key or a network blip never silences the assistant.

Kokoro's speed depends on the machine. Warm-up measures the real-time factor
(seconds of compute per second of audio) and `status()` reports it; the client
uses it to decide whether local speech can keep ahead of playback. On a busy
4-core laptop it may not, and then the browser voice is the better experience.

Model files (not in git, ~340 MB):
  %LOCALAPPDATA%/wijerco/kokoro/  or  ~/.cache/wijerco/kokoro/  or KOKORO_MODEL_DIR
    kokoro-v1.0.onnx        (fp32, preferred)
    voices-v1.0.bin
Fetch them with:  python -m media.tts --fetch

Use the fp32 model. Measured on wijwork (i7-1185G7, 4 threads, 60% background
load), the int8 file is about 3.4x SLOWER, not faster: real-time factor 2.3 to
2.8 against fp32's 0.68 to 0.84. Its quantised convolutions have no fast CPU
path here. fp32 costs roughly 600 MB of RAM in the voice service.
"""

from __future__ import annotations

import io
import os
import re
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

SAMPLE_RATE = 24_000          # both engines produce 24 kHz mono

TTS_ENGINE: str = os.getenv("TTS_ENGINE", "auto").strip().lower()      # auto | kokoro | elevenlabs
APEX_TTS_VOICE: str = os.getenv("APEX_TTS_VOICE", "bf_emma").strip()
APEX_TTS_SPEED: float = float(os.getenv("APEX_TTS_SPEED", "1.05"))
TTS_MAX_CHARS: int = int(os.getenv("TTS_MAX_CHARS", "600"))            # per request
TTS_THREADS: int = int(os.getenv("TTS_THREADS", "0"))                  # 0 = physical cores

ELEVEN_VOICE_ENV = "APEX_ELEVENLABS_VOICE_ID"
ELEVEN_MODEL: str = os.getenv("APEX_ELEVENLABS_MODEL", "eleven_flash_v2_5")
ELEVEN_TIMEOUT_S: float = float(os.getenv("APEX_ELEVENLABS_TIMEOUT_S", "10"))

MODEL_FILES = ("kokoro-v1.0.onnx", "kokoro-v1.0.fp16.onnx", "kokoro-v1.0.int8.onnx")
VOICES_FILE = "voices-v1.0.bin"
RELEASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"

# Kokoro's language pipeline is chosen by the voice name's first letter.
_LANG = {"a": "en-us", "b": "en-gb"}

# Voices worth offering in the picker. All 54 still work by name.
SUGGESTED_VOICES = [
    ("bf_emma", "British, female"),
    ("bm_george", "British, male"),
    ("bm_fable", "British, male, warmer"),
    ("bf_isabella", "British, female, brighter"),
    ("af_heart", "American, female (Kokoro's best-rated)"),
    ("af_bella", "American, female"),
    ("am_michael", "American, male"),
]


class TTSUnavailable(RuntimeError):
    """No engine could speak this text. The browser voice is the fallback."""


def native_load_hint(package: str, exc: BaseException) -> str:
    """
    Explain a package that is installed but will not import.

    On wijerco (27 Sep 2026) both onnxruntime and ctranslate2 failed with
    "Could not find module ... (or one of its dependencies)" while pip showed
    them installed, and the old message said "not installed", which sent the
    reader the wrong way. The usual cause on Windows is a missing Microsoft
    Visual C++ Redistributable (x64); python.org Python ships vcruntime140 but
    not msvcp140.
    """
    msg = f"{package} is installed but will not load: {type(exc).__name__}: {str(exc)[:200]}"
    text = str(exc).lower()
    if os.name == "nt" and ("dll" in text or "could not find module" in text or "specified module" in text):
        msg += (". On Windows this usually means the Microsoft Visual C++ Redistributable (x64) "
                "is missing: https://aka.ms/vs/17/release/vc_redist.x64.exe")
    return msg


@dataclass
class Speech:
    pcm: bytes                     # int16 little-endian mono
    sample_rate: int
    engine: str
    voice: str
    synth_ms: float
    fallback_from: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def duration_s(self) -> float:
        return len(self.pcm) / 2 / self.sample_rate if self.sample_rate else 0.0

    def to_wav(self) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(self.pcm)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Text hygiene
# ---------------------------------------------------------------------------

_URL = re.compile(r"https?://\S+|www\.\S+")
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_FENCE = re.compile(r"```[\s\S]*?```")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
_STAGE = re.compile(r"<<[^<>]{0,80}>>")


def speakable(text: str) -> str:
    """
    Strip what should never be read aloud: code, links, markdown marks, emoji.
    The persona asks for plain speech, but a prompt is a request; this is the
    guarantee. Numbers and punctuation stay, because both engines read them.
    """
    t = text or ""
    t = _FENCE.sub(" ", t)
    t = _MD_LINK.sub(r"\1", t)
    t = _URL.sub("the link on screen", t)
    t = _STAGE.sub(" ", t)
    t = t.replace("`", "")
    t = re.sub(r"^\s{0,3}#{1,6}\s+", "", t, flags=re.M)
    t = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
    t = re.sub(r"(?<![\w*])[*_](\S.*?\S|\S)[*_](?![\w*])", r"\1", t)
    t = _EMOJI.sub("", t)
    t = t.replace("&", " and ").replace("—", ", ").replace("–", ", ")
    return " ".join(t.split()).strip()


# ---------------------------------------------------------------------------
# Kokoro (local)
# ---------------------------------------------------------------------------

def model_dir() -> Path:
    env = os.getenv("KOKORO_MODEL_DIR", "").strip()
    if env:
        return Path(env)
    base = os.getenv("LOCALAPPDATA")
    if base:
        return Path(base) / "wijerco" / "kokoro"
    return Path.home() / ".cache" / "wijerco" / "kokoro"


def _model_path() -> Path | None:
    """Explicit KOKORO_MODEL wins; otherwise the best file present."""
    explicit = os.getenv("KOKORO_MODEL", "").strip()
    d = model_dir()
    if explicit:
        p = Path(explicit)
        p = p if p.is_absolute() else d / p
        return p if p.exists() else None
    for name in MODEL_FILES:
        p = d / name
        if p.exists() and p.stat().st_size > 1_000_000:
            return p
    return None


class _KokoroEngine:
    def __init__(self):
        self._k = None
        self._lock = threading.Lock()        # one inference at a time; ORT uses the cores
        self.error = ""
        self.model_file = ""
        self.load_s = 0.0
        self.rtf: float | None = None        # measured at warm-up
        self.load_failed = False             # installed and present, but would not load

    def installed(self) -> bool:
        try:
            import kokoro_onnx  # noqa: F401
        except ModuleNotFoundError:
            self.error = "kokoro-onnx is not installed (pip install kokoro-onnx)"
            return False
        except Exception as exc:  # noqa: BLE001 — installed, but its native libraries will not load
            self.error = native_load_hint("kokoro-onnx", exc)
            return False
        if self.error.startswith("kokoro-onnx"):
            self.error = ""                  # fixed since the last check
        return True

    def files_present(self) -> bool:
        return _model_path() is not None and (model_dir() / VOICES_FILE).exists()

    def load(self):
        if self._k is not None:
            return self._k
        with self._lock:
            if self._k is not None:
                return self._k
            if not self.installed():
                raise TTSUnavailable(self.error)
            model = _model_path()
            voices = model_dir() / VOICES_FILE
            if model is None or not voices.exists():
                self.error = f"Kokoro model files missing in {model_dir()} (python -m media.tts --fetch)"
                raise TTSUnavailable(self.error)
            t0 = time.perf_counter()
            try:
                import onnxruntime as ort
                from kokoro_onnx import Kokoro

                opts = ort.SessionOptions()
                threads = TTS_THREADS or max(1, (os.cpu_count() or 2) // 2)
                opts.intra_op_num_threads = threads
                opts.inter_op_num_threads = 1
                opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
                session = ort.InferenceSession(
                    str(model), sess_options=opts, providers=["CPUExecutionProvider"]
                )
                self._k = Kokoro.from_session(session, str(voices))
            except Exception as exc:  # noqa: BLE001
                self.error = f"Kokoro failed to load: {exc}"
                self.load_failed = True
                raise TTSUnavailable(self.error) from exc
            self.load_s = time.perf_counter() - t0
            self.model_file = model.name
            self.error = ""
            return self._k

    def voices(self) -> list:
        try:
            return sorted(self.load().get_voices())
        except TTSUnavailable:
            return []

    def synth(self, text: str, voice: str, speed: float) -> Speech:
        import numpy as np

        k = self.load()
        voice = voice if voice in k.get_voices() else APEX_TTS_VOICE
        if voice not in k.get_voices():
            voice = "bf_emma"
        lang = _LANG.get(voice[:1], "en-us")
        t0 = time.perf_counter()
        with self._lock:
            samples, sr = k.create(text, voice=voice, speed=float(speed), lang=lang)
        ms = (time.perf_counter() - t0) * 1000
        pcm = (np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0) * 32767).astype("<i2").tobytes()
        speech = Speech(pcm=pcm, sample_rate=int(sr), engine="kokoro", voice=voice, synth_ms=ms)
        if speech.duration_s > 0.4:
            # A running estimate, weighted to recent turns: load on this machine
            # changes through the day and the client's choice should follow it.
            rtf = (ms / 1000) / speech.duration_s
            self.rtf = rtf if self.rtf is None else round(0.6 * self.rtf + 0.4 * rtf, 3)
        return speech

    def warm(self) -> None:
        """Load the model and measure how fast this machine speaks."""
        try:
            self.load()
            # The first inference pays one-off costs (allocation, kernel choice)
            # and measured 1.5x real time against 0.47x for the next one on
            # wijwork. Counting it would push Auto onto the browser voice for no
            # reason, so prime first and measure the second.
            self.synth("Ready.", APEX_TTS_VOICE, APEX_TTS_SPEED)
            self.rtf = None
            self.synth("Apex is online and ready when you are.", APEX_TTS_VOICE, APEX_TTS_SPEED)
        except Exception as exc:  # noqa: BLE001
            self.error = self.error or str(exc)


_kokoro = _KokoroEngine()


# ---------------------------------------------------------------------------
# ElevenLabs (cloud, Apex's own voice)
# ---------------------------------------------------------------------------

def _eleven_key() -> str:
    return os.getenv("ELEVENLABS_API_KEY", "").strip()


def _eleven_voice() -> str:
    # Deliberately NOT falling back to ELEVENLABS_VOICE_ID. See module docstring.
    return os.getenv(ELEVEN_VOICE_ENV, "").strip()


def elevenlabs_ready() -> tuple:
    if not _eleven_key():
        return False, "ELEVENLABS_API_KEY is not set"
    if not _eleven_voice():
        return False, (f"{ELEVEN_VOICE_ENV} is not set. Pick a library voice for Apex; "
                       "the clone voice is never used for replies")
    clone = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
    if clone and clone == _eleven_voice():
        return False, (f"{ELEVEN_VOICE_ENV} is the clone voice. Apex must not speak as Aaron; "
                       "choose a different voice")
    return True, ""


def _eleven_synth(text: str, speed: float) -> Speech:
    import httpx

    ok, why = elevenlabs_ready()
    if not ok:
        raise TTSUnavailable(why)
    voice = _eleven_voice()
    t0 = time.perf_counter()
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream",
        params={"output_format": f"pcm_{SAMPLE_RATE}"},
        headers={"xi-api-key": _eleven_key(), "Content-Type": "application/json"},
        json={
            "text": text,
            "model_id": ELEVEN_MODEL,
            "voice_settings": {
                "stability": float(os.getenv("APEX_ELEVENLABS_STABILITY", "0.45")),
                "similarity_boost": float(os.getenv("APEX_ELEVENLABS_SIMILARITY", "0.75")),
                "speed": max(0.7, min(1.2, float(speed))),
            },
        },
        timeout=ELEVEN_TIMEOUT_S,
    )
    if r.status_code >= 400:
        raise TTSUnavailable(f"ElevenLabs returned {r.status_code}: {r.text[:160]}")
    pcm = r.content
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return Speech(pcm=pcm, sample_rate=SAMPLE_RATE, engine="elevenlabs", voice=voice,
                  synth_ms=(time.perf_counter() - t0) * 1000,
                  meta={"characters": len(text), "model": ELEVEN_MODEL})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _order(engine: str) -> list:
    e = (engine or TTS_ENGINE or "auto").lower()
    if e == "elevenlabs":
        return ["elevenlabs", "kokoro"]
    if e == "kokoro":
        return ["kokoro"]
    # auto: the paid engine only when it is the configured default
    return ["elevenlabs", "kokoro"] if TTS_ENGINE == "elevenlabs" else ["kokoro"]


def synthesize(text: str, engine: str = "", voice: str = "", speed: float | None = None) -> Speech:
    """
    Speak one fragment. Raises TTSUnavailable when no engine can, which callers
    turn into "use the browser voice" rather than an error the user hears about.
    """
    clean = speakable(text)[:TTS_MAX_CHARS]
    if not clean:
        raise TTSUnavailable("nothing speakable in the text")
    speed = APEX_TTS_SPEED if speed is None else max(0.6, min(1.6, float(speed)))

    failures: list = []
    for name in _order(engine):
        try:
            if name == "elevenlabs":
                speech = _eleven_synth(clean, speed)
            else:
                speech = _kokoro.synth(clean, voice or APEX_TTS_VOICE, speed)
            if failures:
                speech.fallback_from = failures[0][0]
                speech.meta["fallback_reason"] = failures[0][1][:200]
            return speech
        except TTSUnavailable as exc:
            failures.append((name, str(exc)))
        except Exception as exc:  # noqa: BLE001 — a network or model fault is a fallback, not a crash
            failures.append((name, f"{type(exc).__name__}: {exc}"))
    raise TTSUnavailable("; ".join(f"{n}: {why}" for n, why in failures) or "no engine")


def status() -> dict:
    """What can speak on this machine, and how fast. The UI chooses from this."""
    k_installed = _kokoro.installed()
    k_files = _kokoro.files_present()
    e_ok, e_why = elevenlabs_ready()
    return {
        "default": TTS_ENGINE,
        "voice": APEX_TTS_VOICE,
        "speed": APEX_TTS_SPEED,
        "sample_rate": SAMPLE_RATE,
        "engines": {
            "kokoro": {
                "available": k_installed and k_files and not _kokoro.load_failed,
                "installed": k_installed,
                "model_files": k_files,
                "model_dir": str(model_dir()),
                "model": _kokoro.model_file or (p.name if (p := _model_path()) else ""),
                "loaded": _kokoro._k is not None,
                "load_s": round(_kokoro.load_s, 2),
                "rtf": _kokoro.rtf,
                "error": _kokoro.error,
            },
            "elevenlabs": {
                "available": e_ok,
                "detail": e_why,
                "model": ELEVEN_MODEL,
            },
        },
        "voices": [{"id": v, "label": label} for v, label in SUGGESTED_VOICES],
    }


def warm_in_background() -> threading.Thread | None:
    """Load Kokoro and measure it without holding up service start-up."""
    if not (_kokoro.installed() and _kokoro.files_present()):
        return None
    t = threading.Thread(target=_kokoro.warm, name="tts-warm", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def doctor() -> int:
    """
    Can the native pieces of the voice stack load on this machine? Prints one
    line per library and returns 1 if any fails, so a deploy script can act on
    it (wijerco needed the Visual C++ runtime; pip alone could not tell).
    """
    import importlib

    bad = 0
    for mod in ("numpy", "onnxruntime", "ctranslate2", "faster_whisper", "vosk", "kokoro_onnx"):
        try:
            m = importlib.import_module(mod)
            print(f"  ok    {mod:<15} {getattr(m, '__version__', '')}")
        except ModuleNotFoundError:
            bad += 1
            print(f"  FAIL  {mod:<15} not installed")
        except Exception as exc:  # noqa: BLE001
            bad += 1
            print(f"  FAIL  {mod:<15} {native_load_hint(mod, exc)}")
    return 1 if bad else 0


def fetch_models(which: str = "kokoro-v1.0.onnx") -> Path:
    """Download the model and voices into model_dir(). Skips files already there."""
    import httpx

    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name in (VOICES_FILE, which):
        dest = d / name
        if dest.exists() and dest.stat().st_size > 1_000_000:
            print(f"[tts] have {name}")
            continue
        part = dest.with_suffix(dest.suffix + ".part")
        print(f"[tts] fetching {name} ...", flush=True)
        with httpx.stream("GET", f"{RELEASE_URL}/{name}", follow_redirects=True, timeout=600) as r:
            r.raise_for_status()
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
        part.replace(dest)
        print(f"[tts] saved {dest} ({dest.stat().st_size / 1e6:.0f} MB)")
    return d


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Apex text to speech")
    ap.add_argument("--fetch", nargs="?", const="kokoro-v1.0.onnx",
                    help="download Kokoro model files (default fp32, the fast one on Intel CPUs)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--say", default="", help="synthesise this text to --out")
    ap.add_argument("--out", default="apex_tts.wav")
    ap.add_argument("--engine", default="")
    ap.add_argument("--voice", default="")
    ap.add_argument("--doctor", action="store_true",
                    help="check that the voice stack's native libraries load; exit 1 if not")
    args = ap.parse_args()

    if args.doctor:
        raise SystemExit(doctor())
    if args.fetch:
        fetch_models(args.fetch)
    if args.say:
        s = synthesize(args.say, engine=args.engine, voice=args.voice)
        Path(args.out).write_bytes(s.to_wav())
        print(json.dumps({"engine": s.engine, "voice": s.voice, "synth_ms": round(s.synth_ms),
                          "audio_s": round(s.duration_s, 2), "out": args.out,
                          "fallback_from": s.fallback_from}))
    if args.status or not (args.fetch or args.say):
        print(json.dumps(status(), indent=2))
