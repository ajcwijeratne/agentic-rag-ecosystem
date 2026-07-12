"""
One-time voice clone setup against ElevenLabs.

Usage:
    python -m scripts.setup_voice_clone --name "Aaron" --i-consent \
        samples/clip1.mp3 samples/clip2.mp3 samples/clip3.mp3

Sample guidance: 3 to 5 clips, 1 to 5 minutes total, one speaker, no music,
quiet room, natural speaking pace. Record the way you actually talk on video,
not the way you read.

The --i-consent flag is mandatory. It records that the voice being cloned is
your own (or you hold written consent from its owner). The script refuses to
run without it and writes a consent record next to the samples.

On success it prints the voice_id and the .env line to add.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from media.providers import elevenlabs  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an ElevenLabs voice clone.")
    parser.add_argument("samples", nargs="+", help="Audio sample files (mp3/wav)")
    parser.add_argument("--name", default="Aaron", help="Voice name in ElevenLabs")
    parser.add_argument("--description", default="Aaron Wijeratne, consented self-clone")
    parser.add_argument(
        "--i-consent",
        action="store_true",
        help="Assert the cloned voice is your own or you hold written consent from its owner.",
    )
    args = parser.parse_args()

    if not args.i_consent:
        print("Refusing to clone without --i-consent. The voice being cloned must be "
              "your own, or you must hold written consent from its owner.")
        return 2

    if not elevenlabs.available():
        print("ELEVENLABS_API_KEY is not set in .env.")
        return 2

    paths = [Path(p) for p in args.samples]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        print(f"Sample files not found: {missing}")
        return 2

    total_mb = sum(p.stat().st_size for p in paths) / 1e6
    print(f"Uploading {len(paths)} sample(s), {total_mb:.1f} MB total, as voice {args.name!r}...")
    result = elevenlabs.create_clone(args.name, paths, description=args.description)
    voice_id = result.get("voice_id")
    if not voice_id:
        print(f"Clone failed: {json.dumps(result)[:500]}")
        return 1

    consent = {
        "voice_id": voice_id,
        "name": args.name,
        "samples": [str(p) for p in paths],
        "consent": "self, asserted via --i-consent",
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    record = paths[0].parent / f"voice-clone-consent-{voice_id}.json"
    record.write_text(json.dumps(consent, indent=2), encoding="utf-8")

    print(f"\nDone. voice_id: {voice_id}")
    print(f"Consent record: {record}")
    print("\nAdd to .env:")
    print(f"  ELEVENLABS_VOICE_ID={voice_id}")
    print("  MEDIA_TOOL_DEFAULT_VOICE=elevenlabs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
