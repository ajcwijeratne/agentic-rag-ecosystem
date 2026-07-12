"""
Multimodal Ingestion
====================
Detects an asset's type, creates its registry row, runs the right worker, and
flips the row to `ready` or `failed`. Workers write transcripts and embeddings,
then record point ids back on the asset.

Workers: audio, image, video, slide_deck, web_page, document. A video worker may
create child image assets (keyframes); the dispatcher indexes those too. Heavy
worker dependencies (faster-whisper, ffmpeg, Pillow, pytesseract, python-pptx,
Playwright) are imported lazily inside each worker, so importing this package
stays cheap and each missing dependency degrades that worker gracefully.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from common.security import confine_to_roots
from .. import registry

AUTOINDEX = os.getenv("MEDIA_AUTOINDEX", "1").lower() not in ("0", "false", "no")

INPUT_ROOT   = Path(os.getenv("MEDIA_INPUT_ROOT", "./media_input"))
DERIVED_ROOT = Path(os.getenv("MEDIA_DERIVED_ROOT", "./media_derived"))
_ROOTS = [INPUT_ROOT, DERIVED_ROOT]

_EXT_TYPE: dict[str, str] = {
    # audio
    ".mp3": "audio", ".wav": "audio", ".m4a": "audio", ".flac": "audio",
    ".aac": "audio", ".ogg": "audio", ".opus": "audio",
    # video
    ".mp4": "video", ".mov": "video", ".mkv": "video", ".webm": "video",
    ".avi": "video", ".m4v": "video",
    # image
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".bmp": "image", ".tiff": "image", ".tif": "image",
    # slides
    ".pptx": "slide_deck", ".key": "slide_deck",
    # documents
    ".pdf": "document", ".docx": "document", ".txt": "document", ".md": "document",
}


def detect_type(path_or_url: str) -> str:
    """Return the asset type for a path or URL."""
    if path_or_url.lower().startswith(("http://", "https://")):
        return "web_page"
    return _EXT_TYPE.get(Path(path_or_url).suffix.lower(), "document")


def _autoindex(asset_id: str) -> dict[str, Any] | None:
    """Embed and index a freshly-ingested asset. Best-effort: a missing Qdrant,
    Ollama, or CLIP backend leaves the asset usable in the registry and simply
    skips indexing. Runs the async indexer from this sync worker via asyncio.run,
    which is safe inside a FastAPI BackgroundTask (sync tasks run in a thread)."""
    if not AUTOINDEX:
        return None
    try:
        from rag.media_index import index_asset
        return asyncio.run(index_asset(asset_id))
    except Exception as exc:                       # never break ingestion
        return {"status": "error", "notes": f"autoindex skipped: {exc}"}


def ingest(
    path_or_url: str,
    *,
    project: str | None = None,
    rights:  str = "unknown",
    source:  str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """
    Ingest one asset end to end. Creates the registry row, runs the worker,
    indexes the result (and any child assets), and returns the final record.
    Safe to call from a BackgroundTask.
    """
    asset_type = detect_type(path_or_url)
    is_url = asset_type == "web_page"

    # Confine local paths to the media roots, the same guard the whisper and
    # video services apply. URLs skip this check.
    if not is_url:
        try:
            confined = confine_to_roots(path_or_url, _ROOTS)
            path_or_url = str(confined)
        except Exception as exc:
            return {"status": "rejected", "reason": str(exc)}

    aid = registry.add_asset(
        asset_type,
        path=path_or_url,
        source=source or ("web" if is_url else "upload"),
        rights=rights,
        status="ingesting",
        project=project,
    )

    if asset_type == "audio":
        from .audio import enrich as _enrich
        result = _enrich(aid, path_or_url, language=language)
    elif asset_type == "image":
        from .images import enrich as _enrich
        result = _enrich(aid, path_or_url)
    elif asset_type == "video":
        from .video import enrich as _enrich
        result = _enrich(aid, path_or_url, language=language)
    elif asset_type == "slide_deck":
        from .slides import enrich as _enrich
        result = _enrich(aid, path_or_url)
    elif asset_type == "web_page":
        from .web import enrich as _enrich
        result = _enrich(aid, path_or_url)
    elif asset_type == "document":
        from .docs import enrich as _enrich
        result = _enrich(aid, path_or_url)
    else:
        result = {"status": "deferred", "detail": f"no worker for {asset_type}"}

    index_result = None
    if result.get("status") == "ready":
        index_result = _autoindex(aid)
        # Index any child assets the worker created (e.g. video keyframes).
        for child_id in result.get("children", []) or []:
            _autoindex(child_id)

    asset = registry.get_asset(aid)
    return {"asset_id": aid, "worker": result, "index": index_result, "asset": asset}
