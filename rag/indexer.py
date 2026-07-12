"""
Vault Indexer — reads all Markdown files from the Obsidian vault,
chunks them, embeds them, and upserts into Qdrant.

Also exposes a FastAPI service on port 8005 so the LocalDataAgent
can trigger re-indexing remotely.

Usage:
  # One-off index
  python -m rag.indexer --vault ~/ObsidianVault

  # As a service
  python -m rag.indexer --serve
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

from .embedder import embed_batch

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QDRANT_URL: str        = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION: str = os.getenv("QDRANT_COLLECTION", "obsidian_vault")
VAULT_PATH: Path       = Path(os.getenv("OBSIDIAN_VAULT_PATH", str(Path.home() / "ObsidianVault")))
WIJERCO_PATH: Path     = Path(os.getenv("WIJERCO_PATH", r"C:\Users\ajwij\Claude Cowork\WijerCo"))
WIJERCO_COLLECTION: str = os.getenv("WIJERCO_COLLECTION", "wijerco_knowledge")
CHUNK_SIZE: int        = int(os.getenv("CHUNK_SIZE", "400"))   # words
CHUNK_OVERLAP: int     = int(os.getenv("CHUNK_OVERLAP", "50")) # words
PORT: int              = int(os.getenv("INDEXER_PORT", "8005"))
VECTOR_DIM: int        = 768
KB_RUNS_PATH: Path     = Path(os.getenv("KB_RUNS_PATH", str(Path(__file__).resolve().parent.parent / "data" / "kb_index_runs.jsonl")))


def _record_run(collection: str, docs: int, chunks: int, duration_s: float) -> None:
    """Append one index-run record so the Command Centre reports real
    last-indexed times. Best-effort: an unwritable path never fails a run."""
    try:
        KB_RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "collection": collection,
            "docs": docs,
            "chunks": chunks,
            "duration_s": round(duration_s, 1),
        }
        with KB_RUNS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception as exc:
        print(f"[indexer] Could not record run: {exc}")


# ---------------------------------------------------------------------------
# Qdrant helpers
# ---------------------------------------------------------------------------

async def ensure_collection() -> None:
    """Create the Qdrant collection if it doesn't already exist."""
    async with httpx.AsyncClient() as client:
        # Check if collection exists
        resp = await client.get(f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}", timeout=10.0)
        if resp.status_code == 200:
            print(f"[indexer] Collection '{QDRANT_COLLECTION}' already exists.")
            return

        # Create it
        resp = await client.put(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}",
            json={
                "vectors": {
                    "size": VECTOR_DIM,
                    "distance": "Cosine",
                }
            },
            timeout=15.0,
        )
        resp.raise_for_status()
        print(f"[indexer] Created collection '{QDRANT_COLLECTION}'.")


try:
    from common.retry import async_retry
except Exception:  # pragma: no cover
    def async_retry(*a, **k):
        def _wrap(fn):
            return fn
        return _wrap


@async_retry()
async def upsert_points(points: list[dict[str, Any]]) -> None:
    """Batch upsert a list of Qdrant point dicts. Retries on transient errors."""
    async with httpx.AsyncClient() as client:
        resp = await client.put(
            f"{QDRANT_URL}/collections/{QDRANT_COLLECTION}/points",
            json={"points": points},
            timeout=60.0,
        )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    words = text.split()
    chunks, i = [], 0
    while i < len(words):
        chunks.append(" ".join(words[i : i + chunk_size]))
        i += chunk_size - overlap
    return chunks


_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*\S)\s*$")


