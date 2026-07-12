"""
Episodic Memory — summarised past conversations.

The middle tier of the three-tier memory model:
  • Working   — the live session (orchestrator/session_store.py)
  • Episodic  — summaries of completed conversations (this module)
  • Semantic  — durable entity/client facts (memory/memory_store.py)

After a session has a few turns, summarise_session() condenses it (cheap local
model) into a short episode and stores it in a dedicated Qdrant collection.
recall_episodes() returns semantically relevant past episodes for a new query.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass

import httpx

QDRANT_URL:          str = os.getenv("QDRANT_URL", "http://localhost:6333")
EPISODIC_COLLECTION: str = os.getenv("EPISODIC_COLLECTION", "episodic_memory")
VECTOR_DIM:          int = 768


@dataclass
class Episode:
    id:         str
    session_id: str
    summary:    str
    department: str
    timestamp:  float
    score:      float = 0.0


async def _ensure_collection() -> None:
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{QDRANT_URL}/collections/{EPISODIC_COLLECTION}", timeout=10.0)
        if resp.status_code == 200:
            return
        resp = await client.put(
            f"{QDRANT_URL}/collections/{EPISODIC_COLLECTION}",
            json={"vectors": {"size": VECTOR_DIM, "distance": "Cosine"}},
            timeout=15.0,
        )
        resp.raise_for_status()


_SUMMARY_SYSTEM = """\
Summarise this conversation into 2-4 sentences for long-term memory. Capture:
what the user wanted, what was decided or produced, any client/project names,
and any open follow-ups. Be specific and factual. No preamble — summary only.
"""


async def summarise_session(session_id: str, department: str = "general") -> Episode | None:
    """
    Condense a session's messages into an episode and store it.
    Returns the Episode, or None if the session is too short / on failure.
    """
    from orchestrator.session_store import get_messages
    from orchestrator.multi_llm import call_model
    from rag.embedder import embed_text

    msgs = get_messages(session_id, limit=40)
    convo = [m for m in msgs if m.role in ("user", "assistant")]
    if len(convo) < 2:
        return None

    transcript = "\n".join(f"{m.role}: {m.content[:1000]}" for m in convo)[:6000]

    try:
        resp = await call_model(
            user_message    = transcript,
            system_prompt   = _SUMMARY_SYSTEM,
            force_model_key = "ollama/llama3",   # local, free
        )
        summary = (resp.content or "").strip()
        if not summary:
            return None

        await _ensure_collection()
        vec = await embed_text(summary)
        ep_id = str(uuid.uuid4())
        async with httpx.AsyncClient() as client:
            await client.put(
                f"{QDRANT_URL}/collections/{EPISODIC_COLLECTION}/points",
                json={"points": [{
                    "id":      ep_id,
                    "vector":  vec,
                    "payload": {
                        "session_id": session_id,
                        "summary":    summary,
                        "department": department,
                        "timestamp":  time.time(),
                    },
                }]},
                timeout=15.0,
            )
        return Episode(id=ep_id, session_id=session_id, summary=summary,
                       department=department, timestamp=time.time())
    except Exception:
        return None


async def recall_episodes(query: str, top_k: int = 3) -> list[Episode]:
    """Return past episodes semantically relevant to the query."""
    try:
        await _ensure_collection()
        from rag.embedder import embed_text
        vec = await embed_text(query)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{QDRANT_URL}/collections/{EPISODIC_COLLECTION}/points/search",
                json={"vector": vec, "limit": top_k, "with_payload": True, "score_threshold": 0.35},
                timeout=10.0,
            )
            resp.raise_for_status()
            results = resp.json().get("result", [])
    except Exception:
        return []

    return [
        Episode(
            id=r["id"], session_id=r["payload"].get("session_id", ""),
            summary=r["payload"].get("summary", ""),
            department=r["payload"].get("department", ""),
            timestamp=r["payload"].get("timestamp", 0.0),
            score=round(r.get("score", 0.0), 4),
        )
        for r in results
    ]
