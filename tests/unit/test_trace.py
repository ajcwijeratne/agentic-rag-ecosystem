"""Unit tests for the per-request trace."""

from __future__ import annotations

import importlib
import json


def _fresh_trace_module(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_LOG_PATH", str(tmp_path / "traces.jsonl"))
    import orchestrator.trace as tr
    importlib.reload(tr)
    return tr


def test_trace_records_spans_and_events(tmp_path, monkeypatch):
    tr = _fresh_trace_module(tmp_path, monkeypatch)
    t = tr.new_trace(query="hello")
    t.start_span("route")
    t.end_span("route")
    t.add_event("model_fallback", provider="deepseek", attempt=1)
    t.update(backend="cloud", retrieval_count=3)
    rec = t.finish()

    assert rec["query"] == "hello"
    assert rec["backend"] == "cloud"
    assert rec["retrieval_count"] == 3
    assert "route" in rec["spans"]
    assert rec["events"][0]["kind"] == "model_fallback"
    assert rec["total_latency_ms"] >= 0


def test_trace_writes_one_line_and_reads_back(tmp_path, monkeypatch):
    tr = _fresh_trace_module(tmp_path, monkeypatch)
    tr.new_trace(query="q1").finish()
    tr.new_trace(query="q2").finish()
    traces = tr.read_traces(10)
    assert len(traces) == 2
    assert traces[0]["query"] == "q2"  # newest first

    path = tmp_path / "traces.jsonl"
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    json.loads(lines[0])  # valid JSON


def test_end_span_without_start_is_safe(tmp_path, monkeypatch):
    tr = _fresh_trace_module(tmp_path, monkeypatch)
    t = tr.new_trace()
    t.end_span("never_started")  # must not raise
    rec = t.finish()
    assert "never_started" not in rec["spans"]
