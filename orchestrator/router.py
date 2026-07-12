"""
Model Router — decides whether to use local Ollama or DeepSeek-R1 cloud.

Routing heuristics (all configurable via env vars):
  - Token length  > TOKEN_THRESHOLD   -> cloud
  - Task type from the scored classifier (reasoning/code/long_context/advisory)
  - Otherwise                         -> local

Local model  : Ollama  (llama3 / phi3)
Cloud model  : DeepSeek-R1 via OpenAI-compatible API
"""

from __future__ import annotations

import os
import re
from typing import Literal

from pydantic import ValidationError

from .state import AgentState, RoutingDecision

# ---------------------------------------------------------------------------
# Configuration (override via environment)
# ---------------------------------------------------------------------------

TOKEN_THRESHOLD: int = int(os.getenv("ROUTER_TOKEN_THRESHOLD", "512"))

LOCAL_MODEL: str = os.getenv("OLLAMA_MODEL", "llama3")
CLOUD_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-reasoner")

# Keywords that signal a complex / reasoning-heavy task
COMPLEXITY_KEYWORDS: list[str] = [
    "reason", "explain why", "analyse", "analyze", "compare", "synthesize",
    "summarise research", "summarize research", "write code", "debug",
    "architecture", "strategy", "plan", "deep dive", "pros and cons",
    "evaluate", "critique", "hypothesis",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rough_token_count(text: str) -> int:
    """Approximate token count: ~4 chars per token."""
    return max(1, len(text) // 4)


def _is_complex(query: str) -> bool:
    q = query.lower()
    return any(kw in q for kw in COMPLEXITY_KEYWORDS)


# ---------------------------------------------------------------------------
# Router node (called as a LangGraph node)
# ---------------------------------------------------------------------------

# Task types that justify the cloud (reasoning-heavy or long).
_CLOUD_TASKS = {"reasoning", "code", "long_context", "advisory"}


def route_query(state: AgentState) -> AgentState:
    """
    Examine the query and populate state["routing"] with a RoutingDecision.
    Uses the scored classifier for task type and confidence, then maps that plus
    token length to a local/cloud backend. Never calls an LLM. Logs the decision.
    """
    from .classifier import classify
    from .decision_log import log_decision

    query = state.get("query", "")
    token_count = _rough_token_count(query)
    cls = classify(query, input_tokens=token_count)

    long_or_complex = token_count > TOKEN_THRESHOLD
    task_wants_cloud = cls.task_type in _CLOUD_TASKS and cls.decided_by != "low_confidence_default"

    if long_or_complex or task_wants_cloud or _is_complex(query):
        backend, model_name = "cloud", CLOUD_MODEL
        if long_or_complex:
            why = f"token count {token_count} > threshold {TOKEN_THRESHOLD}"
        elif task_wants_cloud:
            why = f"task '{cls.task_type}' (conf {cls.confidence:.2f}) routed to cloud"
        else:
            why = "complexity keywords detected"
    else:
        backend, model_name = "local", LOCAL_MODEL
        why = (f"task '{cls.task_type}' (conf {cls.confidence:.2f}, "
               f"{cls.decided_by}) routed to local inference")

    decision = RoutingDecision(
        model=backend,
        model_name=model_name,
        reason=why,
        estimated_tokens=token_count,
        confidence=cls.confidence,
        runner_up=cls.runner_up,
        decided_by=cls.decided_by,
    )

    log_decision("task_route", query, {
        "backend": backend,
        "model_name": model_name,
        **cls.to_dict(),
    })

    return {**state, "routing": decision}
