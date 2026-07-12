"""Retrieval recall tests.

Offline: seed a fake BM25 corpus from the recall set and assert recall@k over
the sparse path, no live service. Live: run the same queries against the indexed
wijerco_knowledge collection in Qdrant (skipped unless reachable).
"""

from __future__ import annotations

import os

import pytest

from harness.recall_set import RECALL_CASES


def _seed_corpus():
    """Build a small BM25 corpus, one document per expected file, plus distractors."""
    from rag import retriever
    texts, payloads = [], []
    # One representative document per expected file, using the query terms as content.
    docs = {
        "KNOWLEDGE BASE/wijerco-diagnostic-sprint.md":
            "diagnostic sprint price cost timeline two week audit four thousand dollars deliverables roadmap",
        "KNOWLEDGE BASE/wijerco-services.md":
            "free triage call entry point new client core services curriculum learning design list",
        "KNOWLEDGE BASE/wijerco-positioning.md":
            "wijerco positioning teaching quality faculty workload adopt ai safely one line position",
        "KNOWLEDGE BASE/wijerco-sector-context.md":
            "teqsa teaching qualification forcing function sector context research prestige international revenue",
        "KNOWLEDGE BASE/wijerco-competitors.md":
            "competitors deloitte kpmg pwc ey advisory firms market landscape how wijerco wins specificity",
        "AGENTS/departments/learning-design.md":
            "learning design department curriculum framework course module content production arm learning products",
        "AGENTS/departments/support.md":
            "support department client communication incoming requests scheduling inbox relationships touchpoint",
        "AGENTS/departments/operations.md":
            "operations department project management reporting finance recruitment planning business tracking",
        "AGENTS/subagents/sales-manager.md":
            "sales manager business development pipeline prospective clients outreach proposals closing strategy",
        "AGENTS/subagents/instructional-designer.md":
            "instructional designer structure sequence learning outcomes assessment design pedagogical framework architect design brief",
        "AGENTS/subagents/research-analyst.md":
            "research analyst literature review published research sector sources evidence",
        "ABOUT ME/about-me.md":
            "aaron wijeratne academic director oes swinburne online phd organisational behaviour melbourne education leader",
        "ABOUT ME/my-company.md":
            "goals pro vice-chancellor academic dean twelve month horizon thought leadership linkedin sector publications",
        "ABOUT ME/anti-ai-writing-style.md":
            "banned words em dashes writing style ai tells buzzwords constructions openings closings tone rules",
    }
    for f, t in docs.items():
        texts.append(t)
        payloads.append({"file": f, "text": t, "chunk_id": f, "collection": "wijerco_knowledge"})
    # Distractors.
    for i in range(5):
        texts.append(f"unrelated filler document number {i} about weather and sport")
        payloads.append({"file": f"distractor{i}.md", "text": texts[-1], "collection": "wijerco_knowledge"})
    return retriever, texts, payloads


def test_offline_bm25_recall_at_3():
    pytest.importorskip("rank_bm25")
    retriever, texts, payloads = _seed_corpus()
    retriever.update_bm25_corpus(texts, payloads, collection="wijerco_knowledge")

    hits = 0
    for case in RECALL_CASES:
        results = retriever._bm25_search(case.query, top_k=3, collection="wijerco_knowledge")
        got_files = {r.get("file") for r in results}
        if any(f in got_files for f in case.expected_files):
            hits += 1
    recall = hits / len(RECALL_CASES)
    assert recall >= 0.8, f"offline BM25 recall@3 too low: {recall:.2%}"


@pytest.mark.live
def test_live_recall_against_qdrant():
    import asyncio
    qdrant = os.getenv("QDRANT_URL", "http://localhost:6333")
    try:
        import httpx
        if httpx.get(f"{qdrant}/healthz", timeout=1.0).status_code >= 500:
            pytest.skip("qdrant not reachable")
    except Exception:
        pytest.skip("qdrant not reachable")

    from rag.retriever import search
    hits = 0
    for case in RECALL_CASES:
        results = asyncio.get_event_loop().run_until_complete(
            search(case.query, top_k=5, collection="wijerco_knowledge"))
        got_files = {r.get("file") for r in results}
        if any(f in got_files for f in case.expected_files):
            hits += 1
    recall = hits / len(RECALL_CASES)
    assert recall >= 0.6, f"live recall@5 too low: {recall:.2%}"
