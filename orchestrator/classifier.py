"""
Task-type classifier with confidence and ambiguity handling.

This is the routing authority. It replaces first-match-wins keyword logic with a
scored model: each task type accumulates weighted keyword hits, the scores are
softmaxed into a confidence, and a low confidence or a narrow margin falls back
to a safe default instead of guessing.

Transparent and dependency-free by default. An optional embedding stage
(ROUTER_USE_EMBEDDING=1) can refine the decision against per-label centroids
tuned from the eval set; it stays off unless those centroids exist.

Thresholds (env):
  ROUTER_MIN_CONFIDENCE   default 0.45
  ROUTER_MIN_MARGIN       default 0.15
  ROUTER_DEFAULT_TASK     default "advisory"
  ROUTER_USE_EMBEDDING    default off
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

# Canonical keyword signals. Kept here (not imported from token_optimizer) so the
# classifier has no heavy imports and stays unit-testable. Phrases with more
# words are more specific and weighted higher.
TASK_SIGNALS: dict[str, list[str]] = {
    "code": [
        "write code", "debug", "python", "javascript", "typescript", "function",
        "class", "script", "refactor", "stack trace", "implement",
        "algorithm", "api endpoint", "sql", "bash", "shell script",
    ],
    "reasoning": [
        "why does", "explain why", "analyse", "analyze", "compare", "evaluate",
        "pros and cons", "trade-off", "root cause", "infer",
        "hypothesis", "argue", "critique", "assess the impact",
        "strategic recommendation", "deep dive", "first principles",
    ],
    "advisory": [
        "proposal", "strategy", "recommend", "advice", "plan",
        "curriculum", "course design", "workshop", "assessment design",
        "linkedin post", "article", "thought leadership", "sector",
        "wijerco", "client", "higher education", "teqsa", "aqf",
    ],
    "creative": [
        "write a", "draft", "blog post", "email", "newsletter", "cover letter",
        "social media", "caption", "story", "creative", "pitch",
    ],
    "summary": [
        "summarise", "summarize", "tldr", "key points", "main ideas",
        "condense", "brief", "overview", "digest",
    ],
    "retrieval": [
        "search", "find", "retrieve", "look up", "fetch", "get me",
        "what's in my", "obsidian", "notes", "vault",
    ],
    "classification": [
        "is this", "classify", "categorise", "categorize", "yes or no",
        "true or false", "which of", "label", "detect",
    ],
}

DEFAULT_TASK   = os.getenv("ROUTER_DEFAULT_TASK", "advisory")
LONG_CONTEXT_TOKENS = int(os.getenv("ROUTER_LONG_CONTEXT_TOKENS", "30000"))


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


MIN_CONFIDENCE = _f("ROUTER_MIN_CONFIDENCE", 0.45)
MIN_MARGIN     = _f("ROUTER_MIN_MARGIN", 0.15)
USE_EMBEDDING  = os.getenv("ROUTER_USE_EMBEDDING", "0").lower() in ("1", "true", "yes")


@dataclass
class ClassificationResult:
    task_type:  str
    confidence: float
    runner_up:  str | None
    margin:     float
    method:     str                  # heuristic | embedding
    decided_by: str                  # heuristic | embedding | low_confidence_default | long_context
    scores:     dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_type":  self.task_type,
            "confidence": round(self.confidence, 4),
            "runner_up":  self.runner_up,
            "margin":     round(self.margin, 4),
            "method":     self.method,
            "decided_by": self.decided_by,
            "scores":     {k: round(v, 4) for k, v in self.scores.items()},
        }


def _heuristic_scores(query: str) -> dict[str, float]:
    q = query.lower()
    scores: dict[str, float] = {}
    for task, signals in TASK_SIGNALS.items():
        total = 0.0
        for s in signals:
            if s in q:
                total += 1.0 + 0.5 * (s.count(" "))   # multi-word phrases weigh more
        if total:
            scores[task] = total
    return scores


def _softmax(scores: dict[str, float], temperature: float = 1.0) -> dict[str, float]:
    if not scores:
        return {}
    vals = {k: v / temperature for k, v in scores.items()}
    mx = max(vals.values())
    exp = {k: math.exp(v - mx) for k, v in vals.items()}
    total = sum(exp.values())
    return {k: v / total for k, v in exp.items()}


def classify(query: str, input_tokens: int = 0) -> ClassificationResult:
    """Classify a query into a task type with a confidence and runner-up."""
    # Long-document override beats any keyword signal.
    if input_tokens > LONG_CONTEXT_TOKENS:
        return ClassificationResult(
            task_type="long_context", confidence=1.0, runner_up=None,
            margin=1.0, method="heuristic", decided_by="long_context",
            scores={"long_context": 1.0},
        )

    raw = _heuristic_scores(query)
    probs = _softmax(raw)

    if not probs:
        return ClassificationResult(
            task_type=DEFAULT_TASK, confidence=0.0, runner_up=None, margin=0.0,
            method="heuristic", decided_by="low_confidence_default", scores={},
        )

    ordered = sorted(probs.items(), key=lambda x: x[1], reverse=True)
    top_task, top_p = ordered[0]
    runner_up, runner_p = (ordered[1] if len(ordered) > 1 else (None, 0.0))
    margin = top_p - runner_p

    method = "heuristic"
    if USE_EMBEDDING:
        refined = _embedding_refine(query, probs)
        if refined is not None:
            top_task, top_p, runner_up, runner_p, method = refined
            margin = top_p - runner_p

    if top_p < MIN_CONFIDENCE or margin < MIN_MARGIN:
        return ClassificationResult(
            task_type=DEFAULT_TASK, confidence=top_p, runner_up=top_task,
            margin=margin, method=method, decided_by="low_confidence_default",
            scores=probs,
        )

    return ClassificationResult(
        task_type=top_task, confidence=top_p, runner_up=runner_up,
        margin=margin, method=method, decided_by=method, scores=probs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Optional embedding stage (off by default, dependency-free unless enabled)
# ─────────────────────────────────────────────────────────────────────────────

_CENTROID_PATH = os.getenv(
    "ROUTER_CENTROID_PATH",
    os.path.join(os.path.dirname(__file__), os.pardir, "logs", "router_centroids.json"),
)


def _embedding_refine(query: str, heuristic_probs: dict[str, float]):
    """Blend heuristic probs with cosine similarity to per-label centroids.

    Returns (top_task, top_p, runner_up, runner_p, "embedding") or None if the
    centroid file or the embedder is unavailable, in which case the caller keeps
    the heuristic result. Never raises.
    """
    try:
        import json
        if not os.path.exists(_CENTROID_PATH):
            return None
        with open(_CENTROID_PATH, "r", encoding="utf-8") as fh:
            centroids = json.load(fh)          # {task: [floats]}
        if not centroids:
            return None

        from rag.embedder import embed_text   # async
        import asyncio
        vec = asyncio.get_event_loop().run_until_complete(embed_text(query))

        def cos(a, b):
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a)) or 1.0
            nb = math.sqrt(sum(y * y for y in b)) or 1.0
            return dot / (na * nb)

        sims = {t: max(0.0, cos(vec, c)) for t, c in centroids.items()}
        sim_probs = _softmax(sims)
        blended = {
            t: 0.5 * heuristic_probs.get(t, 0.0) + 0.5 * sim_probs.get(t, 0.0)
            for t in set(heuristic_probs) | set(sim_probs)
        }
        total = sum(blended.values()) or 1.0
        blended = {t: v / total for t, v in blended.items()}
        ordered = sorted(blended.items(), key=lambda x: x[1], reverse=True)
        top_task, top_p = ordered[0]
        runner_up, runner_p = (ordered[1] if len(ordered) > 1 else (None, 0.0))
        return top_task, top_p, runner_up, runner_p, "embedding"
    except Exception:
        return None
