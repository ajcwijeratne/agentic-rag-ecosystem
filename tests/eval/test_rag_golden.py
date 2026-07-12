"""Golden-answer RAG evals (live).

Runs each WijerCo golden question through the full hybrid pipeline and scores the
answer on expected facts plus Aaron's deterministic style check. Skipped unless
Qdrant and a model are reachable. Needs the wijerco_knowledge collection indexed.
"""

from __future__ import annotations

import os

import pytest

from harness.golden import GOLDEN


def _live_or_skip():
    qdrant = os.getenv("QDRANT_URL", "http://localhost:6333")
    try:
        import httpx
        if httpx.get(f"{qdrant}/healthz", timeout=1.0).status_code >= 500:
            pytest.skip("qdrant not reachable")
    except Exception:
        pytest.skip("qdrant not reachable")


def _facts_present(answer: str, item) -> bool:
    a = answer.lower()
    if item.expected_all and not all(s.lower() in a for s in item.expected_all):
        return False
    if item.expected_any and not any(s.lower() in a for s in item.expected_any):
        return False
    return True


@pytest.mark.live
@pytest.mark.parametrize("item", GOLDEN, ids=[g.id for g in GOLDEN])
def test_golden_answer(item):
    _live_or_skip()
    import asyncio
    from orchestrator.fallback_chain import call_with_fallback
    from rag.retriever import search
    from orchestrator.context_assembler import assemble, CITATION_SYSTEM_PROMPT
    from harness.eval_suite import deterministic_score

    loop = asyncio.get_event_loop()
    chunks = loop.run_until_complete(search(item.question, top_k=6, collection="wijerco_knowledge"))
    assembled = assemble(chunks, query=item.question)
    user = f"Context:\n{assembled['rendered']}\n\nQuery: {item.question}"
    resp = loop.run_until_complete(call_with_fallback(
        user_message=user, system_prompt=CITATION_SYSTEM_PROMPT, max_tier=1))

    answer = resp.content or ""
    assert _facts_present(answer, item), f"[{item.id}] missing expected facts. Answer: {answer[:300]}"
    # Style: deterministic score should not be terrible.
    assert deterministic_score(answer).score >= 0.5, f"[{item.id}] weak style score"
