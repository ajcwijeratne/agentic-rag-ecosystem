"""
Context assembler — turns a pile of retrieved chunks into the text that goes to
the model. Deliberate, not "concatenate the first ten".

Stages, in order:
  1. Score      — effective score is rerank_score when present, else score.
  2. Recency    — blend the score with a freshness factor from modified_at.
  3. Dedupe     — drop chunks whose text overlaps a kept chunk (Jaccard).
  4. Diversity  — cap chunks per file and per collection.
  5. Select     — keep the top max_chunks after the filters.
  6. Compress   — when over token_budget, trim each chunk to its most
                  query-relevant sentences rather than hard-truncating.
  7. Render     — number each kept chunk [n] with its file and section, so the
                  model can cite [n] and the synthesis node can map markers back.

Pure Python, no service calls, deterministic for a given input. Tunable via env.
"""

from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from typing import Any

from rag.schema import Chunk

# ─────────────────────────────────────────────────────────────────────────────
# Tunables
# ─────────────────────────────────────────────────────────────────────────────

def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


DEDUPE_THRESHOLD      = _f("CONTEXT_DEDUPE_THRESHOLD", 0.85)
MAX_PER_FILE          = _i("CONTEXT_MAX_PER_FILE", 3)
MAX_PER_COLLECTION    = _i("CONTEXT_MAX_PER_COLLECTION", 0)        # 0 = no cap
RECENCY_WEIGHT        = _f("CONTEXT_RECENCY_WEIGHT", 0.15)
RECENCY_HALFLIFE_DAYS = _f("CONTEXT_RECENCY_HALFLIFE_DAYS", 180.0)
DEFAULT_MAX_CHUNKS    = _i("CONTEXT_MAX_CHUNKS", 8)
DEFAULT_TOKEN_BUDGET  = _i("CONTEXT_TOKEN_BUDGET", 2000)

_WORD_RE = re.compile(r"[a-z0-9]+")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")