def chunk_with_sections(
    text: str,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[tuple[str, str]]:
    """Chunk Markdown into (chunk_text, section_heading) pairs.

    Splits on Markdown headings so each chunk carries the nearest preceding
    heading as its section. Text before the first heading gets an empty section.
    """
    blocks: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            if current_lines:
                blocks.append((current_heading, current_lines))
            current_heading = m.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        blocks.append((current_heading, current_lines))

    pairs: list[tuple[str, str]] = []
    for heading, lines in blocks:
        words = " ".join(lines).split()
        i = 0
        while i < len(words):
            chunk = " ".join(words[i : i + chunk_size])
            if chunk.strip():
                pairs.append((chunk, heading))
            i += chunk_size - overlap
    return pairs


def _mtime_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    except Exception:
        return ""


def load_vault(vault: Path) -> list[tuple[str, str, str]]:
    """Returns list of (relative_path, text, modified_at_iso) for all .md files."""
    results = []
    for md_file in vault.rglob("*.md"):
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
            rel = str(md_file.relative_to(vault))
            results.append((rel, text, _mtime_iso(md_file)))
        except Exception as exc:
            print(f"[indexer] Could not read {md_file}: {exc}")
    return results


# ---------------------------------------------------------------------------
# Main indexing routine
# ---------------------------------------------------------------------------

async def index_vault(vault: Path = VAULT_PATH) -> dict[str, Any]:
    t0 = time.monotonic()
    await ensure_collection()

    notes = load_vault(vault)
    if not notes:
        return {"status": "error", "message": f"No .md files found in {vault}"}

    all_chunks: list[str]             = []
    all_metadata: list[dict[str, Any]] = []

    for rel_path, text, modified_at in notes:
        for j, (chunk, section) in enumerate(chunk_with_sections(text)):
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{rel_path}::{j}"))
            all_chunks.append(chunk)
            all_metadata.append({
                "file":        rel_path,
                "text":        chunk,
                "section":     section,
                "modified_at": modified_at,
                "chunk_id":    chunk_id,
                "collection":  QDRANT_COLLECTION,
                "source":      "vault",
            })

    print(f"[indexer] Embedding {len(all_chunks)} chunks from {len(notes)} notes...")
    vectors = await embed_batch(all_chunks)

    points = [
        {
            "id":      meta["chunk_id"],
            "vector":  vec,
            "payload": meta,
        }
        for vec, meta in zip(vectors, all_metadata)
    ]

    # Upsert in batches of 256
    batch_size = 256
    for i in range(0, len(points), batch_size):
        await upsert_points(points[i : i + batch_size])
        print(f"[indexer] Upserted {min(i + batch_size, len(points))}/{len(points)} points")

    # Update the retriever's BM25 corpus for hybrid search (this collection only)
    try:
        from .retriever import update_bm25_corpus
        update_bm25_corpus(all_chunks, all_metadata, collection=QDRANT_COLLECTION)
        print(f"[indexer] BM25 corpus updated ({len(all_chunks)} docs)")
    except Exception as exc:
        print(f"[indexer] Warning: could not update BM25 corpus: {exc}")

    _record_run(QDRANT_COLLECTION, len(notes), len(points), time.monotonic() - t0)

    return {
        "status":        "ok",
        "notes_indexed": len(notes),
        "chunks_total":  len(points),
        "collection":    QDRANT_COLLECTION,
    }


# ---------------------------------------------------------------------------
# WijerCo knowledge base indexing
# ---------------------------------------------------------------------------

# Folders inside the WijerCo directory that are worth indexing
_WIJERCO_INDEX_DIRS = [
    "KNOWLEDGE BASE",
    "AGENTS/departments",
    "AGENTS/subagents",
    "ABOUT ME",
]


async def index_wijerco(wijerco_path: Path = WIJERCO_PATH) -> dict[str, Any]:
    """
    Index the WijerCo knowledge base into a dedicated Qdrant collection
    ('wijerco_knowledge') so RAG queries can retrieve WijerCo-specific context.

    Indexes:
      KNOWLEDGE BASE/*.md      — WijerCo offer, positioning, sector, competitors
      AGENTS/departments/*.md  — department role definitions
      AGENTS/subagents/*.md    — subagent role definitions
      ABOUT ME/*.md            — Aaron's voice, company context
    """
    t0 = time.monotonic()
    # Ensure the WijerCo collection exists
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{QDRANT_URL}/collections/{WIJERCO_COLLECTION}", timeout=10.0
        )
        if resp.status_code != 200:
            resp = await client.put(
                f"{QDRANT_URL}/collections/{WIJERCO_COLLECTION}",
                json={"vectors": {"size": VECTOR_DIM, "distance": "Cosine"}},
                timeout=15.0,
            )
            resp.raise_for_status()
            print(f"[indexer] Created collection '{WIJERCO_COLLECTION}'.")

    # Collect all markdown files from target dirs
    all_files: list[tuple[str, str, str]] = []
    for subdir in _WIJERCO_INDEX_DIRS:
        target = wijerco_path / subdir
        if not target.exists():
            print(f"[indexer] Skipping missing dir: {target}")
            continue
        for md_file in target.rglob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8", errors="replace")
                rel  = str(md_file.relative_to(wijerco_path))
                all_files.append((rel, text, _mtime_iso(md_file)))
            except Exception as exc:
                print(f"[indexer] Could not read {md_file}: {exc}")

    if not all_files:
        return {
            "status":  "error",
            "message": f"No .md files found in WijerCo dirs under {wijerco_path}",
        }

    all_chunks:   list[str]             = []
    all_metadata: list[dict[str, Any]]  = []

    for rel_path, text, modified_at in all_files:
        for j, (chunk, section) in enumerate(chunk_with_sections(text)):
            chunk_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"wijerco::{rel_path}::{j}"))
            all_chunks.append(chunk)
            all_metadata.append({
                "file":        rel_path,
                "text":        chunk,
                "section":     section,
                "modified_at": modified_at,
                "chunk_id":    chunk_id,
                "collection":  WIJERCO_COLLECTION,
                "source":      "wijerco",
            })

    print(f"[indexer] Embedding {len(all_chunks)} WijerCo chunks from {len(all_files)} files...")
    vectors = await embed_batch(all_chunks)

    points = [
        {
            "id":      meta["chunk_id"],
            "vector":  vec,
            "payload": meta,
        }
        for vec, meta in zip(vectors, all_metadata)
    ]

    batch_size = 256
    for i in range(0, len(points), batch_size):
        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{QDRANT_URL}/collections/{WIJERCO_COLLECTION}/points",
                json={"points": points[i : i + batch_size]},
                timeout=60.0,
            )
            resp.raise_for_status()
        print(f"[indexer] WijerCo upserted {min(i + batch_size, len(points))}/{len(points)}")

    # Also update BM25 corpus for hybrid search on the WijerCo collection
    try:
        from .retriever import update_bm25_corpus
        update_bm25_corpus(all_chunks, all_metadata, collection=WIJERCO_COLLECTION)
    except Exception as exc:
        print(f"[indexer] BM25 update warning: {exc}")

    _record_run(WIJERCO_COLLECTION, len(all_files), len(points), time.monotonic() - t0)

    return {
        "status":         "ok",
        "files_indexed":  len(all_files),
        "chunks_total":   len(points),
        "collection":     WIJERCO_COLLECTION,
        "wijerco_path":   str(wijerco_path),
    }


