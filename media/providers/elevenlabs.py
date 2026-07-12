"""
ElevenLabs provider: text-to-speech with Aaron's cloned voice.

Plain REST client, no SDK. Env:
  ELEVENLABS_API_KEY    required
  ELEVENLABS_VOICE_ID   the cloned voice (from scripts/setup_voice_clone.py)
  ELEVENLABS_MODEL_ID   default eleven_multilingual_v2

Every output produced with the cloned voice is clone output. Callers mark the
registered asset meta with clone=True; governance blocks it at the
clone_output gate before it leaves the system.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

API_BASE = "https://api.elevenlabs.io"


def api_key() -> str:
    return os.getenv("ELEVENLABS_API_KEY", "")


def available() -> bool:
    return bool(api_key())


def _headers() -> dict[str, str]:
    return {"xi-api-key": api_key()}


def synthesize(
    text: str,
    out_path: str | Path,
    *,
    voice_id: str | None = None,
    model_id: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Render `text` to an mp3 at out_path with the configured voice."""
    if not available():
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    voice = voice_id or os.getenv("ELEVENLABS_VOICE_ID", "")
    if not voice:
        raise RuntimeError("ELEVENLABS_VOICE_ID is not set; run scripts/setup_voice_clone.py first")
    model = model_id or os.getenv("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2")

    resp = httpx.post(
        f"{API_BASE}/v1/text-to-speech/{voice}",
        headers={**_headers(), "Content-Type": "application/json", "Accept": "audio/mpeg"},
        json={
            "text": text,
            "model_id": model,
            "voice_settings": {
                "stability": float(os.getenv("ELEVENLABS_STABILITY", "0.5")),
                "similarity_boost": float(os.getenv("ELEVENLABS_SIMILARITY", "0.8")),
            },
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(resp.content)
    return {"path": str(out), "voice_id": voice, "model_id": model,
            "bytes": len(resp.content), "clone": True}


def list_voices(timeout: float = 30.0) -> list[dict[str, Any]]:
    if not available():
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    resp = httpx.get(f"{API_BASE}/v1/voices", headers=_headers(), timeout=timeout)
    resp.raise_for_status()
    return resp.json().get("voices", [])


def create_clone(
    name: str,
    sample_paths: list[str | Path],
    *,
    description: str = "",
    timeout: float = 300.0,
) -> dict[str, Any]:
    """Create an instant voice clone from local audio samples. Returns voice_id.

    Consent note: only clone a voice whose owner has consented in writing.
    scripts/setup_voice_clone.py enforces an explicit --i-consent flag.
    """
    if not available():
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    files = []
    handles = []
    try:
        for p in sample_paths:
            f = open(p, "rb")
            handles.append(f)
            files.append(("files", (Path(p).name, f, "audio/mpeg")))
        resp = httpx.post(
            f"{API_BASE}/v1/voices/add",
            headers=_headers(),
            data={"name": name, "description": description},
            files=files,
            timeout=timeout,
        )
        resp.raise_for_status()
    finally:
        for f in handles:
            try:
                f.close()
            except Exception:
                pass
    data = resp.json()
    return {"voice_id": data.get("voice_id"), "response": data}