def _tokens(text: str) -> int:
    """Rough token count at ~4 chars/token."""
    return max(1, len(text) // 4)


def _word_set(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _effective_score(c: Chunk) -> float:
    return c.rerank_score if c.rerank_score is not None else c.score


def _recency_factor(modified_at: str, now: datetime) -> float:
    """1.0 for a file modified now, decaying by half every half-life. 0 if unknown."""
    if not modified_at:
        return 0.0
    try:
        ts = datetime.fromisoformat(modified_at)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return 0.0
    age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
    if RECENCY_HALFLIFE_DAYS <= 0:
        return 0.0
    return math.pow(0.5, age_days / RECENCY_HALFLIFE_DAYS)


# ─────────────────────────────────────────────────────────────────────────────
# Compression
# ─────────────────────────────────────────────────────────────────────────────

def _compress(text: str, query_terms: set[str], max_tokens: int) -> str:
    """Keep the sentences most relevant to the query, in original order, until
    max_tokens is reached. Falls back to a hard char trim if needed."""
    if _tokens(text) <= max_tokens:
        return text
    sentences = _SENT_RE.split(text.strip())
    if len(sentences) <= 1:
        return text[: max_tokens * 4]

    scored = []
    for idx, s in enumerate(sentences):
        overlap = len(_word_set(s) & query_terms)
        scored.append((overlap, idx, s))
    # Best-scoring sentences first, ties broken by original order.
    scored.sort(key=lambda x: (-x[0], x[1]))

    kept: list[tuple[int, str]] = []
    used = 0
    for overlap, idx, s in scored:
        t = _tokens(s)
        if used + t > max_tokens and kept:
            break
        kept.append((idx, s))
        used += t
    kept.sort(key=lambda x: x[0])
    out = " ".join(s for _, s in kept)
    return out or text[: max_tokens * 4]


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def assemble(
    chunks: list[dict[str, Any]],
    query: str = "",
    max_chunks: int = DEFAULT_MAX_CHUNKS,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Select, order, compress, and render retrieved chunks.

    Returns:
      {
        "chunks":   [dict, ...]  # selected, each with 'citation_index' and 'text'
        "rendered": str          # citation-numbered context block for the prompt
        "stats":    {...}        # counts before/after each stage, token totals
      }
    """
    now = now or datetime.now(timezone.utc)
    objs = [Chunk.from_dict(c) for c in (chunks or [])]
    raw_count = len(objs)

    # Stage 1+2: effective score blended with recency.
    def blended(c: Chunk) -> float:
        base = _effective_score(c)
        rec = _recency_factor(c.modified_at, now)
        return base * (1.0 + RECENCY_WEIGHT * rec)

    objs.sort(key=blended, reverse=True)

    # Stage 3+4: dedupe and diversity caps, taking best first.
    kept: list[Chunk] = []
    kept_words: list[set[str]] = []
    per_file: dict[str, int] = {}
    per_collection: dict[str, int] = {}
    dropped_dupe = 0
    dropped_diversity = 0

    for c in objs:
        if len(kept) >= max_chunks:
            break
        ws = _word_set(c.text)
        if any(_jaccard(ws, kw) >= DEDUPE_THRESHOLD for kw in kept_words):
            dropped_dupe += 1
            continue
        if MAX_PER_FILE and per_file.get(c.file, 0) >= MAX_PER_FILE and c.file:
            dropped_diversity += 1
            continue
        if (MAX_PER_COLLECTION and c.collection
                and per_collection.get(c.collection, 0) >= MAX_PER_COLLECTION):
            dropped_diversity += 1
            continue
        kept.append(c)
        kept_words.append(ws)
        if c.file:
            per_file[c.file] = per_file.get(c.file, 0) + 1
        if c.collection:
            per_collection[c.collection] = per_collection.get(c.collection, 0) + 1

    # Stage 6: compression when the kept set exceeds the budget.
    tokens_before = sum(_tokens(c.text) for c in kept)
    query_terms = _word_set(query)
    rendered_chunks: list[dict[str, Any]] = []
    compressed_any = False

    if kept:
        per_chunk_budget = max(64, token_budget // len(kept))
    else:
        per_chunk_budget = token_budget

    for i, c in enumerate(kept, start=1):
        text = c.text
        if tokens_before > token_budget:
            new_text = _compress(text, query_terms, per_chunk_budget)
            if new_text != text:
                compressed_any = True
            text = new_text
        d = c.to_dict()
        d["citation_index"] = i
        d["prompt_text"] = text       # possibly compressed copy used in the prompt
        rendered_chunks.append(d)

    tokens_after = sum(_tokens(d["prompt_text"]) for d in rendered_chunks)

    # Stage 7: citation-aware render.
    rendered = render_context(rendered_chunks)

    return {
        "chunks": rendered_chunks,
        "rendered": rendered,
        "stats": {
            "raw_count":          raw_count,
            "kept_count":         len(rendered_chunks),
            "dropped_duplicates": dropped_dupe,
            "dropped_diversity":  dropped_diversity,
            "tokens_before":      tokens_before,
            "tokens_after":       tokens_after,
            "compressed":         compressed_any,
        },
    }


def render_context(rendered_chunks: list[dict[str, Any]]) -> str:
    """Number each chunk [n] with its file and section for citation."""
    if not rendered_chunks:
        return ""
    blocks = []
    for d in rendered_chunks:
        n = d.get("citation_index")
        file = d.get("file", "") or "unknown"
        section = d.get("section", "")
        header = f"[{n}] {file}"
        if section:
            header += f" > {section}"
        body = d.get("prompt_text", d.get("text", ""))
        blocks.append(f"{header}\n{body}")
    return "\n\n".join(blocks)


CITATION_SYSTEM_PROMPT = (
    "You are a careful assistant. Answer using the numbered context below where "
    "relevant. When a sentence relies on a source, cite it inline with its number "
    "in square brackets, for example [1] or [2]. Cite only sources you used. If "
    "the context does not contain the answer, say so rather than guessing."
)


def extract_citation_indices(answer: str) -> list[int]:
    """Return the distinct [n] markers used in the answer, in first-seen order."""
    seen: list[int] = []
    for m in re.findall(r"\[(\d{1,3})\]", answer or ""):
        n = int(m)
        if n not in seen:
            seen.append(n)
    return seen