# ---------------------------------------------------------------------------
# FastAPI service
# ---------------------------------------------------------------------------

from fastapi import Depends
from fastapi.middleware.cors import CORSMiddleware
from common.security import require_api_key, cors_kwargs, bind_host
app = FastAPI(title="Vault Indexer Service", dependencies=[Depends(require_api_key)])
app.add_middleware(CORSMiddleware, **cors_kwargs())


class IndexRequest(BaseModel):
    vault_path: str = str(VAULT_PATH)


class WijerCoIndexRequest(BaseModel):
    wijerco_path: str = str(WIJERCO_PATH)


@app.post("/index")
async def trigger_index(req: IndexRequest):
    result = await index_vault(Path(req.vault_path))
    return result


@app.post("/index/wijerco")
async def trigger_wijerco_index(req: WijerCoIndexRequest):
    """Index the WijerCo knowledge base into the wijerco_knowledge collection."""
    result = await index_wijerco(Path(req.wijerco_path))
    return result


@app.get("/health")
def health():
    return {"status": "ok", "service": "indexer"}


@app.get("/health/deep")
async def health_deep():
    from common.health import deep_health, qdrant_check
    return await deep_health([qdrant_check()], service="indexer")


@app.on_event("startup")
async def _startup_checks():
    from common.health import qdrant_check
    from common.startup import require_dependencies
    await require_dependencies([qdrant_check()], service="indexer")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Obsidian Vault Indexer")
    parser.add_argument("--vault", default=str(VAULT_PATH), help="Path to Obsidian vault")
    parser.add_argument("--wijerco", action="store_true", help="Index WijerCo KB instead of vault")
    parser.add_argument("--serve", action="store_true", help="Run as FastAPI service")
    args = parser.parse_args()

    if args.serve:
        uvicorn.run("rag.indexer:app", host=bind_host(), port=PORT, reload=False)
    elif args.wijerco:
        asyncio.run(index_wijerco())
    else:
        asyncio.run(index_vault(Path(args.vault)))
