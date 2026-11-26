"""The daemon reindexes the knowledge base once a day."""

from __future__ import annotations

import httpx
import pytest
import respx


@pytest.fixture()
def daemon(monkeypatch):
    import orchestrator.daemon as d
    logged, notes = [], []
    monkeypatch.setattr(d, "save_state", lambda state: None)
    monkeypatch.setattr(d, "_log_decision", lambda action, detail: logged.append((action, detail)))

    async def _notify(title, body, **kw):  # kind/view since 26 Nov 2026 (phone push)
        notes.append(title)
    monkeypatch.setattr(d, "_notify", _notify)
    monkeypatch.setattr(d, "KB_REINDEX_HOUR", 0)
    monkeypatch.setattr(d, "KB_REINDEX_TARGETS", ("/index",))
    monkeypatch.setenv("INDEXER_URL", "http://indexer.test")
    d._test_logged, d._test_notes = logged, notes
    return d


@respx.mock
async def test_reindex_runs_once_a_day(daemon):
    route = respx.post("http://indexer.test/index").mock(return_value=httpx.Response(
        200, json={"status": "ok", "notes_indexed": 420, "chunks_total": 4300, "collection": "obsidian_vault"}))
    state: dict = {}
    await daemon._maybe_reindex(state)
    await daemon._maybe_reindex(state)          # same day: no second run
    assert route.call_count == 1
    assert state["reindexed_date"]
    action, detail = daemon._test_logged[0]
    assert action == "kb_reindex"
    assert detail["/index"]["notes_indexed"] == 420
    assert daemon._test_notes == []


@respx.mock
async def test_reindex_failure_is_logged_and_notified(daemon):
    respx.post("http://indexer.test/index").mock(return_value=httpx.Response(500))
    state: dict = {}
    await daemon._maybe_reindex(state)
    assert daemon._test_logged[0][0] == "kb_reindex_error"
    assert daemon._test_notes == ["Knowledge base reindex failed"]
    assert state["reindexed_date"]              # does not retry every minute


async def test_reindex_off_when_hour_negative(daemon, monkeypatch):
    monkeypatch.setattr(daemon, "KB_REINDEX_HOUR", -1)
    state: dict = {}
    await daemon._maybe_reindex(state)
    assert state == {}
