"""
Unified Recall
==============
One call that reaches every memory store and returns a single merged list.

Stores queried, each hit tagged with its origin:
  * semantic  — entity facts in Qdrant `agent_memory` (memory/memory_store.py)
  * episodic  — summarised past conversations in Qdrant (memory/episodic.py)
  * project   — durable project facts in SQLite (orchestrator/operating.py)

Semantic and episodic recall are vector searches and need Qdrant plus the
embedder; each store fails independently and silently, so recall degrades
rather than breaking when a dependency is down. Project memory is a keyword
filter over SQLite and always works.

Usage:
    from memory.recall import recall, render

    hits = await recall("Swinburne MBA redesign", k=8)
    context_block = render(hits)
"""

from __future__ import annotations

import re
from typing import Any


def _keyword_score(query: str, text: str) -> float:
    """Cheap term-overlap score for the non-vector project store."""
    q_terms = set(re.findall(r"[a-z0-9]{3,}", query.lower()))
    if not q_terms:
        return 0.0
    t_terms = set(re.findall(r"[a-z0-9]{3,}", (text or "").lower()))
    return len(q_terms & t_terms) / len(q_terms)


async def recall(query: str, k: int = 8, project: str | None = None) -> list[dict[str, Any]]:
    """Fan out to all stores, tag, merge, and return the top k hits."""
    hits: list[dict[str, Any]] = []

    # Semantic entity facts.
    try:
        from memory.memory_store import store as semantic_store
        for m in await semantic_store.recall(query, top_k=k):
            hits.append({
                "store": "semantic",
                "content": m.content,
                "entity": m.entity,
                "score": float(m.score or 0.0),
                "source": m.source,
                "timestamp": m.timestamp,
            })
    except Exception:
        pass

    # Episodic conversation summaries.
    try:
        from memory.episodic import recall_episodes
        for ep in await recall_episodes(query, top_k=min(k, 3)):
            hits.append({
                "store": "episodic",
                "content": getattr(ep, "summary", "") or getattr(ep, "content", ""),
                "entity": getattr(ep, "department", None),
                "score": float(getattr(ep, "score", 0.0) or 0.0),
                "source": "episodic",
                "timestamp": getattr(ep, "timestamp", None),
            })
    except Exception:
        pass

    # Project facts (SQLite, keyword scored so they merge on the same scale).
    try:
        from orchestrator import operating
        for m in operating.list_project_memory(project=project, limit=100):
            score = _keyword_score(query, m.get("content", ""))
            if score > 0.15:
                hits.append({
                    "store": "project",
                    "content": m.get("content"),
                    "entity": m.get("project"),
                    "score": score,
                    "source": m.get("source"),
                    "timestamp": m.get("created_at"),
                })
    except Exception:
        pass

    hits.sort(key=lambda h: h["score"], reverse=True)

    # Dedupe near-identical content across stores, keep the higher score.
    seen: list[str] = []
    unique: list[dict[str, Any]] = []
    for h in hits:
        key = re.sub(r"\s+", " ", (h["content"] or "").lower())[:200]
        if any(key and key in s or s and s in key for s in seen):
            continue
        seen.append(key)
        unique.append(h)

    return unique[:k]


def render(hits: list[dict[str, Any]], max_chars: int = 3000) -> str:
    """Render recall hits as a compact context block for a prompt."""
    if not hits:
        return ""
    lines = ["Known context from memory:"]
    used = len(lines[0])
    for h in hits:
        entity = f" ({h['entity']})" if h.get("entity") else ""
        line = f"- [{h['store']}{entity}] {h['content']}"
        if used + len(line) > max_chars:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines)
