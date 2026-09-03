"""
Agentic Execution Harness
==========================
Turns an unattended `agent` task (dispatched by orchestrator/daemon.py) into
real, multi-step, tool-using work instead of one free-text completion.

Before this module existed, daemon._dispatch_agent called
wijerco_agent.call_wijerco_agent — a single LLM call with no tool access, so
an autonomous task could describe what should happen but never actually
retrieve, draft, or advance anything. run_autonomous_task below runs the same
tool-calling loop chat already uses (agent_executor.run_agentic_turn) but in
"autonomous" mode: a class of actions that need Aaron present (sending or
publishing anything, moving money, signing, touching grades/admissions/
records — see workforce/AGENTS/DECISION-RIGHTS.md) are gated instead of
executed, and the agent is required to close out with the Handoff Contract's
Return block (workforce/AGENTS/HANDOFF-CONTRACT.md) so the daemon can tell
"done" from "blocked" from "needs a human" from "next step belongs to another
department" without guessing from prose.

The daemon owns retry counts, the budget breaker and notification; this
module only owns one task's execution and its Return-contract parsing.
"""

from __future__ import annotations

import re
from typing import Any

from .wijerco_router import WijerCoDept

try:
    from typing import get_args
    DEPARTMENTS: tuple[str, ...] = get_args(WijerCoDept)
except Exception:
    DEPARTMENTS = (
        "learning_design", "academic_development", "marketing_sales", "operations",
        "research_intelligence", "support", "academic_affairs_registry",
        "student_experience_success", "library_scholarly_services",
        "research_innovation", "governance_risk_assurance", "people_culture",
    )

_DEPARTMENT_LABELS = {d: d.replace("_", " ") for d in DEPARTMENTS}


# ─────────────────────────────────────────────────────────────────────────────
# Completion contract — appended to the department system prompt for
# autonomous dispatch only. Interactive chat never sees this.
# ─────────────────────────────────────────────────────────────────────────────

_COMPLETION_INSTRUCTIONS = """

---

## You are running unattended

The operating daemon dispatched this task; no one will answer a follow-up
question this turn. Use your tools to actually finish it now — retrieve,
draft, compare, create or advance the internal records this needs — rather
than describing what someone should do.

When you are done (or stuck), end your reply with exactly this block, per
the WijerCo Handoff Contract, filled in truthfully:

TASK: <short id or title for this task>
FROM: <your department>
STATUS: complete | blocked | needs-decision
OUTPUT: <the artifact, answer, or durable pointer this task produced>
SOURCES: <evidence and provenance, or "none">
ASSUMPTIONS: <material assumptions you made, or "none">
RISKS: <open risks and controls, or "none">
DECISION NEEDED: <one specific question for Aaron, or "none">
NEXT: <"Aaron" if this is finished or needs a human, or the exact name of one
  other department if the next step is squarely their job>

Use STATUS: needs-decision whenever finishing requires something you cannot
do unattended (sending or publishing anything external, moving money,
signing or binding anything, or touching grades, admissions, misconduct or
records) — name it in DECISION NEEDED rather than attempting a workaround.
Use NEXT to name another department only when your part is genuinely done —
never to pass off work you could finish yourself.
"""

_FIELD_ORDER = (
    "TASK", "FROM", "STATUS", "OUTPUT", "SOURCES",
    "ASSUMPTIONS", "RISKS", "DECISION NEEDED", "NEXT",
)
_FIELD_PATTERN = re.compile(
    r"(?im)^[ \t]*(" + "|".join(re.escape(f) for f in _FIELD_ORDER) + r")[ \t]*:[ \t]*"
)


def parse_return_contract(text: str) -> dict[str, str]:
    """Pull the labelled Handoff Contract fields out of free text.

    Tolerant by design: an agent that ignores the format entirely yields an
    empty dict, which callers treat as "assume complete, whole text is the
    output" rather than an error.
    """
    text = text or ""
    matches = list(_FIELD_PATTERN.finditer(text))
    fields: dict[str, str] = {}
    for i, m in enumerate(matches):
        label = m.group(1).upper()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        fields[label] = text[start:end].strip()
    return fields


def _normalize(fields: dict[str, str], fallback_text: str) -> dict[str, Any]:
    status = (fields.get("STATUS") or "").strip().lower()
    if status not in ("complete", "blocked", "needs-decision"):
        # No contract block at all (legacy free text) — treat as complete
        # rather than blocking every unformatted response.
        status = "complete"
    output = (fields.get("OUTPUT") or "").strip() or fallback_text.strip()
    decision = (fields.get("DECISION NEEDED") or "").strip()
    if decision.lower() in ("", "none", "n/a", "none.", "n/a."):
        decision = ""
    return {
        "status": status,
        "output": output,
        "sources": (fields.get("SOURCES") or "").strip(),
        "assumptions": (fields.get("ASSUMPTIONS") or "").strip(),
        "risks": (fields.get("RISKS") or "").strip(),
        "decision_needed": decision or None,
        "next_raw": (fields.get("NEXT") or "").strip() or None,
    }


