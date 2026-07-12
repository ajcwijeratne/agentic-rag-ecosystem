"""
File Uploads — text extraction, per-chat context, and KB indexing.

Two modes:
  • "chat" — extract text and hold it as context for the current session only.
             Injected into the system prompt of subsequent queries in that session.
  • "kb"   — chunk, embed, and upsert into a dedicated Qdrant collection
             ('uploaded_docs') so it is retrievable across all future queries.

Supported types: .pdf, .docx, .txt, .md  (others fall back to utf-8 text read).
"""

from __future__ import annotations

import io
import os
import uuid
from typing import Any

import httpx

QDRANT_URL:        str = os.getenv("QDRANT_URL", "http://localhost:6333")
UPLOADS_COLLECTION: str = os.getenv("UPLOADS_COLLECTION", "uploaded_docs")
VECTOR_DIM:        int = 768

# Per-session in-memory chat context: {session_id: [ {name, text}, ... ]}
_chat_context: dict[str, list[dict]] = {}


# ─────────────────────────────────────────────────────────────────────────────
# Text extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_text(filename: str, data: bytes) -> str:
    """Extract plain text from an uploaded file's bytes."""
    name = filename.lower()

    if name.endswith(".pdf"):
        return _extract_pdf(data)
    if name.endswith(".docx"):
        return _extract_docx(data)
    # txt, md, csv, json, code, etc.
    try:
        return data.decode("utf-8", errors="replace")
    except Exception:
        return data.decode("latin-1", errors="replace")


def _extract_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        return f"[Could not extract PDF text: {exc}]"


def _extract_docx(data: bytes) -> str:
    try:
        import docx
        document = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in document.paragraphs)
    except Exception as exc:
        return f"[Could not extract DOCX text: {exc}]"


# ─────────────────────────────────────────────────────────────────────────────
# Chat-mode context
# ─────────────────────────────────────────────────────────────────────────────

def add_chat_context(session_id: str, filename: str, text: str) -> None:
    _chat_context.setdefault(session_id, []).append({"name": filename, "text": text})


def get_chat_context(session_id: str) -> str:
    """Return uploaded chat-context for a session, formatted for the system prompt."""
    files = _chat_context.get(session_id, [])
    if not files:
        return ""
    blocks = ["[Files the user uploaded to this conversation:]"]
    for f in files:
        # Cap each file to keep prompt size reasonable
        blocks.append(f"\n### {f['name']}\n{f['text'][:6000]}")
    return "\n".join(blocks)


def clear_chat_context(session_id: str) -> int:
    n = len(_chat_context.get(session_id, []))
    _chat_context.pop(session_id, None)
    return n


def list_chat_context(session_id: str) -> list[str]:
    return [f["name"] for f in _chat_context.get(session_id, [])]


# ─────────────────────────────────────────────────────────────────────────────
# KB-mode indexing
# ─────────────────────────────────────────────────────────────────────────────

def _chunk(text: str, size: int = 400, overlap: int = 50) -> list[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i:i + size]))
        i += size - overlap
    return chunks


async def index_to_kb(filename: str, text: str) -> dict[str, Any]:
    """Chunk, embed, and upsert an uploaded document into Qdrant."""
    from datetime import datetime, timezone

    from rag.embedder import embed_batch

    # Ensure collection
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{QDRANT_URL}/collections/{UPLOADS_COLLECTION}", timeout=10.0)
        if resp.status_code != 200:
            resp = await client.put(
                f"{QDRANT_URL}/collections/{UPLOADS_COLLECTION}",
                json={"vectors": {"size": VECTOR_DIM, "distance": "Cosine"}},
                timeout=15.0,
            )
            resp.raise_for_status()

    # Section-aware chunking shares the indexer's logic; fall back to plain
    # word-window chunking if the indexer import is unavailable.
    try:
        from rag.indexer import chunk_with_sections
        pairs = chunk_with_sections(text)
    except Exception:
        pairs = [(c, "") for c in _chunk(text)]

    if not pairs:
        return {"status": "error", "message": "No extractable text"}

    uploaded_at = datetime.now(timezone.utc).isoformat()
    chunks    = [c for c, _ in pairs]
    metadata  = []
    for i, (chunk, section) in enumerate(pairs):
        metadata.append({
            "file":        filename,
            "text":        chunk,
            "section":     section,
            "modified_at": uploaded_at,
            "chunk_id":    str(uuid.uuid5(uuid.NAMESPACE_URL, f"upload::{filename}::{i}")),
            "collection":  UPLOADS_COLLECTION,
            "source":      "upload",
        })

    vectors = await embed_batch(chunks)
    points = [
        {"id": meta["chunk_id"], "vector": vec, "payload": meta}
        for vec, meta in zip(vectors, metadata)
    ]

    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{QDRANT_URL}/collections/{UPLOADS_COLLECTION}/points",
            json={"points": points},
            timeout=60.0,
        )
        resp.raise_for_status()

    # Update BM25 corpus for hybrid retrieval (uploads collection only)
    try:
        from rag.retriever import update_bm25_corpus
        update_bm25_corpus(chunks, metadata, collection=UPLOADS_COLLECTION)
    except Exception:
        pass

    return {
        "status":      "ok",
        "file":        filename,
        "chunks":      len(points),
        "collection":  UPLOADS_COLLECTION,
    }
