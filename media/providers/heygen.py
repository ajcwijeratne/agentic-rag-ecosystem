"""
HeyGen provider: talking-head video with Aaron's avatar clone.

Plain REST client. Env:
  HEYGEN_API_KEY     required
  HEYGEN_AVATAR_ID   the avatar created in HeyGen Studio (photo or video clone)
  HEYGEN_VOICE_ID    optional HeyGen voice; leave empty when supplying audio

Two input modes:
  * audio: upload a narration file (for example ElevenLabs clone output) and
    HeyGen lip-syncs the avatar to it. The self-clone pipeline default.
  * text: HeyGen speaks it with HEYGEN_VOICE_ID.

Generation is async on HeyGen's side: generate() submits, poll() waits for the
video URL, download() fetches the mp4. generate_and_download() does all three.
All outputs are clone output; callers mark asset meta clone=True and the
clone_output gate blocks them until Aaron approves.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx

API_BASE = "https://api.heygen.com"
UPLOAD_BASE = "https://upload.heygen.com"


def api_key() -> str:
    return os.getenv("HEYGEN_API_KEY", "")


def available() -> bool:
    return bool(api_key())


def _headers() -> dict[str, str]:
    return {"X-Api-Key": api_key()}


def upload_audio(path: str | Path, timeout: float = 300.0) -> str:
    """Upload a local audio file as a HeyGen asset. Returns the asset URL/id."""
    if not available():
        raise RuntimeError("HEYGEN_API_KEY is not set")
    p = Path(path)
    content_type = "audio/mpeg" if p.suffix.lower() in (".mp3", ".mpga") else "audio/wav"
    with p.open("rb") as f:
        resp = httpx.post(
            f"{UPLOAD_BASE}/v1/asset",
            headers={**_headers(), "Content-Type": content_type},
            content=f.read(),
            timeout=timeout,
        )
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    asset = data.get("url") or data.get("id")
    if not asset:
        raise RuntimeError(f"HeyGen upload returned no asset reference: {resp.text[:300]}")
    return str(asset)


def generate(
    *,
    avatar_id: str | None = None,
    audio_url: str | None = None,
    text: str | None = None,
    voice_id: str | None = None,
    width: int = 1280,
    height: int = 720,
    timeout: float = 60.0,
) -> str:
    """Submit a video generation job. Returns HeyGen's video_id."""
    if not available():
        raise RuntimeError("HEYGEN_API_KEY is not set")
    avatar = avatar_id or os.getenv("HEYGEN_AVATAR_ID", "")
    if not avatar:
        raise RuntimeError("HEYGEN_AVATAR_ID is not set; create your avatar in HeyGen Studio first")

    if audio_url:
        voice: dict[str, Any] = {"type": "audio", "audio_url": audio_url}
    elif text:
        vid = voice_id or os.getenv("HEYGEN_VOICE_ID", "")
        if not vid:
            raise RuntimeError("text mode needs HEYGEN_VOICE_ID (or supply audio instead)")
        voice = {"type": "text", "input_text": text, "voice_id": vid}
    else:
        raise RuntimeError("generate needs audio_url or text")

    resp = httpx.post(
        f"{API_BASE}/v2/video/generate",
        headers={**_headers(), "Content-Type": "application/json"},
        json={
            "video_inputs": [{
                "character": {"type": "avatar", "avatar_id": avatar, "avatar_style": "normal"},
                "voice": voice,
            }],
            "dimension": {"width": width, "height": height},
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json().get("data") or {}
    video_id = data.get("video_id")
    if not video_id:
        raise RuntimeError(f"HeyGen generate returned no video_id: {resp.text[:300]}")
    return str(video_id)


def poll(video_id: str, *, interval: float = 10.0, timeout: float = 900.0) -> dict[str, Any]:
    """Wait for a video to finish. Returns {status, video_url, ...}."""
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        resp = httpx.get(
            f"{API_BASE}/v1/video_status.get",
            headers=_headers(),
            params={"video_id": video_id},
            timeout=30.0,
        )
        resp.raise_for_status()
        last = resp.json().get("data") or {}
        status = last.get("status")
        if status == "completed":
            return last
        if status in ("failed", "error"):
            raise RuntimeError(f"HeyGen render failed: {last.get('error') or last}")
        time.sleep(interval)
    raise TimeoutError(f"HeyGen video {video_id} not ready after {timeout}s; last status {last.get('status')}")


def download(video_url: str, out_path: str | Path, timeout: float = 600.0) -> str:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", video_url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        with out.open("wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)
    return str(out)


def generate_and_download(
    out_path: str | Path,
    *,
    avatar_id: str | None = None,
    audio_path: str | Path | None = None,
    text: str | None = None,
    poll_timeout: float = 900.0,
) -> dict[str, Any]:
    """Full pipeline: (upload audio) -> generate -> poll -> download."""
    audio_url = upload_audio(audio_path) if audio_path else None
    video_id = generate(avatar_id=avatar_id, audio_url=audio_url, text=text)
    status = poll(video_id, timeout=poll_timeout)
    video_url = status.get("video_url")
    if not video_url:
        raise RuntimeError(f"HeyGen completed without a video_url: {status}")
    path = download(video_url, out_path)
    return {"path": path, "video_id": video_id, "clone": True,
            "duration": status.get("duration"), "avatar_id": avatar_id or os.getenv("HEYGEN_AVATAR_ID", "")}
