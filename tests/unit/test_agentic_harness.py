"""Offline tests for the autonomous execution harness: contract parsing,
department matching, tool gating, and daemon wiring (handoff + needs-decision).

No live LLM or n8n calls — run_autonomous_task and the tool loop are mocked
out wherever a test exercises the daemon around them.
"""

from __future__ import annotations

import asyncio
import importlib

import pytest

from orchestrator import agentic_harness as harness


# ---------------------------------------------------------------------------
# Return-contract parsing
# ---------------------------------------------------------------------------

_WELL_FORMED = """
I looked into this and here's where it landed.

TASK: t-123
FROM: research_intelligence
STATUS: complete
OUTPUT: Three competitors raised prices in Q3; summary attached.
SOURCES: public pricing pages, checked 2026-08-30
ASSUMPTIONS: none
RISKS: pricing pages may lag actual invoices
DECISION NEEDED: none
NEXT: Aaron
"""


def test_parse_return_contract_well_formed():
    fields = harness.parse_return_contract(_WELL_FORMED)
    assert fields["STATUS"] == "complete"
    assert fields["OUTPUT"].startswith("Three competitors")
    assert fields["NEXT"] == "Aaron"


def test_normalize_well_formed_is_ok_shaped():
    fields = harness.parse_return_contract(_WELL_FORMED)
    norm = harness._normalize(fields, _WELL_FORMED)
    assert norm["status"] == "complete"
    assert norm["decision_needed"] is None
    assert norm["next_raw"] == "Aaron"


def test_normalize_free_text_without_contract_assumes_complete():
    text = "Here's a draft paragraph with no contract block at all."
    fields = harness.parse_return_contract(text)
    assert fields == {}
    norm = harness._normalize(fields, text)
    assert norm["status"] == "complete"
    assert norm["output"] == text.strip()
    assert norm["decision_needed"] is None


def test_normalize_needs_decision_keeps_the_question():
    text = (
        "TASK: t-9\nFROM: operations\nSTATUS: needs-decision\n"
        "OUTPUT: partial draft\nSOURCES: none\nASSUMPTIONS: none\nRISKS: none\n"
        "DECISION NEEDED: this requires sending an external email — who approves it?\n"
        "NEXT: Aaron\n"
    )
    fields = harness.parse_return_contract(text)
    norm = harness._normalize(fields, text)
    assert norm["status"] == "needs-decision"
    assert "approves" in norm["decision_needed"]


# ---------------------------------------------------------------------------
# Department matching
# ---------------------------------------------------------------------------

def test_match_department_finds_slug_or_label():
    assert harness.match_department("marketing_sales") == "marketing_sales"
    assert harness.match_department("Marketing & Sales should pick this up") == "marketing_sales"
    assert harness.match_department("research intelligence") == "research_intelligence"


def test_match_department_none_for_aaron_or_missing():
    assert harness.match_department("Aaron") is None
    assert harness.match_department(None) is None
    assert harness.match_department("") is None


# ---------------------------------------------------------------------------
# Autonomous tool gating (agent_executor)
# ---------------------------------------------------------------------------

def test_gate_reason_flags_external_actions():
    from orchestrator import agent_executor as ae
    assert ae._gate_reason("send_email", "Send Email") == "send"
    assert ae._gate_reason("publish_workflow", None) == "publish"
    assert ae._gate_reason("create_content_production", "create_content_production") is None


@pytest.mark.asyncio
async def test_execute_refuses_gated_tool_when_autonomous():
    from orchestrator import agent_executor as ae
    result = await ae._execute("send_email", {"to": "x@example.com"}, {"send_email": "Send Email"}, autonomous=True)
    assert "[gated" in result
    assert "needs-decision" in result


@pytest.mark.asyncio
async def test_execute_gate_does_not_apply_when_not_autonomous():
    from orchestrator import agent_executor as ae
    # An unrecognised name isn't a local tool, so this falls through to the
    # n8n path (and fails there, since nothing's listening in tests) rather
    # than being refused by the autonomous gate — proving gating is opt-in.
    # Deliberately NOT calling a real local tool here: that would hit the
    # live production/media DB, which is exactly the cross-test pollution
    # this suite's isolated tmp-DB fixtures exist to avoid.
    result = await ae._execute("unknown_tool_xyz", {}, {})
    assert "[gated" not in result


