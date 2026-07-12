"""Unit tests for the routing classifier and decision logging."""

from __future__ import annotations

import importlib

from orchestrator import classifier


def test_code_query_classified_as_code():
    res = classifier.classify("Write code to debug this python function with a stack trace")
    assert res.task_type == "code"
    assert res.confidence > 0.0
    assert res.decided_by in ("heuristic", "embedding")


def test_reasoning_query_classified_as_reasoning():
    res = classifier.classify("Compare these two options and explain why one is better, with pros and cons")
    assert res.task_type == "reasoning"


def test_long_context_override():
    res = classifier.classify("anything at all", input_tokens=40_000)
    assert res.task_type == "long_context"
    assert res.decided_by == "long_context"
    assert res.confidence == 1.0


def test_empty_query_falls_back_to_default():
    res = classifier.classify("")
    assert res.task_type == classifier.DEFAULT_TASK
    assert res.decided_by == "low_confidence_default"
    assert res.confidence == 0.0


def test_confidence_and_margin_present():
    res = classifier.classify("draft a blog post and an email newsletter")
    d = res.to_dict()
    assert set(["task_type", "confidence", "runner_up", "margin", "decided_by", "scores"]).issubset(d)
    assert 0.0 <= d["confidence"] <= 1.0


def test_low_margin_defaults(monkeypatch):
    # Force a high margin requirement so any close call defaults.
    monkeypatch.setenv("ROUTER_MIN_MARGIN", "0.99")
    importlib.reload(classifier)
    try:
        res = classifier.classify("write code and draft a blog post")  # mixes code + creative
        assert res.decided_by == "low_confidence_default"
    finally:
        monkeypatch.delenv("ROUTER_MIN_MARGIN", raising=False)
        importlib.reload(classifier)


def test_decision_log_writes_line(tmp_path, monkeypatch):
    log = tmp_path / "routing_decisions.jsonl"
    monkeypatch.setenv("ROUTING_LOG_PATH", str(log))
    import orchestrator.decision_log as dl
    importlib.reload(dl)
    dl.log_decision("task_route", "hello world", {"backend": "local", "task_type": "advisory"})
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert "task_route" in lines[0]
    recent = dl.read_decisions(10)
    assert recent and recent[0]["kind"] == "task_route"
    importlib.reload(dl)
