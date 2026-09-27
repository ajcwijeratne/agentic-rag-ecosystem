"""
LangGraph state machine — Central Orchestrator.
Updated to use the multi-LLM optimizer instead of a single hardcoded model.

Node execution order:
  START
    └─► route_node          (detect task type + pick optimal model)
          └─► rag_node      (retrieve context via MCP sub-agents)
                └─► llm_node  (generate answer with the chosen model)
                      └─► synthesize_node  (build structured JSON payload)
                            └─► END
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx
from langgraph.graph import END, START, StateGraph

from .cost_tracker import tracker as cost_tracker
from .multi_llm import call_model
from .fallback_chain import call_with_fallback
from .router import route_query
from .state import AgentState
from .token_optimizer import pick_model, classify_task, rough_token_count
from .context_assembler import (
    assemble,
    CITATION_SYSTEM_PROMPT,
    extract_citation_indices,
)


# ─────────────────────────────────────────────────────────────────────────────
# Nodes
# ─────────────────────────────────────────────────────────────────────────────

def route_node(state: AgentState) -> AgentState:
    """Creates the request trace, then runs the model router."""
    from .trace import new_trace
    trace = state.get("trace") or new_trace(
        query=state.get("query", ""), request_id=state.get("request_id") or None
    )
    trace.start_span("route")
    new_state = route_query(state)
    routing = new_state.get("routing")
    trace.end_span("route")
    if routing is not None:
        trace.update(
            backend=routing.model,
            model_name=routing.model_name,
            task_type=getattr(routing, "decided_by", ""),
            routing_confidence=getattr(routing, "confidence", None),
        )
    return {**new_state, "trace": trace, "request_id": trace.request_id}


async def _fetch_agent(name: str, base: str, query: str) -> tuple[str, list[dict], str | None, float]:
    """Call one sub-agent retrieve endpoint.
    Returns (name, chunks, error_or_None, latency_ms)."""
    import time
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base}/retrieve",
                json={"query": query, "top_k": int(os.getenv("RAG_RETRIEVE_TOP_K", "12"))},
                timeout=15.0,
            )
        resp.raise_for_status()
        chunks = []
        for c in resp.json().get("chunks", []):
            c["source_agent"] = name
            chunks.append(c)
        return name, chunks, None, (time.perf_counter() - t0) * 1000
    except Exception as exc:
        return name, [], f"[{name}] {exc}", (time.perf_counter() - t0) * 1000


AGENT_ENDPOINTS = {
    "local_data": ("LOCAL_DATA_AGENT_URL", "http://localhost:8001"),
    "search":     ("SEARCH_AGENT_URL",     "http://localhost:8002"),
    "cloud":      ("CLOUD_AGENT_URL",      "http://localhost:8003"),
}


async def gather_agents(query: str, timeout: float | None = None,
                        essential: tuple = (), essential_timeout: float = 6.0) -> list:
    """
    Query every retrieval agent in parallel and return their results.

    With `timeout`, agents that have not answered by the deadline are cancelled
    and reported as errors, and whatever did arrive is still returned. Without
    it, this waits for all of them — the behaviour written chat relies on.

    The distinction matters because the agents finish very unevenly: local_data
    returns useful chunks in about a second, while the cloud agent takes nearly
    four and, in this deployment, returns nothing. gather() waits for the
    slowest, so it set the pace for every spoken answer. Simply wrapping the
    whole gather in wait_for was worse than useless — being all-or-nothing, a
    short deadline produced no context at all rather than partial results, and
    spoken answers regressed to "I don't have any information about that".
    Cancelling per agent is what makes a deadline safe.

    A single deadline is still the wrong instrument, because the agents are not
    equally valuable. Measured here over five runs: local_data returns all
    twelve chunks every time at a median of 1.26s, while search (0.56s) and
    cloud (3.29s) return nothing at all. A blanket 2s deadline therefore became
    a coin flip on the only agent that mattered — it landed right on
    local_data's tail and dropped the vault answer one run in four.

    So `essential` agents are waited for up to `essential_timeout`, and
    everything else is held to `timeout`. The slow, empty agent stops setting
    the pace without ever risking the answer.
    """
    tasks = {
        asyncio.create_task(_fetch_agent(name, os.getenv(env, default), query)): name
        for name, (env, default) in AGENT_ENDPOINTS.items()
    }
    if timeout is None:
        await asyncio.gather(*tasks)
        done, pending = set(tasks), set()
    else:
        essential_tasks = {t for t, name in tasks.items() if name in essential}
        optional_tasks = set(tasks) - essential_tasks

        # Optional agents get the short deadline; essential ones get long enough
        # that they are effectively always waited for.
        done_opt, pend_opt = (await asyncio.wait(optional_tasks, timeout=timeout)
                              if optional_tasks else (set(), set()))
        done_ess, pend_ess = (await asyncio.wait(essential_tasks, timeout=essential_timeout)
                              if essential_tasks else (set(), set()))
        done, pending = done_opt | done_ess, pend_opt | pend_ess

    results = []
    for task in done:
        try:
            results.append(task.result())
        except Exception as exc:  # noqa: BLE001 — one agent must not sink the turn
            results.append((tasks[task], [], f"[{tasks[task]}] {exc}", 0.0))

    for task in pending:
        task.cancel()
        name = tasks[task]
        results.append((name, [], f"[{name}] timed out after {timeout}s", (timeout or 0) * 1000))

    return results


async def rag_node(state: AgentState) -> AgentState:
    """Calls all three FastMCP sub-agents IN PARALLEL via asyncio.gather()."""
    query  = state["query"]
    errors = list(state.get("errors", []))

    # No deadline: written answers wait for every agent, as before.
    results = await gather_agents(
        query,
        timeout=state.get("agent_timeout_s"),
        essential=tuple(state.get("essential_agents") or ()),
        essential_timeout=float(state.get("essential_timeout_s") or 6.0),
    )

    trace = state.get("trace")
    chunks:      list[dict] = []
    agents_used: list[str]  = []
    for name, agent_chunks, error, latency_ms in results:
        if error:
            errors.append(error)
            if trace:
                trace.spans[f"agent.{name}"] = {"ms": round(latency_ms, 2), "chunks": 0, "error": True}
        else:
            chunks.extend(agent_chunks)
            agents_used.append(name)
            if trace:
                trace.spans[f"agent.{name}"] = {"ms": round(latency_ms, 2), "chunks": len(agent_chunks)}

    # Deliberate context assembly: dedupe, diversify, recency-weight, compress.
    assembled = assemble(chunks, query=query)

    if trace:
        trace.update(
            agents_used=agents_used,
            retrieval_count=len(chunks),
            chunks_after_assembly=assembled.get("stats", {}).get("kept_count", 0),
            assembly_stats=assembled.get("stats", {}),
        )

    return {
        **state,
        "context_chunks":    chunks,
        "assembled_context": assembled,
        "agents_used":       agents_used,
        "errors":            errors,
    }


async def llm_node(state: AgentState) -> AgentState:
    """
    Calls call_model() with the task-optimal model.
    Builds a prompt from the query + retrieved context chunks.
    """
    query  = state["query"]
    errors = list(state.get("errors", []))

    assembled    = state.get("assembled_context", {}) or {}
    context_text = assembled.get("rendered", "")

    system_prompt = CITATION_SYSTEM_PROMPT
    user_message = (
        f"Context:\n{context_text}\n\nQuery: {query}"
        if context_text else query
    )

    # Respect forced model from state if set
    force_key = state.get("output_payload", {}).get("_force_model_key")

    trace = state.get("trace")
    if trace:
        trace.start_span("llm")

    response = await call_with_fallback(
        user_message    = user_message,
        system_prompt   = system_prompt,
        history         = [],
        force_model_key = force_key,
        trace           = trace,
    )

    if trace:
        trace.end_span("llm")
        trace.update(
            model_key=response.model_key,
            model_label=response.model_label,
            provider=response.provider,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            cost_usd=response.cost_usd,
        )

    # Record cost
    cost_tracker.record(
        model_key     = response.model_key,
        model_label   = response.model_label,
        provider      = response.provider,
        task_type     = response.task_type,
        input_tokens  = response.input_tokens,
        output_tokens = response.output_tokens,
        cost_usd      = response.cost_usd,
        latency_ms    = response.latency_ms,
        query         = query,
    )

    if response.error:
        errors.append(response.error)

    from langchain_core.messages import AIMessage, HumanMessage
    new_messages = list(state.get("messages", [])) + [
        HumanMessage(content=query),
        AIMessage(content=response.content),
    ]

    return {
        **state,
        "messages": new_messages,
        "errors":   errors,
        "output_payload": {
            **state.get("output_payload", {}),
            "_llm_response": {
                "model_key":    response.model_key,
                "model_label":  response.model_label,
                "provider":     response.provider,
                "task_type":    response.task_type,
                "cost_usd":     response.cost_usd,
                "latency_ms":   response.latency_ms,
                "input_tokens": response.input_tokens,
                "output_tokens":response.output_tokens,
            },
        },
    }


def synthesize_node(state: AgentState) -> AgentState:
    """Packages the final answer into a structured JSON output payload."""
    messages = state.get("messages", [])
    last_ai  = next(
        (m for m in reversed(messages) if hasattr(m, "type") and m.type == "ai"),
        None,
    )
    answer  = last_ai.content if last_ai else ""
    llm_meta = state.get("output_payload", {}).get("_llm_response", {})

    # Build the provenance trail. `sources` is one entry per chunk that went into
    # the prompt; `citations` maps the [n] markers the answer actually used.
    assembled       = state.get("assembled_context", {}) or {}
    assembled_chunks = assembled.get("chunks", [])
    from rag.schema import Chunk
    sources = []
    by_index: dict[int, dict] = {}
    for d in assembled_chunks:
        ref = Chunk.from_dict(d).source_ref()
        ref["citation_index"] = d.get("citation_index")
        sources.append(ref)
        if d.get("citation_index") is not None:
            by_index[d["citation_index"]] = ref

    used_indices = extract_citation_indices(answer)
    citations = [
        {"index": n, **by_index[n]}
        for n in used_indices if n in by_index
    ]

    # Final confidence: routing confidence scaled by whether the answer cited
    # any retrieved source. A cited answer is more trustworthy than an uncited one.
    routing = state.get("routing")
    routing_conf = getattr(routing, "confidence", 1.0) if routing else 1.0
    final_confidence = round(routing_conf * (1.0 if citations else 0.7), 4)

    trace = state.get("trace")
    request_id = state.get("request_id", "")
    if trace:
        trace.update(
            final_confidence=final_confidence,
            citation_count=len(citations),
            source_count=len(sources),
            errors=state.get("errors", []),
        )
        trace.finish()
        request_id = trace.request_id

    payload = {
        "request_id":     request_id,
        "final_confidence": final_confidence,
        "query":          state.get("query", ""),
        "answer":         answer,
        "model_key":      llm_meta.get("model_key", "unknown"),
        "model_label":    llm_meta.get("model_label", "unknown"),
        "provider":       llm_meta.get("provider", "unknown"),
        "task_type":      llm_meta.get("task_type", "unknown"),
        "cost_usd":       llm_meta.get("cost_usd", 0.0),
        "latency_ms":     llm_meta.get("latency_ms", 0),
        "input_tokens":   llm_meta.get("input_tokens", 0),
        "output_tokens":  llm_meta.get("output_tokens", 0),
        "agents_used":    state.get("agents_used", []),
        "context_count":  len(state.get("context_chunks", [])),
        "context_chunks": state.get("context_chunks", []),
        "assembly_stats": assembled.get("stats", {}),
        "sources":        sources,
        "citations":      citations,
        "errors":         state.get("errors", []),
    }

    return {**state, "output_payload": payload, "finished": True}


# ─────────────────────────────────────────────────────────────────────────────
# Graph compilation
# ─────────────────────────────────────────────────────────────────────────────

def build_graph() -> StateGraph:
    builder = StateGraph(AgentState)
    builder.add_node("route_node",      route_node)
    builder.add_node("rag_node",        rag_node)
    builder.add_node("llm_node",        llm_node)
    builder.add_node("synthesize_node", synthesize_node)
    builder.add_edge(START,             "route_node")
    builder.add_edge("route_node",      "rag_node")
    builder.add_edge("rag_node",        "llm_node")
    builder.add_edge("llm_node",        "synthesize_node")
    builder.add_edge("synthesize_node", END)
    return builder.compile()


graph = build_graph()
