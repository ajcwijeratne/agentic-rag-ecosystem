"""Recall check scores each case against the index its file lives in."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest


def _run(monkeypatch, tmp_path, built: set[str]):
    import orchestrator.dashboard as d

    calls: list[str] = []

    async def fake_search(query, top_k=5, collection=""):
        calls.append(collection)
        from harness.recall_set import RECALL_CASES
        case = next(c for c in RECALL_CASES if c.query == query)
        return [{"file": case.expected_files[0]}]

    async def fake_exists(name):
        return name in built

    monkeypatch.setitem(sys.modules, "rag.retriever", types.SimpleNamespace(search=fake_search))
    monkeypatch.setattr(d, "_kb_collection_exists", fake_exists)
    monkeypatch.setattr(d, "KB_QUALITY_PATH", tmp_path / "kb_quality.json")
    monkeypatch.delenv("WIJERCO_COLLECTION", raising=False)
    monkeypatch.delenv("WIJERCO_AGENTS_COLLECTION", raising=False)
    return asyncio.run(d.kb_quality_run()), calls


def test_agent_cases_skipped_when_agents_index_absent(monkeypatch, tmp_path):
    from harness.recall_set import RECALL_CASES
    out, calls = _run(monkeypatch, tmp_path, built={"wijerco_knowledge"})
    n_agents = sum(1 for c in RECALL_CASES if c.collection == "agents")
    assert n_agents == 6
    assert out["cases_skipped"] == n_agents
    assert out["cases_evaluated"] == len(RECALL_CASES) - n_agents
    assert out["cases_passed"] == out["cases_evaluated"]
    assert out["recall_at_5"] == 1.0
    assert set(calls) == {"wijerco_knowledge"}
    skipped = [c for c in out["cases"] if c.get("skipped")]
    assert all(c["pass"] is None and "wijerco_agents" in c["reason"] for c in skipped)


def test_agent_cases_scored_against_agents_index_when_built(monkeypatch, tmp_path):
    out, calls = _run(monkeypatch, tmp_path, built={"wijerco_knowledge", "wijerco_agents"})
    assert out["cases_skipped"] == 0
    assert calls.count("wijerco_agents") == 6
    assert out["recall_at_5"] == 1.0


def test_missing_knowledge_index_is_not_skipped(monkeypatch, tmp_path):
    # The knowledge index is required: its cases still run (and miss) when absent.
    out, calls = _run(monkeypatch, tmp_path, built=set())
    assert "wijerco_knowledge" in calls
    assert out["cases_evaluated"] == 9
