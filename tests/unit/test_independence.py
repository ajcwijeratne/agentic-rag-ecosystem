"""Offline tests for the independence layer: daemon, budget breaker, inbox."""

from __future__ import annotations

import asyncio
import importlib
import json
import time

import pytest


# ---------------------------------------------------------------------------
# Fixtures: isolated DB, state, and cost ledger per test
# ---------------------------------------------------------------------------

@pytest.fixture()
def iso_env(tmp_path, monkeypatch):
    """Point every persistent path at tmp and reload the touched modules."""
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("DAEMON_STATE_PATH", str(tmp_path / "daemon_state.json"))
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "0")
    from orchestrator import operating
    importlib.reload(operating)
    yield tmp_path


# ---------------------------------------------------------------------------
# Budget breaker
# ---------------------------------------------------------------------------

def _write_ledger(path, entries):
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_budget_disabled_when_unset(monkeypatch, tmp_path):
    from orchestrator import cost_tracker
    monkeypatch.setattr(cost_tracker, "LOG_PATH", tmp_path / "cost.jsonl")
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "0")
    st = cost_tracker.budget_status()
    assert st["enabled"] is False
    assert st["level"] == "ok"


def test_budget_warn_and_stop(monkeypatch, tmp_path):
    from orchestrator import cost_tracker
    ledger = tmp_path / "cost.jsonl"
    monkeypatch.setattr(cost_tracker, "LOG_PATH", ledger)
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "10")

    now = time.time()
    _write_ledger(ledger, [{"timestamp": now, "cost_usd": 8.5}])
    st = cost_tracker.budget_status()
    assert st["level"] == "warn"
    assert st["enabled"] is True

    _write_ledger(ledger, [{"timestamp": now, "cost_usd": 8.5},
                           {"timestamp": now, "cost_usd": 2.0}])
    st = cost_tracker.budget_status()
    assert st["level"] == "stop"

    # Spend from a previous month does not count.
    _write_ledger(ledger, [{"timestamp": now - 40 * 86400, "cost_usd": 99.0}])
    st = cost_tracker.budget_status()
    assert st["level"] == "ok"


# ---------------------------------------------------------------------------
# Inbox classification
# ---------------------------------------------------------------------------

def test_inbox_classification():
    from orchestrator.inbox import classify_inbox
    assert classify_inbox("approve external_publish abc-123") == "approval"
    assert classify_inbox("Reject paid_job xyz money reasons") == "approval"
    assert classify_inbox("plan: launch the TEQSA briefing series") == "plan"
    assert classify_inbox("What did we tell Swinburne about the MBA redesign?") == "ask"
    assert classify_inbox("Draft a LinkedIn post on adaptive leadership") == "task"
    # Mode override wins.
    assert classify_inbox("anything at all", mode="ask") == "ask"


def test_inbox_rejects_unknown_gate(iso_env):
    from fastapi import HTTPException
    from orchestrator import inbox
    msg = inbox.InboxMessage(channel="test", sender="t", text="approve not_a_gate abc")
    with pytest.raises(HTTPException):
        inbox._handle_approval(msg)


# ---------------------------------------------------------------------------
# Daemon: state, pause, and the human-gate rule
# ---------------------------------------------------------------------------

def test_daemon_pause_resume_persists(iso_env, monkeypatch):
    from orchestrator import daemon
    monkeypatch.setattr(daemon, "STATE_PATH", iso_env / "daemon_state.json")
    daemon.pause(actor="test")
    assert daemon.load_state()["paused"] is True
    daemon.resume(actor="test")
    assert daemon.load_state()["paused"] is False


def test_cycle_never_executes_approval_tasks(iso_env, monkeypatch):
    """An approval task must produce a notification, never an execution."""
    from orchestrator import daemon, operating
    monkeypatch.setattr(daemon, "STATE_PATH", iso_env / "daemon_state.json")
    monkeypatch.setattr(daemon, "LOG_PATH", iso_env / "daemon.jsonl")

    executed = []

    async def fake_run_task(task):
        executed.append(task["task_id"])
        return {"ok": True}

    notifications = []

    async def fake_notify(title, body):
        notifications.append(title)

    monkeypatch.setattr(daemon, "run_task", fake_run_task)
    monkeypatch.setattr(daemon, "_notify", fake_notify)

    operating.add_task(None, "Approve external_publish for launch video",
                       type="approval", status="waiting_approval", priority=5)

    state = daemon.load_state()
    summary = asyncio.run(daemon.run_cycle(state))
    assert summary["action"] == "waiting_on_human"
    assert executed == []
    assert notifications, "operator was not notified about the waiting approval"


