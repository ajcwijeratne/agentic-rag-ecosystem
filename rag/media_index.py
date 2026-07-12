"""
Media Indexing
==============
Writes a media asset's extracted content into three Qdrant collections and
records the resulting point ids back onto the registry row:

  media_text         — OCR text, slide text/notes, cleaned web text  (768, nomic)
  media_transcripts  — transcript chunks carrying timestamps          (768, nomic)
  media_visual       — image and keyframe CLIP embeddings             (VISUAL_DIM)

`index_asset(asset_id)` reads the asset and its transcript from the registry and
indexes by type. It is best-effort: if Qdrant, Ollama, or the CLIP backend is
unavailable, it records what it can, returns a result dict noting the miss, and
never raises into the ingestion path. The asset stays usable in the registry and
can be re-indexed later.

Reuses the existing text embedder and BM25 corpus from the rag layer; only the
visual leg is new.
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import httpx

from media import registry
from .embedder import embed_batch, VECTOR_DIM
from . import visual_embedder

logger = logging.getLogger(__name__)

QDRANT_URL: str = os.getenv("QDRANT_URL", "http://localhost:6333")

MEDIA_TEXT_COLLECTION:        str = os.getenv("MEDIA_TEXT_COLLECTION", "media_text")
MEDIA_TRANSCRIPTS_COLLECTION: str = os.getenv("MEDIA_TRANSCRIPTS_COLLECTION", "media_transcripts")
MEDIA_VISUAL_COLLECTION:      str = os.getenv("MEDIA_VISUAL_COLLECTION", "media_visual")

CHUNK_WORDS:   int = int(os.getenv("MEDIA_CHUNK_WORDS", "180"))
CHUNK_OVERLAP: int = int(os.getenv("MEDIA_CHUNK_OVERLAP", "30"))


# --------------------------------------------------------------------------- #
# Qdrant helpers
# --------------------------------------------------------------------------- #

async def _ensure_collection(name: str, dim: int) -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{QDRANT_URL}/collections/{name}", timeout=10.0)
        if resp.status_code == 200:
            return
        resp = await client.put(
            f"{QDRANT_URL}/collections/{name}",
            json={"vectors": {"size": dim, "distance": "Cosine"}},
            timeout=15.0,
        )
        resp.raise_for_status()
        logger.info(f"[media_index] created collection '{name}' (dim {dim})")


async def ensure_media_collections() -> None:
    await _ensure_collection(MEDIA_TEXT_COLLECTION, VECTOR_DIM)
    await _ensure_collection(MEDIA_TRANSCRIPTS_COLLECTION, VECTOR_DIM)
    await _ensure_collection(MEDIA_VISUAL_COLLECTION, visual_embedder.VISUAL_DIM)


async def _upsert(collection: str, points: list[dict[str, Any]]) -> None:
    if not points:
        return
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{QDRANT_URL}/collections/{collection}/points",
            json={"points": points},
            timeout=60.0,
        )
        resp.raise_for_status()


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #

def chunk_text(text: str, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    words = text.split()
    out, i = [], 0
    while i < len(words):
        piece = " ".join(words[i : i + size])
        if piece.strip():
            out.append(piece)
        i += max(1, size - overlap)
    return out


def chunk_transcript(
    segments: list[dict],
    size: int = CHUNK_WORDS,
) -> list[dict]:
    """Group consecutive transcript segments into ~`size`-word chunks, carrying
    the start of the first segment, the end of the last, and the speaker when the
    whole chunk shares one. Returns [{text, t_start, t_end, speaker}, ...]."""
    chunks: list[dict] = []
    buf: list[str] = []
    words = 0
    t_start: float | None = None
    t_end: float | None = None
    speakers: set[str] = set()

    def flush():
        nonlocal buf, words, t_start, t_end, speakers
        if buf:
            chunks.append({
                "text":    " ".join(buf).strip(),
                "t_start": t_start,
                "t_end":   t_end,
                "speaker": next(iter(speakers)) if len(speakers) == 1 else "",
            })
        buf, words, t_start, t_end, speakers = [], 0, None, None, set()

    for seg in segments:
        txt = (seg.get("text") or "").strip()
        if not txt:
            continue
        if t_start is None:
            t_start = seg.get("start")
        t_end = seg.get("end")
        if seg.get("speaker"):
            speakers.add(seg["speaker"])
        buf.append(txt)
        words += len(txt.split())
        if words >= size:
            flush()
    flush()
    return [c for c in chunks if c["text"]]


# --------------------------------------------------------------------------- #
# Per-modality indexing
# --------------------------------------------------------------------------- #

async def _index_text(asset: dict, text: str) -> list[str]:
    """Embed plain text (OCR, slide, web) into media_text. Returns point ids."""
    pieces = chunk_text(text)
    if not pieces:
        return []
    aid = asset["asset_id"]
    payloads = []
    for j, piece in enumerate(pieces):
        payloads.append({
            "text":       piece,
            "asset_id":   aid,
            "media_type": asset["type"],
            "project":    asset.get("project") or "",
            "source":     "media",
            "collection": MEDIA_TEXT_COLLECTION,
            "chunk_id":   str(uuid.uuid5(uuid.NAMESPACE_URL, f"mediatext::{aid}::{j}")),
            "file":       asset.get("path") or "",
        })
    vectors = await embed_batch(pieces)
    points = [{"id": p["chunk_id"], "vector": v, "payload": p}
              for v, p in zip(vectors, payloads)]
    await _upsert(MEDIA_TEXT_COLLECTION, points)
    _append_bm25(MEDIA_TEXT_COLLECTION, pieces, payloads)
    return [p["chunk_id"] for p in payloads]


async def _index_transcript(asset: dict, segments: list[dict]) -> list[str]:
    """Embed transcript chunks (with timestamps) into media_transcripts."""
    chunks = chunk_transcript(segments)
    if not chunks:
        return []
    aid = asset["asset_id"]
    pieces = [c["text"] for c in chunks]
    payloads = []
    for j, c in enumerate(chunks):
        payloads.append({
            "text":       c["text"],
            "asset_id":   aid,
            "media_type": asset["type"],
            "t_start":    c["t_start"],
            "t_end":      c["t_end"],
            "speaker":    c["speaker"],
            "project":    asset.get("project") or "",
            "source":     "media",
            "collection": MEDIA_TRANSCRIPTS_COLLECTION,
            "chunk_id":   str(uuid.uuid5(uuid.NAMESPACE_URL, f"mediatx::{aid}::{j}")),
            "file":       asset.get("path") or "",
        })
    vectors = await embed_batch(pieces)
    points = [{"id": p["chunk_id"], "vector": v, "payload": p}
              for v, p in zip(vectors, payloads)]
    await _upsert(MEDIA_TRANSCRIPTS_COLLECTION, points)
    _append_bm25(MEDIA_TRANSCRIPTS_COLLECTION, pieces, payloads)
    return [p["chunk_id"] for p in payloads]


def _index_image_sync(asset: dict, thumbnail_path: str = "") -> tuple[list[str], list[dict]]:
    """Build the visual point for an image/keyframe. Sync because CLIP is sync.
    Returns (point_ids, points) or ([], []) when CLIP is unavailable."""
    vec = visual_embedder.embed_image(asset["path"])
    if vec is None:
        return [], []
    aid = asset["asset_id"]
    pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mediavis::{aid}"))
    payload = {
        "asset_id":       aid,
        "media_type":     asset["type"],
        "project":        asset.get("project") or "",
        "source":         "media",
        "collection":     MEDIA_VISUAL_COLLECTION,
        "thumbnail_path": thumbnail_path or asset["path"],
        "file":           asset["path"],
    }
    return [pid], [{"id": pid, "vector": vec, "payload": payload}]


def _append_bm25(collection: str, pieces: list[str], payloads: list[dict]) -> None:
    try:
        from .retriever import append_bm25_corpus
        append_bm25_corpus(pieces, payloads, collection)
    except Exception as exc:
        logger.warning(f"[media_index] BM25 append skipped for {collection}: {exc}")


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

async def index_asset(asset_id: str) -> dict[str, Any]:
    """Index one asset by type. Best-effort; records embedding ids on the registry.
    Returns {status, embedding_ids, notes}."""
    asset = registry.get_asset(asset_id)
    if not asset:
        return {"status": "error", "notes": "asset not found"}

    embedding_ids: dict[str, list[str]] = {}
    notes: list[str] = []

    try:
        await ensure_media_collections()
    except Exception as exc:
        return {"status": "error", "notes": f"qdrant unavailable: {exc}", "embedding_ids": {}}

    a_type = asset["type"]
    transcript = asset.get("transcript") or {}
    segments = transcript.get("segments") or []
    text = transcript.get("text") or ""

    try:
        if a_type in ("audio", "video") and segments:
            ids = await _index_transcript(asset, segments)
            if ids:
                embedding_ids[MEDIA_TRANSCRIPTS_COLLECTION] = ids
        if a_type == "image":
            pids, points = _index_image_sync(asset)
            if points:
                await _upsert(MEDIA_VISUAL_COLLECTION, points)
                embedding_ids[MEDIA_VISUAL_COLLECTION] = pids
            else:
                notes.append("visual embedding skipped (CLIP unavailable)")
            if text:  # OCR text from the image worker
                ids = await _index_text(asset, text)
                if ids:
                    embedding_ids[MEDIA_TEXT_COLLECTION] = ids
        if a_type in ("slide_deck", "web_page", "document") and text:
            ids = await _index_text(asset, text)
            if ids:
                embedding_ids[MEDIA_TEXT_COLLECTION] = ids
    except Exception as exc:
        logger.error(f"[media_index] indexing failed for {asset_id}: {exc}")
        notes.append(f"indexing error: {exc}")

    if embedding_ids:
        try:
            merged = dict(asset.get("embedding_ids") or {})
            merged.update(embedding_ids)
            registry.set_embeddings(asset_id, merged)
        except Exception as exc:
            notes.append(f"could not record embedding ids: {exc}")

    return {
        "status":        "ok" if embedding_ids else "noop",
        "embedding_ids": embedding_ids,
        "notes":         notes,
    }