def _loose(s: str) -> str:
    """Lowercase and strip everything but letters/digits, so punctuation like
    the "&" in "Marketing & Sales" can't break a match."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def match_department(raw: str | None) -> str | None:
    """Map a free-text NEXT field to a canonical department slug, if any."""
    if not raw:
        return None
    low = raw.lower()
    if "aaron" in low or "human" in low or "me" in low.split():
        return None
    loose = _loose(raw)
    for dept in DEPARTMENTS:
        if dept == "orchestrator":
            continue
        # Multi-word department slugs tolerate punctuation/spacing changes
        # ("Marketing & Sales" -> "marketing_sales"); single-word slugs need a
        # real word boundary so e.g. "support" doesn't match "supportive".
        if "_" in dept:
            if _loose(dept) in loose:
                return dept
        elif re.search(rf"\b{re.escape(dept)}\b", low):
            return dept
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ─────────────────────────────────────────────────────────────────────────────

async def run_autonomous_task(
    *,
    department: str,
    query: str,
    plan_id: str | None = None,
    max_tier: int = 2,
    subagent: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Run one unattended task through the tool-using agent loop.

    Returns a dict the daemon can act on directly:
      ok                 — True only if status == complete, no decision is
                            needed, and there's non-empty output.
      status              complete | blocked | needs-decision
      output              the agent's Return-contract OUTPUT (or raw text)
      decision_needed     the question for Aaron, or None
      handoff_department  a department slug if NEXT named one, else None
      tool_calls          [{"tool", "args", "ok"}, ...] in call order
      cost_usd, model_label, raw
    """
    from .wijerco_agent import _build_system_prompt
    from .agent_executor import run_agentic_turn

    system_prompt = _build_system_prompt(
        department, subagent=subagent, extra_instructions=_COMPLETION_INSTRUCTIONS,
    )

    full_text = ""
    tool_calls: list[dict[str, Any]] = []
    final_event: dict[str, Any] = {}
    try:
        async for ev in run_agentic_turn(
            query, system_prompt, history=[], max_tier=max_tier,
            autonomous=True, cost_task_type=f"wijerco/{department}/autonomous",
        ):
            etype = ev.get("type")
            if etype == "tool_call":
                tool_calls.append({"tool": ev.get("tool"), "args": ev.get("args")})
            elif etype == "tool_result" and tool_calls:
                tool_calls[-1]["ok"] = ev.get("ok")
            elif etype == "token":
                full_text += ev.get("token") or ""
            elif etype == "end":
                final_event = ev
    except Exception as exc:
        return {
            "ok": False, "status": "blocked", "output": "", "decision_needed": None,
            "handoff_department": None, "department": department, "tool_calls": tool_calls,
            "error": str(exc), "raw": full_text,
        }

    fields = parse_return_contract(full_text)
    norm = _normalize(fields, full_text)
    handoff_department = match_department(norm["next_raw"])
    if handoff_department == department:
        handoff_department = None  # naming your own department isn't a handoff

    ok = (
        norm["status"] == "complete"
        and not norm["decision_needed"]
        and not handoff_department
        and bool(norm["output"].strip())
    )

    return {
        "ok": ok,
        "status": norm["status"],
        "output": norm["output"],
        "sources": norm["sources"],
        "assumptions": norm["assumptions"],
        "risks": norm["risks"],
        "decision_needed": norm["decision_needed"],
        "handoff_department": handoff_department,
        "department": department,
        "tool_calls": tool_calls,
        "cost_usd": final_event.get("cost_usd", 0.0),
        "model_label": final_event.get("model_label"),
        "raw": full_text,
    }


def create_handoff_task(*, parent_task: dict[str, Any], result: dict[str, Any]) -> str:
    """File the Handoff Contract dispatch fields as a new task for the
    department the finished task named in NEXT."""
    from . import operating

    to_dept = result["handoff_department"]
    from_dept = result.get("department", "orchestrator")
    priority = parent_task.get("priority")
    priority = priority if isinstance(priority, int) else 3

    dispatch = (
        f"TASK: {parent_task.get('task_id')}\n"
        f"FROM: {from_dept}\n"
        f"TO: {to_dept}\n"
        f"OUTCOME: {parent_task.get('title')}\n"
        f"INPUTS: {(result.get('output') or '')[:1500]}\n"
        "EXPECTED OUTPUT: complete the next step and return the Handoff "
        "Contract block.\n"
        "CONSTRAINTS: same scope, privacy, budget and approval limits as the "
        "parent task.\n"
        "DEADLINE: this session\n"
        "DECISION OWNER: Aaron\n"
        f"NEXT: {to_dept}"
    )
    return operating.add_task(
        parent_task.get("plan_id"),
        title=f"[handoff from {from_dept}] {parent_task.get('title')}",
        type="agent",
        status="todo",
        assignee=to_dept,
        priority=priority,
        note=dispatch,
        meta={"handoff": {"from": from_dept, "parent_task_id": parent_task.get("task_id")}},
    )
