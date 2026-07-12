"""
Retriever — hybrid BM25 + dense vector search over Qdrant, with cross-encoder reranking.

Search strategy:
  1. Dense search  — nomic-embed-text cosine similarity via Qdrant
  2. BM25 search   — rank_bm25 over the indexed corpus (in-memory, rebuilt on demand)
  3. RRF merge     — Reciprocal Rank Fusion combines both ranked lists
  4. Rerank        — cross-encoder/ms-marco-MiniLM-L-6-v2 for final ordering

Falls back gracefully: if BM25 corpus is empty or rank_bm25 is not installed,
dense-only search is used. If reranker is unavailable, RRF order is kept.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from .embedder import embed_text
from .reranker import rerank
from .schema import Chunk

logger = logging.getLogger(__name__)

QDRANT_URL:        str  = os.getenv("QDRANT_URL",        "http://localhost:6333")
QDRANT_COLLECTION: str  = os.getenv("QDRANT_COLLECTION", "obsidian_vault")
PORT:              int  = int(os.getenv("RETRIEVER_PORT", "8006"))
USE_RERANKER_DEFAULT: bool = os.getenv("RETRIEVER_USE_RERANKER", "1").lower() not in ("0", "false", "no")
KB_MISS_LOG:   Path  = Path(os.getenv("KB_MISS_LOG", str(Path(__file__).resolve().parent.parent / "data" / "kb_misses.jsonl")))
KB_MISS_SCORE: float = float(os.getenv("KB_MISS_SCORE", "0.45"))


def _log_miss(query: str, collection: str, dense_top: float, dense_hits: int, sparse_hits: int) -> None:
    """Record a query the corpus could not answer well. Surfaced by the
    Command Centre at GET /kb/misses. Best-effort: never fails a search."""
    try:
        KB_MISS_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "query": query[:300],
            "collection": collection,
            "dense_top": round(dense_top, 3),
            "dense_hits": dense_hits,
            "sparse_hits": sparse_hits,
        }
        with KB_MISS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass

# In-memory BM25 corpus, keyed by collection so the vault, wijerco_knowledge,
# and uploaded_docs corpora do not clobber each other.
_bm25_corpus:   dict[str, list[str]]  = {}
_bm25_payloads: dict[str, list[dict]] = {}
_bm25_model:    dict[str, Any]        = {}


def update_bm25_corpus(
    texts: list[str],
    payloads: list[dict],
    collection: str = QDRANT_COLLECTION,
) -> None:
    """Called by the indexer/uploads after each index run to refresh one
    collection's BM25 corpus. Other collections are left untouched."""
    _bm25_corpus[collection]   = texts
    _bm25_payloads[collection] = payloads
    _bm25_model.pop(collection, None)   # reset so it's rebuilt on next search
    logger.info(f"[retriever] BM25 corpus updated for '{collection}': {len(texts)} documents")


def append_bm25_corpus(
    texts: list[str],
    payloads: list[dict],
    collection: str,
) -> None:
    """Add documents to a collection's BM25 corpus without clobbering existing
    ones. Used by media indexing, where assets arrive one at a time rather than
    in a single whole-corpus rebuild like the vault indexer does."""
    _bm25_corpus.setdefault(collection, []).extend(texts)
    _bm25_payloads.setdefault(collection, []).extend(payloads)
    _bm25_model.pop(collection, None)   # reset so it's rebuilt on next search
    logger.info(f"[retriever] BM25 corpus appended for '{collection}': +{len(texts)} documents")


def _get_bm25(collection: str):
    if collection in _bm25_model:
        return _bm25_model[collection]
    texts = _bm25_corpus.get(collection, [])
    if not texts:
        _bm25_model[collection] = None
        return None
    try:
        from rank_bm25 import BM25Okapi
        tokenized = [t.lower().split() for t in texts]
        _bm25_model[collection] = BM25Okapi(tokenized)
    except Exception as exc:
        logger.warning(f"[retriever] rank_bm25 unavailable: {exc}")
        _bm25_model[collection] = None
    return _bm25_model[collection]


# -----------------------------------------------------------------------------
# Reciprocal Rank Fusion
# -----------------------------------------------------------------------------

def _rrf_merge(
    dense_results: list[dict],
    sparse_results: list[dict],
    k: int = 60,
) -> list[dict]:
    """
    Merge two ranked lists using Reciprocal Rank Fusion.
    Returns merged list sorted by descending RRF score.
    """
    scores: dict[str, float] = {}
    items:  dict[str, dict]  = {}

    def key(chunk: dict) -> str:
        return f"{chunk.get('file','')}__{chunk.get('text','')[:80]}"

    for rank, chunk in enumerate(dense_results):
        k_ = key(chunk)
        scores[k_]  = scores.get(k_, 0.0) + 1.0 / (k + rank + 1)
        items[k_]   = chunk

    for rank, chunk in enumerate(sparse_results):
        k_ = key(chunk)
        scores[k_]  = scores.get(k_, 0.0) + 1.0 / (k + rank + 1)
        if k_ not in items:
            items[k_] = chunk

    merged = sorted(items.keys(), key=lambda k_: scores[k_], reverse=True)
    result = []
    for k_ in merged:
        c = dict(items[k_])
        c["_rrf_score"]     = round(scores[k_], 6)
        c["rrf_score"]      = round(scores[k_], 6)
        c["retrieval_mode"] = "rrf"
        result.append(c)
    return result


