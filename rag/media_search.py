"""
Media Search
============
Answers the three retrieval intents from the expansion plan:

  "find the clip where I discuss X"   -> transcript search, returns timestamps
  "locate diagrams about Y"           -> visual search (text query -> image space)
  "reuse approved visual assets for Z"-> registry filter (rights/status/project)
                                         then visual similarity within that set

Text and transcript legs reuse the existing hybrid retriever (dense + BM25 + RRF
+ rerank) by passing the media collection name. The visual leg is new: it embeds
the query with CLIP and searches the media_visual collection. Every hit is joined
back to the registry so the caller gets the asset's rights, status, and project,
not just a vector match.

Filters (project, rights, status, type) are applied against the registry after
retrieval, so governance — not only relevance — scopes what comes back.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from media import registry
from . import visual_embedder
from .retriever import search as text_search
from .schema import Chunk
from .media_index import (
    MEDIA_TEXT_COLLECTION,
    MEDIA_TRANSCRIPTS_COLLECTION,
    MEDIA_VISUAL_COLLECTION,
)

logger = logging.getLogger(__name__)

QDRANT_URL: str = os.getenv("QDRANT_URL", "http://localhost:6333")

MODALITIES = ("text", "transcript", "visual")


# --------------------------------------------------------------------------- #
# Visual leg
# --------------------------------------------------------------------------- #

async def _visual_search(query: str, top_k: int) -> list[dict]:
    vec = visual_embedder.embed_text(query)
    if vec is None:
        return []
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{QDRANT_URL}/collections/{MEDIA_VISUAL_COLLECTION}/points/search",
            json={"vector": vec, "limit": top_k, "with_payload": True},
            timeout=15.0,
        )
        resp.raise_for_status()
        results = resp.json().get("result", [])
    return [
        Chunk.from_qdrant_payload(
            r.get("payload", {}),
            score=r.get("score", 0.0),
            collection=MEDIA_VISUAL_COLLECTION,
            chunk_id=str(r.get("id", "")),
            retrieval_mode="dense",
        ).to_dict()
        for r in results
    ]


# --------------------------------------------------------------------------- #
# Registry join + filter
# --------------------------------------------------------------------------- #

def _passes(asset: dict | None, filters: dict[str, Any]) -> bool:
    if asset is None:
        return False
    for key in ("project", "rights", "status", "type"):
        want = filters.get(key)
        if want and asset.get(key) != want:
            return False
    return True


def _attach_and_filter(hits: list[dict], filters: dict[str, Any]) -> list[dict]:
    """Join each hit to its registry asset, drop hits that fail the filters or
    whose asset is gone, and attach a compact asset summary."""
    out = []
    for h in hits:
        aid = h.get("asset_id")
        asset = registry.get_asset(aid, with_relations=False) if aid else None
        if filters and not _passes(asset, filters):
            continue
        if asset is not None:
            h["asset"] = {
                "asset_id": asset["asset_id"],
                "type":     asset["type"],
                "path":     asset["path"],
                "project":  asset.get("project"),
                "rights":   asset.get("rights"),
                "status":   asset.get("status"),
            }
        out.append(h)
    return out


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

async def media_search(
    query: str,
    *,
    modalities: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """Search the media indexes. Returns results grouped by modality, each hit
    joined to its registry asset and filtered by it."""
    modalities = [m for m in (modalities or list(MODALITIES)) if m in MODALITIES]
    filters = filters or {}
    results: dict[str, list[dict]] = {}

    if "text" in modalities:
        try:
            hits = await text_search(query, top_k=top_k, collection=MEDIA_TEXT_COLLECTION)
            results["text"] = _attach_and_filter(hits, filters)
        except Exception as exc:
            logger.warning(f"[media_search] text leg failed: {exc}")
            results["text"] = []

    if "transcript" in modalities:
        try:
            hits = await text_search(query, top_k=top_k, collection=MEDIA_TRANSCRIPTS_COLLECTION)
            results["transcript"] = _attach_and_filter(hits, filters)
        except Exception as exc:
            logger.warning(f"[media_search] transcript leg failed: {exc}")
            results["transcript"] = []

    if "visual" in modalities:
        try:
            # Pull extra so registry filtering still leaves a useful set.
            hits = await _visual_search(query, top_k=top_k * 3 if filters else top_k)
            results["visual"] = _attach_and_filter(hits, filters)[:top_k]
        except Exception as exc:
            logger.warning(f"[media_search] visual leg failed: {exc}")
            results["visual"] = []

    return {
        "query":       query,
        "modalities":  modalities,
        "filters":     filters,
        "visual_ready": visual_embedder.available(),
        "results":     results,
    }
