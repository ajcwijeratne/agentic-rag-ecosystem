"""
Local Data Agent — FastMCP Server
==================================
Exposes Obsidian vault Markdown files as a retrieval endpoint.

Tools exposed via MCP:
  • retrieve(query, top_k)   — semantic search over Qdrant (pre-indexed vault)
  • list_notes()             — list all note filenames
  • read_note(filename)      — return full text of a single note
  • index_vault()            — trigger a full re-index of the vault

Also provides a plain REST endpoint POST /retrieve for the orchestrator's
rag_node (which uses httpx, not the MCP protocol directly).

Run:
  python -m agents.local_data_agent
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from fastmcp import FastMCP
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OBSIDIAN_VAULT: Path = Path(os.getenv("OBSIDIAN_VAULT_PATH", str(Path.home() / "ObsidianVault")))
QDRANT_URL: str = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION: str = os.getenv("QDRANT_COLLECTION", "obsidian_vault")
EMBED_URL: str = os.getenv("EMBED_URL", "http://localhost:11434/api/embeddings")
EMBED_MODEL: str = os.getenv("EMBED_MODEL", "nomic-embed-text")
PORT: int = int(os.getenv("LOCAL_DATA_AGENT_PORT", "8001"))

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="local-data-agent",
    instructions=(
        "Retrieves information from the user's local Obsidian vault. "
        "Use `retrieve` for semantic search, `list_notes` to browse available notes, "
        "and `read_note` to read a specific file."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_note(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _chunk_text(text: str, chunk_size: int = 400, overlap: int = 50) -> list[str]:
    """Split text into overlapping chunks by word count."""
    words = text.split()
    chunks = []
    i = 0
    while i < len(words):
        chunk = " ".join(words[i : i + chunk_size])
        chunks.append(chunk)
        i += chunk_size - overlap
    return chunks


async def _embed(text: str) -> list[float]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            EMBED_URL,
            json={"model": EMBED_MODEL, "prompt": text},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


async def _qdrant_search(query_vector: list[float], top_k: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points/search",
            json={
                "vector": query_vector,
                "limit": top_k,
                "with_payload": True,
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        return resp.json().get("result", [])


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def retrieve(query: str, top_k: int = 5) -> list[dict[str, Any]]:
    """
    Hybrid search over the Obsidian vault: BM25 + dense + RRF + cross-encoder
    rerank via rag.retriever.search(). Falls back to dense-only Qdrant search
    if the hybrid pipeline is unavailable (e.g. rank_bm25 or the reranker is
    not installed). Returns the top_k most relevant chunks with full provenance.
    """
    try:
        from rag.retriever import search as hybrid_search
        results = await hybrid_search(query, top_k=top_k, collection=QDRANT_COLLECTION)
        if results:
            for r in results:
                r.setdefault("source_agent", "local_data")
            return results
    except Exception as exc:
        # Fall through to the dense-only path below.
        import logging
        logging.getLogger(__name__).warning(f"[local-data-agent] hybrid search failed: {exc}")

    # Dense-only fallback.
    try:
        vector = await _embed(query)
        results = await _qdrant_search(vector, top_k)
        return [
            {
                "text":           r["payload"].get("text", ""),
                "file":           r["payload"].get("file", ""),
                "section":        r["payload"].get("section", ""),
                "modified_at":    r["payload"].get("modified_at", ""),
                "collection":     QDRANT_COLLECTION,
                "chunk_id":       str(r.get("id", "")),
                "score":          r.get("score", 0.0),
                "retrieval_mode": "dense",
                "source_agent":   "local_data",
            }
            for r in results
        ]
    except Exception as exc:
        return [{"error": str(exc), "text": "", "file": "", "score": 0.0}]


@mcp.tool()
def list_notes() -> list[str]:
    """List all Markdown note filenames in the Obsidian vault."""
    if not OBSIDIAN_VAULT.exists():
        return []
    return [str(p.relative_to(OBSIDIAN_VAULT)) for p in OBSIDIAN_VAULT.rglob("*.md")]


@mcp.tool()
def read_note(filename: str) -> str:
    """Return the full text of a single note by its relative path."""
    target = OBSIDIAN_VAULT / filename
    if not target.exists():
        return f"[Error] Note not found: {filename}"
    return _load_note(target)


@mcp.tool()
async def index_vault() -> dict[str, Any]:
    """
    Re-index all Markdown files in the vault into Qdrant.
    Calls the RAG indexer service.
    """
    indexer_url = os.getenv("INDEXER_URL", "http://localhost:8005")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{indexer_url}/index",
                json={"vault_path": str(OBSIDIAN_VAULT)},
                timeout=120.0,
            )
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


# ---------------------------------------------------------------------------
# REST wrapper (used by the LangGraph rag_node via httpx)
# ---------------------------------------------------------------------------

from fastapi import Depends
from fastapi.middleware.cors import CORSMiddleware
from common.security import require_api_key, cors_kwargs, bind_host
rest_app = FastAPI(title="Local Data Agent REST Bridge", dependencies=[Depends(require_api_key)])
rest_app.add_middleware(CORSMiddleware, **cors_kwargs())


class RetrieveRequest(BaseModel):
    query: str
    top_k: int = 5


@rest_app.post("/retrieve")
async def rest_retrieve(req: RetrieveRequest):
    chunks = await retrieve(req.query, req.top_k)
    return {"chunks": chunks}


@rest_app.get("/health")
def health():
    return {"status": "ok", "agent": "local-data-agent"}


@rest_app.get("/health/deep")
async def health_deep():
    from common.health import deep_health, qdrant_check, ollama_check
    return await deep_health([qdrant_check(), ollama_check()], service="local-data-agent")


@rest_app.on_event("startup")
async def _startup_checks():
    from common.health import qdrant_check, ollama_check
    from common.startup import require_dependencies
    await require_dependencies([qdrant_check(), ollama_check()], service="local-data-agent")


# ---------------------------------------------------------------------------
# Mount MCP on REST app and run
# ---------------------------------------------------------------------------

# Mount the MCP server under /mcp so both REST + MCP share one port.
# FastMCP renamed the ASGI factory across versions, so try each known name.
def _mcp_asgi(m):
    for name in ("http_app", "streamable_http_app", "sse_app", "get_asgi_app"):
        fn = getattr(m, name, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                continue
    return None

_mcp_app = _mcp_asgi(mcp)
if _mcp_app is not None:
    rest_app.mount("/mcp", _mcp_app)
else:
    print("[local-data-agent] MCP ASGI app unavailable in this FastMCP version; REST endpoint still active.")


if __name__ == "__main__":
    uvicorn.run("agents.local_data_agent:rest_app", host=bind_host(), port=PORT, reload=False)