def test_cycle_persists_notify_once_marker(iso_env, monkeypatch):
    from orchestrator import daemon, operating
    monkeypatch.setattr(daemon, "STATE_PATH", iso_env / "daemon_state.json")
    monkeypatch.setattr(daemon, "LOG_PATH", iso_env / "daemon.jsonl")

    notifications = []

    async def fake_notify(title, body):
        notifications.append(title)

    monkeypatch.setattr(daemon, "_notify", fake_notify)
    operating.add_task(
        None,
        "Approve external_publish for launch video",
        type="approval",
        status="waiting_approval",
        priority=5,
    )

    first = asyncio.run(daemon.run_cycle(daemon.load_state()))
    second = asyncio.run(daemon.run_cycle(daemon.load_state()))

    assert first["action"] == "waiting_on_human"
    assert second["action"] == "waiting_on_human"
    assert notifications == ["Waiting on you"]
    assert daemon.load_state()["notified_tasks"]


def test_cycle_executes_agent_task_and_marks_done(iso_env, monkeypatch):
    from orchestrator import daemon, operating
    monkeypatch.setattr(daemon, "STATE_PATH", iso_env / "daemon_state.json")
    monkeypatch.setattr(daemon, "LOG_PATH", iso_env / "daemon.jsonl")
    monkeypatch.setattr(daemon, "DRY_RUN", False)

    async def fake_run_task(task):
        return {"ok": True, "output": "the work"}

    async def fake_notify(title, body):
        pass

    monkeypatch.setattr(daemon, "run_task", fake_run_task)
    monkeypatch.setattr(daemon, "_notify", fake_notify)

    task_id = operating.add_task(None, "Research TEQSA update", type="agent", priority=4)
    summary = asyncio.run(daemon.run_cycle(daemon.load_state()))
    assert summary["action"] == "done"
    task = [t for t in operating.list_tasks(limit=10) if t["task_id"] == task_id][0]
    assert task["status"] == "done"
    assert "the work" in (task["note"] or "")


def test_cycle_blocks_after_two_failures(iso_env, monkeypatch):
    from orchestrator import daemon, operating
    monkeypatch.setattr(daemon, "STATE_PATH", iso_env / "daemon_state.json")
    monkeypatch.setattr(daemon, "LOG_PATH", iso_env / "daemon.jsonl")
    monkeypatch.setattr(daemon, "DRY_RUN", False)

    async def failing_run_task(task):
        return {"ok": False, "error": "boom"}

    notifications = []

    async def fake_notify(title, body):
        notifications.append(title)

    monkeypatch.setattr(daemon, "run_task", failing_run_task)
    monkeypatch.setattr(daemon, "_notify", fake_notify)

    task_id = operating.add_task(None, "Doomed task", type="agent", priority=4)

    s1 = asyncio.run(daemon.run_cycle(daemon.load_state()))
    assert s1["action"] == "retry_queued"
    s2 = asyncio.run(daemon.run_cycle(daemon.load_state()))
    assert s2["action"] == "blocked"

    task = [t for t in operating.list_tasks(limit=10) if t["task_id"] == task_id][0]
    assert task["status"] == "blocked"
    assert any("blocked" in n.lower() for n in notifications)


def test_budget_stop_blocks_paid_dispatch(iso_env, monkeypatch):
    from orchestrator import daemon, operating
    monkeypatch.setattr(daemon, "STATE_PATH", iso_env / "daemon_state.json")
    monkeypatch.setattr(daemon, "LOG_PATH", iso_env / "daemon.jsonl")
    monkeypatch.setattr(daemon, "DRY_RUN", False)
    monkeypatch.setattr(daemon, "budget_status", lambda: {
        "month": "2026-07", "spent_usd": 60.0, "budget_usd": 50.0,
        "ratio": 1.2, "level": "stop", "enabled": True,
    })

    async def fake_run_task(task):  # must never be reached
        raise AssertionError("dispatched despite budget stop")

    monkeypatch.setattr(daemon, "run_task", fake_run_task)
    operating.add_task(None, "Expensive research", type="agent", priority=4)
    summary = asyncio.run(daemon.run_cycle(daemon.load_state()))
    assert summary["action"] == "budget_stop"
