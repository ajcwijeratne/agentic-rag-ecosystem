"""
Token Optimizer — picks the cheapest capable model for any given task.

Decision flow:
  1. Detect task_type from the query (classification → code → reasoning → advisory → etc.)
  2. Estimate input + output token counts
  3. Filter models: must be available, must fit input, must have required capability
  4. Among passing models, pick the cheapest by estimated total cost
  5. If no cloud model is available, fall back to local Ollama

The optimizer is deterministic — same inputs produce the same model choice.
It never makes an LLM call itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .llm_registry import ModelSpec, available_models

# ─────────────────────────────────────────────────────────────────────────────
# Task type detection
# ─────────────────────────────────────────────────────────────────────────────

# Ordered from most-specific to least-specific
_TASK_SIGNALS: list[tuple[str, list[str]]] = [
    ("code", [
        "write code", "debug", "python", "javascript", "typescript", "function",
        "class", "script", "refactor", "bug", "error", "stack trace", "implement",
        "algorithm", "api endpoint", "sql", "bash", "shell script",
    ]),
    ("long_context", [
        # Triggered by document size heuristic in classify_task(), not keywords alone
    ]),
    ("reasoning", [
        "why does", "explain why", "analyse", "analyze", "compare", "evaluate",
        "pros and cons", "trade-off", "root cause", "logical", "infer",
        "hypothesis", "argue", "critique", "assess the impact",
        "strategic recommendation", "deep dive", "first principles",
    ]),
    ("advisory", [
        "proposal", "strategy", "recommend", "advice", "plan",
        "curriculum", "course design", "workshop", "assessment design",
        "linkedin post", "article", "thought leadership", "sector",
        "wijerco", "client", "higher education", "teqsa", "aqf",
    ]),
    ("creative", [
        "write a", "draft", "blog post", "email", "newsletter", "cover letter",
        "social media", "caption", "story", "creative", "pitch",
    ]),
    ("summary", [
        "summarise", "summarize", "tldr", "key points", "main ideas",
        "condense", "brief", "overview", "digest",
    ]),
    ("retrieval", [
        "search", "find", "retrieve", "look up", "fetch", "get me",
        "what's in my", "obsidian", "notes", "vault",
    ]),
    ("classification", [
        "is this", "classify", "categorise", "yes or no", "true or false",
        "which of", "label", "detect",
    ]),
]

# Expected output tokens by task type (used for cost estimation)
_EXPECTED_OUTPUT_TOKENS: dict[str, int] = {
    "classification": 50,
    "retrieval":      300,
    "summary":        400,
    "creative":       800,
    "advisory":       1_200,
    "reasoning":      1_500,
    "code":           1_000,
    "long_context":   1_000,
    "fast":           200,
}

# Minimum capability tier required per task type
# (prevents using phi3 for a complex advisory task even if it's free)
_MIN_TIER: dict[str, int] = {
    "classification": 0,
    "retrieval":      0,
    "fast":           0,
    "summary":        0,
    "creative":       1,
    "advisory":       1,
    "code":           1,
    "reasoning":      1,
    "long_context":   1,
}


def classify_task(query: str, input_tokens: int = 0) -> str:
    """Return the primary task type for a query string."""
    # Long document override
    if input_tokens > 30_000:
        return "long_context"

    q = query.lower()
    for task_type, signals in _TASK_SIGNALS:
        if not signals:
            continue
        if any(s in q for s in signals):
            return task_type

    # Default
    return "advisory"


def rough_token_count(text: str) -> int:
    """Approximate token count at ~4 chars/token."""
    return max(1, len(text) // 4)


# ─────────────────────────────────────────────────────────────────────────────
# Optimization result
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OptimizationResult:
    model_key:         str
    model:             ModelSpec
    task_type:         str
    input_tokens:      int
    expected_output:   int
    estimated_cost_usd: float
    reason:            str
    candidates_scored: list[tuple[str, float]]   # [(model_key, cost), ...]


# ─────────────────────────────────────────────────────────────────────────────
# Main optimizer
# ─────────────────────────────────────────────────────────────────────────────

def pick_model(
    query: str,
    system_prompt: str = "",
    force_model_key: str | None = None,
    force_task_type: str | None = None,
    max_tier: int = 3,
) -> OptimizationResult:
    """
    Pick the cheapest available model that can handle the query.

    Args:
        query:           The user's query text.
        system_prompt:   Any system prompt that will be prepended (adds to input tokens).
        force_model_key: Override — skip optimisation and use this model key.
        force_task_type: Override the auto-detected task type.
        max_tier:        Cap on model tier (0 = local-only, 1 = budget, 2 = mid, 3 = all).

    Returns:
        OptimizationResult with the chosen model and cost estimate.
    """
    input_tokens = rough_token_count(query + system_prompt)
    task_type    = force_task_type or classify_task(query, input_tokens)
    expected_out = _EXPECTED_OUTPUT_TOKENS.get(task_type, 600)

    # If forced, validate and return early
    if force_model_key:
        from .llm_registry import MODELS
        if force_model_key in MODELS:
            m = MODELS[force_model_key]
            return OptimizationResult(
                model_key          = force_model_key,
                model              = m,
                task_type          = task_type,
                input_tokens       = input_tokens,
                expected_output    = expected_out,
                estimated_cost_usd = m.estimated_cost_usd(input_tokens, expected_out),
                reason             = "Manually forced",
                candidates_scored  = [],
            )

    min_tier = _MIN_TIER.get(task_type, 0)
    pool = {
        k: v for k, v in available_models().items()
        if (
            v.tier >= min_tier
            and v.tier <= max_tier
            and v.can_handle(input_tokens)
            and task_type in v.capabilities
        )
    }

    # If nothing qualifies (e.g. long_context but no key set), loosen tier filter
    if not pool:
        pool = {
            k: v for k, v in available_models().items()
            if v.can_handle(input_tokens)
        }

    # Absolute fallback: local Ollama
    if not pool:
        from .llm_registry import MODELS
        fallback_key = "ollama/llama3"
        fallback     = MODELS[fallback_key]
        return OptimizationResult(
            model_key          = fallback_key,
            model              = fallback,
            task_type          = task_type,
            input_tokens       = input_tokens,
            expected_output    = expected_out,
            estimated_cost_usd = 0.0,
            reason             = "Fallback to local Ollama — no cloud keys configured",
            candidates_scored  = [],
        )

    # Score: cost for this specific call
    scored = sorted(
        ((k, v.estimated_cost_usd(input_tokens, expected_out)) for k, v in pool.items()),
        key=lambda x: x[1],
    )

    best_key, best_cost = scored[0]
    best_model = pool[best_key]

    return OptimizationResult(
        model_key          = best_key,
        model              = best_model,
        task_type          = task_type,
        input_tokens       = input_tokens,
        expected_output    = expected_out,
        estimated_cost_usd = best_cost,
        reason             = (
            f"Cheapest capable model for '{task_type}' "
            f"({len(pool)} candidates, tier {best_model.tier})"
        ),
        candidates_scored  = scored[:8],
    )
