"""The routing panel reads the labelled set in the shape tune_router reads it."""

from __future__ import annotations

import json


def _write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_labelled_set_with_suggested_field_is_read(tmp_path, monkeypatch):
    import orchestrator.router_eval as re_
    labels = tmp_path / "router_labels.jsonl"
    _write(labels, [
        {"query": "Draft a LinkedIn post", "suggested": "creative", "certainty": "high"},
        {"query": "Summarise this", "gold": "summary"},
    ])
    monkeypatch.setattr(re_, "LABELS_PATH", labels)
    assert re_.load_labels() == [
        {"query": "Draft a LinkedIn post", "label": "creative"},
        {"query": "Summarise this", "label": "summary"},
    ]


def test_template_row_needs_a_human_label(tmp_path, monkeypatch):
    import orchestrator.router_eval as re_
    labels = tmp_path / "router_labels.jsonl"
    _write(labels, [
        {"query": "unlabelled", "suggested": "advisory", "label": ""},
        {"query": "labelled", "suggested": "advisory", "label": "reasoning"},
    ])
    monkeypatch.setattr(re_, "LABELS_PATH", labels)
    assert re_.load_labels() == [{"query": "labelled", "label": "reasoning"}]

