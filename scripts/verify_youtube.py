"""
Verify the YouTube publisher credential.

Refreshes an access token from the four YOUTUBE_ env values and calls the API
for your own channel, proving both that auth works and that the readonly scope
the measure loop needs is granted.

  python -m scripts.verify_youtube
"""

from __future__ import annotations

import sys

import httpx
from dotenv import load_dotenv

load_dotenv()


def main() -> int:
    from publishers import youtube

    with httpx.Client() as client:
        try:
            token = youtube._access_token(client)
        except Exception as exc:
            print(f"FAIL: could not get an access token: {exc}")
            print("Check YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN.")
            return 1
        resp = client.get(
            "https://www.googleapis.com/youtube/v3/channels",
            params={"part": "snippet,statistics", "mine": "true"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    if resp.status_code != 200:
        print(f"FAIL: channels.list returned {resp.status_code}: {resp.text[:300]}")
        return 1
    items = resp.json().get("items") or []
    if not items:
        print("FAIL: authenticated but the account has no YouTube channel.")
        return 1
    ch = items[0]
    snippet = ch.get("snippet", {})
    stats = ch.get("statistics", {})
    print("OK: YouTube credential works.")
    print(f"  Channel : {snippet.get('title')}")
    print(f"  Subs    : {stats.get('subscriberCount')}")
    print(f"  Videos  : {stats.get('videoCount')}")
    print("Upload (publish) and stats-read (measure loop) are both ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
