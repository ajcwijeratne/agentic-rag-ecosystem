"""
Audio Utilities — shared PCM plumbing for the voice stack
==========================================================
Every component in the voice pipeline (VAD, Whisper, VOSK) agrees on one
canonical format:

    16 kHz · mono · signed 16-bit little-endian PCM

This module is the single place that converts *into* that format, slices it
into fixed-duration frames, and converts it back out to the float32 arrays
Whisper and Silero expect. Nothing here depends on a specific ASR engine.

Decoding arbitrary containers (mp3, m4a, wav, mp4, webm/opus from a browser)
uses ffmpeg, which the project already requires for the video pipeline.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Canonical format
# ---------------------------------------------------------------------------

SAMPLE_RATE: int = 16_000
CHANNELS:    int = 1
SAMPLE_WIDTH: int = 2          # bytes per sample (int16)
BYTES_PER_SECOND: int = SAMPLE_RATE * SAMPLE_WIDTH


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


# ---------------------------------------------------------------------------
# Conversions
# ---------------------------------------------------------------------------

def pcm_to_float32(pcm: bytes) -> np.ndarray:
    """int16 PCM bytes -> float32 array in [-1.0, 1.0] (Whisper / Silero input)."""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    # An odd trailing byte would misalign the whole buffer; drop it.
    if len(pcm) % 2:
        pcm = pcm[:-1]
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def float32_to_pcm(samples: np.ndarray) -> bytes:
    """float32 array in [-1.0, 1.0] -> int16 PCM bytes."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


def duration_s(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> float:
    """Wall-clock duration of an int16 PCM buffer."""
    return len(pcm) / (sample_rate * SAMPLE_WIDTH)


def resample_pcm(pcm: bytes, src_rate: int, dst_rate: int = SAMPLE_RATE) -> bytes:
    """
    Linear resample of int16 PCM. Adequate for VAD/ASR framing at the rates
    browsers hand us (44.1k/48k -> 16k); ffmpeg does the heavy lifting for
    files, this covers live streams where spawning a process per chunk is not
    an option.
    """
    if src_rate == dst_rate or not pcm:
        return pcm
    samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % 2)], dtype=np.int16)
    if samples.size == 0:
        return b""
    n_out = int(round(samples.size * dst_rate / src_rate))
    if n_out <= 0:
        return b""
    src_idx = np.linspace(0, samples.size - 1, num=n_out, dtype=np.float64)
    out = np.interp(src_idx, np.arange(samples.size), samples.astype(np.float64))
    return out.astype(np.int16).tobytes()


def to_mono(pcm: bytes, channels: int) -> bytes:
    """Downmix interleaved int16 PCM to mono by averaging channels."""
    if channels <= 1 or not pcm:
        return pcm
    samples = np.frombuffer(pcm[: len(pcm) - (len(pcm) % (2 * channels))], dtype=np.int16)
    if samples.size == 0:
        return b""
    frames = samples.reshape(-1, channels).astype(np.int32)
    return frames.mean(axis=1).astype(np.int16).tobytes()


# ---------------------------------------------------------------------------
# File / container decoding
# ---------------------------------------------------------------------------

def decode_to_pcm(path: Path | str, sample_rate: int = SAMPLE_RATE) -> bytes:
    """
    Decode any audio or video file to canonical PCM bytes.

    Fast path: a WAV that is already 16 kHz mono int16 is read directly, so a
    machine without ffmpeg can still run the whole pipeline on prepared WAVs.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {path}")

    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as wf:
                if wf.getsampwidth() == SAMPLE_WIDTH:
                    raw = wf.readframes(wf.getnframes())
                    raw = to_mono(raw, wf.getnchannels())
                    return resample_pcm(raw, wf.getframerate(), sample_rate)
        except wave.Error:
            pass  # compressed WAV (e.g. a-law) — fall through to ffmpeg

    return decode_bytes_to_pcm(path.read_bytes(), sample_rate=sample_rate)


def _wav_bytes_to_pcm(data: bytes, sample_rate: int) -> bytes | None:
    """
    Decode an in-memory WAV without ffmpeg. Returns None when the blob is not a
    plain PCM WAV, so the caller falls through to ffmpeg.
    """
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            if wf.getsampwidth() != SAMPLE_WIDTH:
                return None            # a-law/µ-law/24-bit: let ffmpeg handle it
            raw = wf.readframes(wf.getnframes())
    except (wave.Error, EOFError):
        return None
    return resample_pcm(to_mono(raw, wf.getnchannels()), wf.getframerate(), sample_rate)


def decode_bytes_to_pcm(data: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """
    Decode an in-memory audio blob (an upload, a browser MediaRecorder chunk)
    to canonical PCM.

    A plain PCM WAV is decoded in-process, so uploads work on a machine with no
    ffmpeg. Everything else (webm/opus from MediaRecorder, mp3, m4a) is piped
    through ffmpeg, which those formats genuinely require.
    """
    wav = _wav_bytes_to_pcm(data, sample_rate)
    if wav is not None:
        return wav

    if not ffmpeg_available():
        raise RuntimeError(
            "ffmpeg is required to decode this audio format. "
            "Install it (choco install ffmpeg / brew install ffmpeg), or supply "
            "a 16 kHz mono 16-bit WAV."
        )

    proc = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", str(CHANNELS), "-ar", str(sample_rate),
            "pipe:1",
        ],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg decode failed: {proc.stderr.decode(errors='replace')[:400]}")
    return proc.stdout


def write_wav(pcm: bytes, path: Path | str, sample_rate: int = SAMPLE_RATE) -> Path:
    """Write canonical PCM out as a WAV file (used to hand segments to Whisper)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return path


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Frame:
    """One fixed-duration slice of audio, with its offset in the stream."""
    pcm:     bytes
    index:   int
    start_s: float
    end_s:   float


def frame_bytes(frame_ms: int, sample_rate: int = SAMPLE_RATE) -> int:
    """Byte length of one frame. WebRTC VAD only accepts 10/20/30 ms frames."""
    return int(sample_rate * frame_ms / 1000) * SAMPLE_WIDTH


def iter_frames(pcm: bytes, frame_ms: int = 30, sample_rate: int = SAMPLE_RATE):
    """
    Yield whole Frames from a PCM buffer. A trailing partial frame is dropped:
    VAD backends reject short frames, and the residue is at most `frame_ms`.
    """
    step = frame_bytes(frame_ms, sample_rate)
    if step <= 0:
        return
    n = len(pcm) // step
    for i in range(n):
        chunk = pcm[i * step : (i + 1) * step]
        start = i * frame_ms / 1000.0
        yield Frame(pcm=chunk, index=i, start_s=start, end_s=start + frame_ms / 1000.0)


def rms_dbfs(pcm: bytes) -> float:
    """Loudness of a PCM buffer in dBFS. -100.0 for digital silence."""
    samples = pcm_to_float32(pcm)
    if samples.size == 0:
        return -100.0
    rms = float(np.sqrt(np.mean(np.square(samples))))
    if rms <= 1e-10:
        return -100.0
    return 20.0 * float(np.log10(rms))
