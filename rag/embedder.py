"""
Embedder — generates dense vectors via Ollama's nomic-embed-text model.

Used by the indexer (batch) and the retriever (single query).
"""

from __future__ import annotations

import os
from typing import Union

import httpx

EMBED_URL: str   = os.getenv("EMBED_URL", "http://localhost:11434/api/embeddings")
EMBED_MODEL: str = os.getenv("EMBED_MODEL", "nomic-embed-text")
VECTOR_DIM: int  = 768   # nomic-embed-text output dimension


async def embed_text(text: str) -> list[float]:
    """Return embedding vector for a single string."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            EMBED_URL,
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


def _batch_url() -> str:
    """Ollama's batch endpoint (/api/embed, list input) beside EMBED_URL."""
    url = EMBED_URL.rstrip("/")
    if url.endswith("/api/embeddings"):
        return url[: -len("/api/embeddings")] + "/api/embed"
    return url.rsplit("/api/", 1)[0] + "/api/embed" if "/api/" in url else url + "/api/embed"


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """
    Embed a list of texts in batches. Returns vectors in input order.

    Each batch is one call to Ollama's /api/embed. Until 21 Nov 2026 every
    text was its own HTTP request on a new connection, and a full vault took
    more than 15 minutes on a CPU-only machine. /api/embed returns unit-length
    vectors; the collections use cosine distance, so they rank the same as the
    single-text endpoint's. An Ollama without /api/embed falls back to one
    text per request.
    """
    vectors: list[list[float]] = []
    batch_ok = True
    url = _batch_url()
    async with httpx.AsyncClient() as client:
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            if batch_ok:
                try:
                    resp = await client.post(url, json={"model": EMBED_MODEL, "input": batch}, timeout=300.0)
                    resp.raise_for_status()
                    got = resp.json().get("embeddings") or []
                    if len(got) != len(batch):
                        raise ValueError(f"expected {len(batch)} embeddings, got {len(got)}")
                    vectors.extend(got)
                    print(f"[embedder] Embedded {min(i + batch_size, len(texts))}/{len(texts)}")
                    continue
                except Exception as exc:
                    print(f"[embedder] batch endpoint failed ({exc}); falling back to one text per call")
                    batch_ok = False
            for text in batch:
                vectors.append(await embed_text(text))
            print(f"[embedder] Embedded {min(i + batch_size, len(texts))}/{len(texts)}")
    return vectors
