"""
State schema for the LangGraph orchestrator.
All nodes read from and write to this shared TypedDict.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from typing_extensions import TypedDict
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Pydantic model used by the model router
# ---------------------------------------------------------------------------

class RoutingDecision(BaseModel):
    model: Literal["local", "cloud"] = Field(
        description="Which inference backend to use"
    )
    model_name: str = Field(
        description="Specific model identifier"
    )
    reason: str = Field(
        description="One-line reason for the routing decision"
    )
    estimated_tokens: int = Field(
        default=0,
        description="Rough token count of the incoming prompt"
    )
    confidence: float = Field(
        default=1.0,
        description="Classifier confidence 0..1 for the task-type decision"
    )
    runner_up: str | None = Field(
        default=None,
        description="Second-place task type, if any"
    )
    decided_by: str = Field(
        default="heuristic",
        description="How the decision was reached (heuristic | embedding | low_confidence_default)"
    )


# ---------------------------------------------------------------------------
# Agent graph state
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    # Accumulated conversation messages (LangGraph reducer)
    messages: Annotated[list, add_messages]

    # Original user query (never mutated)
    query: str

    # Routing decision populated by the router node
    routing: RoutingDecision | None

    # Retrieved context chunks from RAG layer
    context_chunks: list[dict[str, Any]]

    # Assembled context (selected chunks + rendered prompt text + stats)
    assembled_context: dict[str, Any]

    # Structured output payload built by the synthesis node
    output_payload: dict[str, Any]

    # Which sub-agents were invoked this cycle
    agents_used: list[str]

    # Error accumulator
    errors: list[str]

    # Per-request structured trace (orchestrator.trace.RequestTrace)
    trace: Any

    # Optional caller-supplied request id (carried into the trace + response)
    request_id: str

    # Terminal flag set by the end node
    finished: bool