# ---------------------------------------------------------------------------
# Daemon wiring: assignee routing, handoff creation, needs-decision gating
# ---------------------------------------------------------------------------

@pytest.fixture()
def iso_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("DAEMON_STATE_PATH", str(tmp_path / "daemon_state.json"))
    monkeypatch.setenv("MONTHLY_BUDGET_USD", "0")
    from orchestrator import operating
    importlib.reload(operating)
    yield tmp_path


def test_dispatch_agent_prefers_explicit_assignee(monkeypatch, iso_env):
    from orchestrator import daemon, agentic_harness as h

    captured = {}

    async def fake_run_autonomous_task(*, department, query, plan_id=None, max_tier=2, subagent=None, task_id=None):
        captured["department"] = department
        return {
            "ok": True, "status": "complete", "output": "done", "decision_needed": None,
            "handoff_department": None, "department": department, "tool_calls": [],
        }

    monkeypatch.setattr(h, "run_autonomous_task", fake_run_autonomous_task)
    task = {"task_id": "t1", "title": "Do a thing", "assignee": "operations", "plan_id": None}
    result = asyncio.run(daemon._dispatch_agent(task))
    assert captured["department"] == "operations"
    assert result["ok"] is True


def test_dispatch_agent_creates_handoff_task(monkeypatch, iso_env):
    from orchestrator import daemon, agentic_harness as h, operating

    async def fake_run_autonomous_task(*, department, **kwargs):
        return {
            "ok": False, "status": "complete", "output": "Findings ready for Marketing.",
            "decision_needed": None, "handoff_department": "marketing_sales",
            "department": department, "tool_calls": [],
        }

    monkeypatch.setattr(h, "run_autonomous_task", fake_run_autonomous_task)
    monkeypatch.setattr(daemon, "_log_decision", lambda *a, **k: None)

    task = {"task_id": "t2", "title": "Research competitor pricing", "plan_id": None, "priority": 2}
    result = asyncio.run(daemon._dispatch_agent(task))

    assert result["ok"] is True
    assert result["handoff_to"] == "marketing_sales"

    handoffs = operating.list_tasks(status="todo")
    assert any(t["assignee"] == "marketing_sales" for t in handoffs)
    assert any("[handoff from" in t["title"] for t in handoffs)


def test_dispatch_agent_surfaces_needs_decision(monkeypatch, iso_env):
    from orchestrator import daemon, agentic_harness as h

    async def fake_run_autonomous_task(*, department, **kwargs):
        return {
            "ok": False, "status": "needs-decision", "output": "partial",
            "decision_needed": "this needs Aaron to approve sending an email",
            "handoff_department": None, "department": department, "tool_calls": [],
        }

    monkeypatch.setattr(h, "run_autonomous_task", fake_run_autonomous_task)
    task = {"task_id": "t3", "title": "Follow up with client", "plan_id": None}
    result = asyncio.run(daemon._dispatch_agent(task))

    assert result["ok"] is False
    assert result["needs_decision"] is True
    assert "approve" in result["error"]


def test_run_cycle_routes_needs_decision_to_waiting_approval(monkeypatch, iso_env):
    from orchestrator import daemon, operating

    monkeypatch.setattr(daemon, "STATE_PATH", iso_env / "daemon_state.json")
    monkeypatch.setattr(daemon, "LOG_PATH", iso_env / "daemon.jsonl")
    monkeypatch.setattr(daemon, "DRY_RUN", False)

    plan_id = operating.create_plan("Test plan", project="test")
    task_id = operating.add_task(plan_id, "Send the client an update", type="agent")

    async def fake_run_task(task):
        return {"ok": False, "needs_decision": True, "error": "needs Aaron to approve sending an email"}

    monkeypatch.setattr(daemon, "run_task", fake_run_task)

    summary = asyncio.run(daemon.run_cycle(daemon.load_state()))
    assert summary["action"] == "gated"

    updated = [t for t in operating.list_tasks(plan_id=plan_id) if t["task_id"] == task_id][0]
    assert updated["status"] == "waiting_approval"
    assert "needs a decision" in (updated["note"] or "")
