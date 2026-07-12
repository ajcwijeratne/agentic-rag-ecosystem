"""
Long-term Agent Memory Store
=============================
Stores entity-keyed memory entries in a dedicated Qdrant collection
('agent_memory'). Each entry has:
  • entity    — who/what this memory is about (client name, project, person)
  • content   — the remembered fact
  • source    — which department agent extracted it
  • timestamp — epoch float
  • embedding — for semantic recall

The memory collection uses the same nomic-embed-text embeddings as the vault,
so semantic recall finds relevant memories even when the exact entity name
isn't mentioned.

Usage:
    from memory.memory_store import store

    await store.add("Swinburne University", "Needs a new unit design for MBA.")
    memories = await store.recall("MBA curriculum redesign", top_k=3)
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

QDRANT_URL:        str = os.getenv("QDRANT_URL",        "http://localhost:6333")
MEMORY_COLLECTION: str = os.getenv("MEMORY_COLLECTION", "agent_memory")
VECTOR_DIM:        int = 768


@dataclass
class MemoryEntry:
    id:        str
    entity:    str
    content:   str
    source:    str
    timestamp: float
    score:     float = 0.0


class MemoryStore:
    """Qdrant-backed persistent memory with semantic recall."""

    # ── Collection setup ──────────────────────────────────────────────────

    async def ensure_collection(self) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{QDRANT_URL}/collections/{MEMORY_COLLECTION}", timeout=10.0
            )
            if resp.status_code == 200:
                return
            resp = await client.put(
                f"{QDRANT_URL}/collections/{MEMORY_COLLECTION}",
                json={"vectors": {"size": VECTOR_DIM, "distance": "Cosine"}},
                timeout=15.0,
            )
            resp.raise_for_status()

    # ── Write ─────────────────────────────────────────────────────────────

    async def add(
        self,
        entity:  str,
        content: str,
        source:  str = "unknown",
    ) -> str:
        """Store a memory and return its UUID."""
        await self.ensure_collection()

        from rag.embedder import embed_text
        vector = await embed_text(f"{entity}: {content}")
        entry_id = str(uuid.uuid4())

        async with httpx.AsyncClient() as client:
            resp = await client.put(
                f"{QDRANT_URL}/collections/{MEMORY_COLLECTION}/points",
                json={
                    "points": [{
                        "id":      entry_id,
                        "vector":  vector,
                        "payload": {
                            "entity":    entity,
                            "content":   content,
                            "source":    source,
                            "timestamp": time.time(),
                        },
                    }]
                },
                timeout=15.0,
            )
            resp.raise_for_status()

        return entry_id

    # ── Read ──────────────────────────────────────────────────────────────

    async def recall(
        self,
        query:  str,
        top_k:  int   = 5,
        entity: str | None = None,
    ) -> list[MemoryEntry]:
        """
        Semantically recall memories relevant to the query.
        Optionally filter to a specific entity with a Qdrant payload filter.
        """
        try:
            await self.ensure_collection()
        except Exception:
            return []

        from rag.embedder import embed_text
        vector = await embed_text(query)

        body: dict[str, Any] = {
            "vector":       vector,
            "limit":        top_k,
            "with_payload": True,
        }
        if entity:
            body["filter"] = {"must": [{"key": "entity", "match": {"value": entity}}]}

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{QDRANT_URL}/collections/{MEMORY_COLLECTION}/points/search",
                    json=body,
                    timeout=10.0,
                )
                resp.raise_for_status()
                results = resp.json().get("result", [])
        except Exception:
            return []

        return [
            MemoryEntry(
                id        = r["id"],
                entity    = r["payload"].get("entity",    ""),
                content   = r["payload"].get("content",   ""),
                source    = r["payload"].get("source",    ""),
                timestamp = r["payload"].get("timestamp", 0.0),
                score     = round(r.get("score", 0.0), 4),
            )
            for r in results
        ]

    async def delete(self, entry_id: str) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{QDRANT_URL}/collections/{MEMORY_COLLECTION}/points/delete",
                json={"points": [entry_id]},
                timeout=10.0,
            )

    async def clear_all(self) -> None:
        """Delete and recreate the memory collection."""
        async with httpx.AsyncClient() as client:
            await client.delete(
                f"{QDRANT_URL}/collections/{MEMORY_COLLECTION}", timeout=10.0
            )
        await self.ensure_collection()

    def format_for_prompt(self, memories: list[MemoryEntry]) -> str:
        """Convert recalled memories into a concise text block for LLM injection."""
        if not memories:
            return ""
        lines = ["[Recalled memories relevant to this query:]"]
        for m in memories:
            lines.append(f"• [{m.entity}] {m.content}")
        return "\n".join(lines)


# Module-level singleton
store = MemoryStore()
