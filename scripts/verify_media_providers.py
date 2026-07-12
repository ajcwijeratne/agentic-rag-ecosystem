"""
Verify the multimedia setup end to end, without rendering anything.

    python -m scripts.verify_media_providers

The pipeline is ready when each capability has at least one working path:

  voice   : free F5-TTS worker (F5_TTS_URL)            [preferred]
            or ElevenLabs (ELEVENLABS_API_KEY + VOICE_ID)
  avatar  : free SadTalker worker (MEDIA_TOOL_SADTALKER_ENDPOINT)
            or MuseTalk (MEDIA_TOOL_MUSETALK_ENDPOINT)  [preferred]
            or HeyGen (HEYGEN_API_KEY + AVATAR_ID)
  assembly: ffmpeg on PATH, Remotion project present

Cloud keys are optional; their absence is a warn, not a fail, as long as the
free worker for that capability answers its /health endpoint.

Exit 0 when voice + avatar + assembly each have a working path.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

OK, WARN, FAIL = "ok", "warn", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    pad = " " * max(1, 36 - len(name))
    print(f"  {name}{pad}{status.upper():5}  {detail}")


def _worker_health(url: str) -> tuple[bool, str]:
    try:
        resp = httpx.get(f"{url.rstrip('/')}/health", timeout=8)
        resp.raise_for_status()
        data = resp.json()
        return data.get("status") == "ok", str(data)[:140]
    except Exception as exc:
        return False, str(exc)[:120]


def main() -> int:
    print("Multimedia setup verification\n")
    voice_ok = False
    avatar_ok = False

    # ---- voice: free worker first ----
    f5_url = os.getenv("F5_TTS_URL", "")
    if f5_url:
        healthy, detail = _worker_health(f5_url)
        check("F5-TTS worker", OK if healthy else FAIL, detail)
        voice_ok = voice_ok or healthy
    else:
        check("F5_TTS_URL", WARN, "not set; run deploy/gpu-worker-setup.ps1 on the GPU PC")

    from media.providers import elevenlabs
    if elevenlabs.available():
        try:
            voices = elevenlabs.list_voices()
            want = os.getenv("ELEVENLABS_VOICE_ID", "")
            found = any(v.get("voice_id") == want for v in voices)
            check("ElevenLabs (optional)", OK if want and found else WARN,
                  "voice found" if found else "key works; voice_id missing")
            voice_ok = voice_ok or bool(want and found)
        except Exception as exc:
            check("ElevenLabs (optional)", WARN, str(exc)[:100])
    else:
        check("ElevenLabs (optional)", WARN, "no key (fine; free path preferred)")

    # ---- avatar: free workers first ----
    for env, label in (("MEDIA_TOOL_SADTALKER_ENDPOINT", "SadTalker worker"),
                       ("MEDIA_TOOL_MUSETALK_ENDPOINT", "MuseTalk worker")):
        url = os.getenv(env, "")
        if url:
            healthy, detail = _worker_health(url)
            check(label, OK if healthy else FAIL, detail)
            avatar_ok = avatar_ok or healthy
        else:
            check(env, WARN, "not set")

    from media.providers import heygen
    if heygen.available():
        check("HeyGen (optional)", OK if os.getenv("HEYGEN_AVATAR_ID") else WARN,
              "key + avatar set" if os.getenv("HEYGEN_AVATAR_ID") else "key works; avatar_id missing")
        avatar_ok = avatar_ok or bool(os.getenv("HEYGEN_AVATAR_ID"))
    else:
        check("HeyGen (optional)", WARN, "no key (fine; free path preferred)")

    # ---- registry defaults ----
    from media import tool_registry
    for cap, prefer in (("voice", ("f5-tts", "elevenlabs")), ("avatar", ("sadtalker", "musetalk", "heygen"))):
        default = tool_registry.default_tool_for(cap)
        dname = default["name"] if default else "none"
        good = dname in prefer and bool(default and default.get("available"))
        check(f"default {cap} tool", OK if good else WARN,
              dname if good else f"{dname}; set MEDIA_TOOL_DEFAULT_{cap.upper()} "
                                 f"to a configured tool ({', '.join(prefer)})")

    # ---- assembly ----
    ff = bool(shutil.which("ffmpeg"))
    check("ffmpeg", OK if ff else FAIL, "" if ff else "install ffmpeg")
    remotion = Path(__file__).resolve().parent.parent / "my-video" / "package.json"
    check("Remotion project", OK if remotion.is_file() else WARN,
          "" if remotion.is_file() else "my-video/ missing")

    # ---- verdict ----
    check("voice path", OK if voice_ok else FAIL,
          "" if voice_ok else "no working voice engine (free worker down and no cloud fallback)")
    check("avatar path", OK if avatar_ok else FAIL,
          "" if avatar_ok else "no working avatar engine (free worker down and no cloud fallback)")

    fails = [r for r in results if r[1] == FAIL]
    ready = voice_ok and avatar_ok and ff
    print(f"\n{'READY' if ready else 'NOT READY'}: "
          f"{len([r for r in results if r[1] == OK])} ok, "
          f"{len([r for r in results if r[1] == WARN])} warn, {len(fails)} fail")
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