# -----------------------------------------------------------------------------
# Dense search
# -----------------------------------------------------------------------------

async def _dense_search(
    query: str,
    top_k: int,
    score_threshold: float,
    collection: str = QDRANT_COLLECTION,
) -> list[dict]:
    vector = await embed_text(query)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{QDRANT_URL}/collections/{collection}/points/search",
            json={
                "vector":          vector,
                "limit":           top_k,
                "with_payload":    True,
                "score_threshold": score_threshold,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        results = resp.json().get("result", [])

    return [
        Chunk.from_qdrant_payload(
            r.get("payload", {}),
            score=r.get("score", 0.0),
            collection=collection,
            chunk_id=str(r.get("id", "")),
            retrieval_mode="dense",
        ).to_dict()
        for r in results
    ]


# -----------------------------------------------------------------------------
# BM25 sparse search
# -----------------------------------------------------------------------------

def _bm25_search(query: str, top_k: int, collection: str = QDRANT_COLLECTION) -> list[dict]:
    corpus = _bm25_corpus.get(collection, [])
    if not corpus:
        return []
    bm25 = _get_bm25(collection)
    if bm25 is None:
        return []

    payloads        = _bm25_payloads.get(collection, [])
    tokenized_query = query.lower().split()
    scores          = bm25.get_scores(tokenized_query)
    top_indices     = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_k]

    results = []
    for idx in top_indices:
        if scores[idx] <= 0:
            continue
        payload = dict(payloads[idx]) if idx < len(payloads) else {}
        payload.setdefault("text", corpus[idx])
        results.append(
            Chunk.from_qdrant_payload(
                payload,
                score=float(scores[idx]),
                collection=collection,
                chunk_id=payload.get("chunk_id", ""),
                retrieval_mode="bm25",
            ).to_dict()
        )
    return results


# -----------------------------------------------------------------------------
# Public hybrid search
# -----------------------------------------------------------------------------

async def search(
    query:           str,
    top_k:           int   = 5,
    score_threshold: float = 0.3,
    use_reranker:    bool  = USE_RERANKER_DEFAULT,
    collection:      str   = QDRANT_COLLECTION,
) -> list[dict[str, Any]]:
    """
    Full hybrid search pipeline over one collection:
      dense -> BM25 -> RRF merge -> cross-encoder rerank -> top_k

    Falls back gracefully: dense-only when BM25 corpus is empty, RRF order when
    the reranker is unavailable. Every returned chunk carries provenance
    (collection, file, section, modified_at, chunk_id, score, retrieval_mode).
    """
    dense_task     = _dense_search(query, top_k * 2, score_threshold, collection)
    sparse_results = _bm25_search(query, top_k * 2, collection)    # sync, fast

    try:
        dense_results = await dense_task
    except Exception as exc:
        logger.error(f"[retriever] Dense search failed: {exc}")
        dense_results = []

    # Miss logging: nothing semantically close in the corpus for this query.
    dense_top = max((c.get("score", 0.0) or 0.0) for c in dense_results) if dense_results else 0.0
    if query.strip() and (not dense_results or dense_top < KB_MISS_SCORE):
        _log_miss(query, collection, dense_top, len(dense_results), len(sparse_results))

    # RRF merge
    if sparse_results:
        merged = _rrf_merge(dense_results, sparse_results)
    else:
        merged = dense_results

    # Cross-encoder rerank — promotes retrieval_mode to "rerank" on survivors.
    if use_reranker and merged:
        ranked = rerank(query=query, chunks=merged, top_k=top_k)
        for c in ranked:
            if "_rerank_score" in c:
                c["rerank_score"] = c["_rerank_score"]
            c["retrieval_mode"] = "rerank"
        merged = ranked
    else:
        merged = merged[:top_k]

    return merged


# -----------------------------------------------------------------------------
# FastAPI service
# -----------------------------------------------------------------------------

from fastapi import Depends
from fastapi.middleware.cors import CORSMiddleware
from common.security import require_api_key, cors_kwargs, bind_host
app = FastAPI(title="Qdrant Retriever Service", dependencies=[Depends(require_api_key)])
app.add_middleware(CORSMiddleware, **cors_kwargs())


class SearchRequest(BaseModel):
    query:           str   = ""
    top_k:           int   = 5
    score_threshold: float = 0.3
    use_reranker:    bool  = USE_RERANKER_DEFAULT
    collection:      str   = QDRANT_COLLECTION


@app.post("/search")
async def search_endpoint(req: SearchRequest):
    results = await search(
        req.query, req.top_k, req.score_threshold, req.use_reranker, req.collection
    )
    return {"results": results, "count": len(results), "collection": req.collection}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "retriever",
        "bm25_collections": {c: len(t) for c, t in _bm25_corpus.items()},
    }


@app.get("/health/deep")
async def health_deep():
    from common.health import deep_health, qdrant_check
    return await deep_health([qdrant_check()], service="retriever")


@app.on_event("startup")
async def _startup_checks():
    from common.health import qdrant_check
    from common.startup import require_dependencies
    await require_dependencies([qdrant_check()], service="retriever")


if __name__ == "__main__":
    uvicorn.run("rag.retriever:app", host=bind_host(), port=PORT, reload=False)
