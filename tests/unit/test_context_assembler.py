"""Unit tests for context assembly: dedupe, diversity, recency, compression, render."""

from __future__ import annotations

import importlib
from datetime import datetime, timezone

from orchestrator import context_assembler as ca


def test_dedupe_drops_near_duplicates(sample_chunks):
    out = ca.assemble(sample_chunks, query="diagnostic sprint")
    kept_ids = {c["chunk_id"] for c in out["chunks"]}
    # c1 and c2 are near-duplicates; only one survives.
    assert not ({"c1", "c2"}).issubset(kept_ids)
    assert out["stats"]["dropped_duplicates"] >= 1


def test_per_file_cap(monkeypatch):
    monkeypatch.setenv("CONTEXT_MAX_PER_FILE", "1")
    monkeypatch.setenv("CONTEXT_DEDUPE_THRESHOLD", "0.99")  # disable dedupe for this test
    importlib.reload(ca)
    try:
        chunks = [
            {"text": f"distinct sentence number {i} about teaching", "file": "same.md",
             "chunk_id": f"c{i}", "score": 1.0 - i * 0.1, "collection": "k"}
            for i in range(4)
        ]
        out = ca.assemble(chunks, query="teaching")
        assert out["stats"]["kept_count"] == 1
        assert out["stats"]["dropped_diversity"] >= 1
    finally:
        importlib.reload(ca)


def test_recency_reorders(monkeypatch):
    monkeypatch.setenv("CONTEXT_RECENCY_WEIGHT", "5.0")     # exaggerate recency
    monkeypatch.setenv("CONTEXT_RECENCY_HALFLIFE_DAYS", "30")
    importlib.reload(ca)
    try:
        now = datetime(2026, 6, 1, tzinfo=timezone.utc)
        chunks = [
            {"text": "older but higher base score about alpha", "file": "a.md",
             "chunk_id": "old", "score": 0.9, "modified_at": "2020-01-01T00:00:00+00:00", "collection": "k"},
            {"text": "newer with lower base score about beta", "file": "b.md",
             "chunk_id": "new", "score": 0.7, "modified_at": "2026-05-30T00:00:00+00:00", "collection": "k"},
        ]
        out = ca.assemble(chunks, query="alpha beta", now=now)
        first = out["chunks"][0]["chunk_id"]
        assert first == "new"  # recency boost overtakes the higher base score
    finally:
        importlib.reload(ca)


def test_compression_respects_budget_and_keeps_citation():
    big = " ".join([f"sentence{i} about teaching quality and assessment." for i in range(200)])
    chunks = [{"text": big, "file": "big.md", "chunk_id": "b1", "score": 0.9, "collection": "k"}]
    out = ca.assemble(chunks, query="assessment", token_budget=100)
    assert out["stats"]["compressed"] is True
    assert out["stats"]["tokens_after"] <= out["stats"]["tokens_before"]
    assert out["chunks"][0]["citation_index"] == 1


def test_render_numbers_and_labels(sample_chunks):
    out = ca.assemble(sample_chunks, query="diagnostic sprint")
    rendered = out["rendered"]
    assert rendered.startswith("[1]")
    assert "kb/" in rendered


def test_extract_citation_indices():
    assert ca.extract_citation_indices("Per [1] and [3], also [1] again.") == [1, 3]
    assert ca.extract_citation_indices("no citations here") == []
