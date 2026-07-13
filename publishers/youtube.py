"""Official YouTube Data API v3 resumable uploader."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Iterator

import httpx


def _video_path(production: dict, options: dict[str, Any]) -> Path:
    from media import registry

    preferred = str(options.get("asset_id") or "")
    ids = [preferred] if preferred else list(production.get("linked_assets") or [])
    for asset_id in ids:
        asset = registry.get_asset(asset_id)
        if not asset:
            continue
        path = Path(str(asset.get("path") or ""))
        if asset.get("type") == "video" and path.is_file():
            return path
    raise ValueError("production has no readable linked video asset")


def _description(production: dict, options: dict[str, Any]) -> str:
    if options.get("description"):
        return str(options["description"])
    brief = production.get("brief") or {}
    if isinstance(brief, dict):
        for key in ("description", "summary", "purpose", "_raw"):
            if brief.get(key):
                return str(brief[key])
    return f"Published by WijerCo production {production.get('production_id')}"


def _access_token(client: httpx.Client) -> str:
    direct = os.getenv("YOUTUBE_ACCESS_TOKEN", "").strip()
    if direct:
        return direct
    values = {
        "client_id": os.getenv("YOUTUBE_CLIENT_ID", "").strip(),
        "client_secret": os.getenv("YOUTUBE_CLIENT_SECRET", "").strip(),
        "refresh_token": os.getenv("YOUTUBE_REFRESH_TOKEN", "").strip(),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise RuntimeError(f"YouTube OAuth is not configured: missing {', '.join(missing)}")
    response = client.post(
        "https://oauth2.googleapis.com/token",
        data={**values, "grant_type": "refresh_token"},
        timeout=30.0,
    )
    response.raise_for_status()
    token = str(response.json().get("access_token") or "")
    if not token:
        raise RuntimeError("YouTube OAuth refresh returned no access token")
    return token


def _chunks(path: Path, size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(size)
            if not chunk:
                break
            yield chunk


def _upload_sync(production: dict, options: dict[str, Any]) -> dict:
    path = _video_path(production, options)
    mime = str(options.get("mime_type") or "video/mp4")
    privacy = str(options.get("privacy_status") or os.getenv("YOUTUBE_PRIVACY_STATUS", "private"))
    if privacy not in {"private", "unlisted", "public"}:
        raise ValueError("privacy_status must be private, unlisted, or public")
    with httpx.Client(follow_redirects=True) as client:
        token = _access_token(client)
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(path.stat().st_size),
            "X-Upload-Content-Type": mime,
        }
        metadata = {
            "snippet": {
                "title": str(options.get("title") or production.get("title") or "WijerCo video")[:100],
                "description": _description(production, options)[:5000],
                "tags": list(options.get("tags") or []),
                "categoryId": str(options.get("category_id") or "27"),
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        start = client.post(
            "https://www.googleapis.com/upload/youtube/v3/videos",
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers=headers,
            json=metadata,
            timeout=30.0,
        )
        start.raise_for_status()
        upload_url = start.headers.get("location")
        if not upload_url:
            raise RuntimeError("YouTube resumable upload returned no Location header")
        upload = client.put(
            upload_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": mime},
            content=_chunks(path),
            timeout=600.0,
        )
        upload.raise_for_status()
        payload = upload.json()
    video_id = str(payload.get("id") or "")
    if not video_id:
        raise RuntimeError("YouTube upload completed without a video ID")
    return {
        "channel": "youtube",
        "status": "published",
        "external_id": video_id,
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "privacy_status": privacy,
        "asset_path": str(path),
    }


async def upload(production: dict, options: dict[str, Any] | None = None) -> dict:
    return await asyncio.to_thread(_upload_sync, production, options or {})
