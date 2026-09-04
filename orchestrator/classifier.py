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
import re
from dataclasses import dataclass, field

# Canonical keyword signals. Kept here (not imported from token_optimizer) so the
# classifier has no heavy imports and stays unit-testable. Phrases with more
# words are more specific and weighted higher.
#
# RULE: a signal describes the KIND OF WORK being asked for, never the subject
# matter. Domain vocabulary (curriculum, sector, client, TEQSA, AQF, workshop,
# proposal ...) belongs in orchestrator/wijerco_router.py, which routes on
# department. Those words were in "advisory" until 4 Sep 2026, which meant every
# query about WijerCo's actual subject matter scored advisory whatever the task
# was. See docs/router-calibration-2026-09-04.md.
TASK_SIGNALS: dict[str, list[str]] = {
    "code": [
        "write code", "debug", "python", "javascript", "typescript", "function",
        "class", "script", "refactor", "stack trace", "traceback", "implement",
        "algorithm", "api endpoint", "sql", "bash", "shell script",
        "unit test", "regex", "compile", "exception",
    ],
    "reasoning": [
        "why", "why does", "why did", "why is", "explain why", "what explains",
        "what caused", "reason for", "root cause", "diagnose",
        "analyse", "analyze", "evaluate", "assess", "compare", "contrast",
        "pros and cons", "trade-off", "tradeoff", "infer", "hypothesis",
        "argue", "critique", "justify", "figure out", "work out",
        "make sense of", "does the evidence", "supported by", "how likely",
        "implications", "first principles", "deep dive",
        "benchmark", "the claim",
    ],
    "advisory": [
        "strategy", "recommend", "advice", "advise", "plan", "propose",
        "should we", "should i", "how should", "what should",
        "how do we", "how can we", "best way", "approach for", "approach to",
        "roadmap", "options for", "guidance on",
        "design a", "design an", "design the", "redesign", "set up",
    ],
    "creative": [
        "write a", "write an", "write the", "draft", "compose", "creative",
        "blog post", "email", "newsletter", "cover letter", "press release",
        "position description", "job ad", "abstract", "acknowledgement",
        "social media", "caption", "headline", "copy for", "wording",
        "story", "reply",
    ],
    "summary": [
        "summarise", "summarize", "summary of", "tldr", "key points",
        "main ideas", "condense", "brief me", "overview", "digest",
        "recap", "what changed", "boil down", "in short",
    ],
    "retrieval": [
        "search", "find", "retrieve", "look up", "look for", "fetch",
        "get me", "show me", "pull together", "pull the",
        "what is the", "what are the", "what is our", "what are our",
        "what does the", "how much", "how many",
        "sources on", "sources for", "figures for", "data on", "statistics",
        "what's in my", "obsidian", "my notes", "the notes", "vault",
    ],
    "classification": [
        "is this", "are these", "does this", "classify", "categorise",
        "categorize", "yes or no", "true or false", "which of", "which of these",
        "label", "detect", "tag", "triage",
        "eligible", "qualify", "compliant", "meets the", "equivalent", "valid",
        "rank these", "sort these", "prioritise these",
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


_SIGNAL_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _signal_re(signal: str) -> "re.Pattern[str]":
    """Whole-word match for a signal phrase.

    Plain substring matching silently fired on word fragments: "research"
    contains "search" (retrieval) and "assessment" contains "assess"
    (reasoning), so any query using those very common words was misrouted.
    """
    rx = _SIGNAL_RE_CACHE.get(signal)
    if rx is None:
        rx = re.compile(r"\b" + re.escape(signal) + r"\b")
        _SIGNAL_RE_CACHE[signal] = rx
    return rx


def _heuristic_scores(query: str) -> dict[str, float]:
    q = query.lower()
    scores: dict[str, float] = {}
    for task, signals in TASK_SIGNALS.items():
        total = 0.0
        for s in signals:
            if _signal_re(s).search(q):
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
