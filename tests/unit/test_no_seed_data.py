"""Live views show real data or nothing, never invented rows (21 Nov 2026)."""

from __future__ import annotations

import json


async def test_trace_recent_reads_the_real_trace_store(tmp_path, monkeypatch):
    import orchestrator.trace as trace
    import orchestrator.dashboard as dash
    log = tmp_path / "traces.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in [
        {"query": "older", "model_label": "DeepSeek V4 Flash", "final_confidence": 0.5,
         "source_count": 3, "total_latency_ms": 1500.4, "cost_usd": 0.0002, "task_type": "retrieval"},
        {"query": "newer", "model_label": "qwen3:4b-instruct (local)", "final_confidence": 0.8,
         "source_count": 8, "total_latency_ms": 76000.0, "cost_usd": 0.0},
    ]) + "\n", encoding="utf-8")
    monkeypatch.setattr(trace, "_LOG_PATH", log)
    rows = (await dash.trace_recent(limit=10))["items"]
    assert [r["q"] for r in rows] == ["newer", "older"]
    assert rows[0] == {"q": "newer", "dept": "", "model": "qwen3:4b-instruct (local)", "conf": 80,
                       "sources": 8, "ms": 76000, "cost": 0.0, "ts": None}
    assert rows[1]["dept"] == "retrieval"


async def test_trace_recent_is_empty_without_traces(tmp_path, monkeypatch):
    import orchestrator.trace as trace
    import orchestrator.dashboard as dash
    monkeypatch.setattr(trace, "_LOG_PATH", tmp_path / "none.jsonl")
    assert (await dash.trace_recent())["items"] == []


async def test_note_backed_views_are_empty_not_seeded(monkeypatch):
    import orchestrator.dashboard as dash
    monkeypatch.setattr(dash, "_load_items", lambda folder: [])
    assert (await dash.engagements())["items"] == []
    assert (await dash.intel_feed())["items"] == []
    assert (await dash.memory_overview())["groups"] == []
