"""
Cross-encoder Reranker
======================
Uses cross-encoder/ms-marco-MiniLM-L-6-v2 (via sentence-transformers) to
rerank a list of retrieved chunks by relevance to the query.

Falls back to the original order if sentence-transformers is not installed
or the model fails to load.

Usage:
    from rag.reranker import rerank

    ranked = rerank(query="machine learning in education", chunks=[...], top_k=5)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_CROSS_ENCODER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_encoder = None
_encoder_loaded = False


def _load_encoder():
    global _encoder, _encoder_loaded
    if _encoder_loaded:
        return _encoder
    try:
        from sentence_transformers import CrossEncoder
        _encoder = CrossEncoder(_CROSS_ENCODER_MODEL)
        logger.info(f"[reranker] Loaded {_CROSS_ENCODER_MODEL}")
    except Exception as exc:
        logger.warning(f"[reranker] Could not load cross-encoder: {exc}. Falling back to original order.")
        _encoder = None
    _encoder_loaded = True
    return _encoder


def rerank(
    query:  str,
    chunks: list[dict[str, Any]],
    top_k:  int = 5,
) -> list[dict[str, Any]]:
    """
    Rerank chunks by cross-encoder score. Returns up to top_k best chunks.

    Each chunk dict must have a 'text' key.
    A '_rerank_score' key is added to each returned chunk.
    """
    if not chunks:
        return chunks

    encoder = _load_encoder()
    if encoder is None:
        return chunks[:top_k]

    texts = [c.get("text", "") for c in chunks]
    pairs = [(query, t) for t in texts]

    try:
        scores = encoder.predict(pairs, show_progress_bar=False)
    except Exception as exc:
        logger.error(f"[reranker] Prediction failed: {exc}")
        return chunks[:top_k]

    scored = sorted(zip(scores, chunks), key=lambda x: x[0], reverse=True)
    result = []
    for score, chunk in scored[:top_k]:
        chunk = dict(chunk)
        chunk["_rerank_score"] = float(score)
        result.append(chunk)

    return result
