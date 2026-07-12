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


async def embed_batch(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """
    Embed a list of texts, processing in batches to avoid overloading Ollama.
    Returns a list of vectors in the same order as the input.
    """
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        for text in batch:
            vec = await embed_text(text)
            vectors.append(vec)
        print(f"[embedder] Embedded {min(i + batch_size, len(texts))}/{len(texts)}")
    return vectors
