"""
GET /status/summary: one call for the phone, every check guarded.

All collectors are stubbed; the test is about shaping and about a failing or
slow check never taking the whole summary down with it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import status_summary as ss


def _client():
    app = FastAPI()
    app.include_router(ss.router)
    return TestClient(app)


def _stub(monkeypatch, **overrides):
    now = datetime.now(timezone.utc)
    defaults = {
        "_deep_health": {"status": "degraded", "checks": {
            "qdrant": {"name": "qdrant", "ok": True},
            "ollama": {"name": "ollama", "ok": False, "error": "connection refused"},
            "search": {"name": "search", "ok": True}}},
        "_daemon": {"paused": False, "cycles": 42, "interval_sec": 60,
                    "last_heartbeat": (now - timedelta(seconds=30)).isoformat(timespec="seconds"),
                    "budget": {"enabled": True, "spent_usd": 0.4, "budget_usd": 2.0, "level": "ok", "extra": 1}},
        "_voice_engines": {"mode": "proxy", "whisper": {"available": True}, "vosk": {"available": True}},
        "_tts": {"engines": {"kokoro": {"available": True, "rtf": 0.64}, "elevenlabs": {"available": False}}},
        "_gates": [{"gate": "client_sensitive", "target_id": "p1"}],
        "_proposals": [{"id": "h1"}, {"id": "h2"}],
        "_spend": {"today_usd": 0.0123, "calls_today": 7},
        "_drafting": 1,
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        if callable(value) and not isinstance(value, dict):
            monkeypatch.setattr(ss, name, value)
        else:
            async def fn(v=value):
                return v
            monkeypatch.setattr(ss, name, fn)


def test_summary_shapes_everything_the_phone_needs(monkeypatch):
    _stub(monkeypatch)
    d = _client().get("/status/summary").json()
    assert d["overall"] == "degraded"
    names = {s["name"]: s for s in d["services"]}
    assert names["Orchestrator"]["ok"] is True
    assert names["Local models (Ollama)"]["ok"] is False and "refused" in names["Local models (Ollama)"]["detail"]
    assert d["daemon"]["state"] == "running" and d["daemon"]["cycles"] == 42
    assert "extra" not in d["daemon"]["budget"]
    assert d["voice"]["ok"] and "Whisper + VOSK" in d["voice"]["detail"] and "0.64x" in d["voice"]["detail"]
    assert d["spend"] == {"today_usd": 0.0123, "calls_today": 7}
    assert d["queue"] == {"gates": 1, "proposals": 2, "total": 3}
    assert d["outputs"] == {"drafting": 1}


def test_a_failing_check_greys_its_row_only(monkeypatch):
    async def boom():
        raise RuntimeError("daemon store locked")

    async def slow():
        await asyncio.sleep(10)

    monkeypatch.setattr(ss, "CHECK_TIMEOUT_S", 0.2)
    _stub(monkeypatch, _daemon=boom, _proposals=slow)
    d = _client().get("/status/summary").json()
    assert d["daemon"]["state"] == "unknown" and "locked" in d["daemon"]["detail"]
    assert d["queue"] == {"gates": 1, "proposals": None, "total": 1}
    assert d["services"][0]["ok"] is True


def test_stale_heartbeat_reads_offline_and_paused_reads_paused(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    _stub(monkeypatch, _daemon={"paused": False, "last_heartbeat": old, "interval_sec": 60})
    assert _client().get("/status/summary").json()["daemon"]["state"] == "offline"
    fresh = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _stub(monkeypatch, _daemon={"paused": True, "last_heartbeat": fresh, "interval_sec": 60})
    assert _client().get("/status/summary").json()["daemon"]["state"] == "paused"


def test_qdrant_down_is_down_not_degraded(monkeypatch):
    _stub(monkeypatch, _deep_health={"checks": {"qdrant": {"ok": False, "error": "timeout"}}})
    assert _client().get("/status/summary").json()["overall"] == "down"


def test_no_voice_service_says_browser_voice(monkeypatch):
    _stub(monkeypatch, _voice_engines={"mode": "unavailable"}, _tts={"mode": "unavailable", "engines": {}})
    v = _client().get("/status/summary").json()["voice"]
    assert v["ok"] is False and "no speech recognition" in v["detail"] and "browser voice only" in v["detail"]


def test_spend_today_reads_only_today_from_the_ledger(tmp_path, monkeypatch):
    from orchestrator import cost_tracker

    log = tmp_path / "cost_log.jsonl"
    now = datetime.now().timestamp()
    rows = [{"timestamp": now - 86400 * 2, "cost_usd": 5.0}, {"timestamp": now - 5, "cost_usd": 0.25},
            {"timestamp": now - 1, "cost_usd": 0.5}]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
    monkeypatch.setattr(cost_tracker, "LOG_PATH", log)
    assert ss._spend_today() == {"today_usd": 0.75, "calls_today": 2}
