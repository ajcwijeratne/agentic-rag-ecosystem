"""Weekly initiative: the system proposes work, not only executes it.

Once a week it reads the pipeline state, what published work performed, and
(when present) sector intelligence, then drafts next week's content plan. The
plan is created paused with its tasks blocked, so nothing runs until Aaron
approves it. Approval is one message: "start plan <id>". This is the shift from
a system that only executes queued work to one that suggests the work.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from . import operating, outcomes, production


def _sector_headline() -> str:
    """Optional sector-intel seed; absent in some deployments, so guarded."""
    try:
        from . import sector_briefing  # type: ignore
    except Exception:
        return ""
    for name in ("latest_headline", "headline", "weekly_headline"):
        fn = getattr(sector_briefing, name, None)
        if callable(fn):
            try:
                value = fn()
                return str(value)[:200] if value else ""
            except Exception:
                return ""
    return ""


def _pipeline_line() -> str:
    intel = production.intelligence()
    s = intel.get("summary") or {}
    parts = [f"{s.get('active', 0)} active", f"{s.get('blocked', 0)} blocked",
             f"{s.get('ready_to_generate', 0)} ready to render"]
    nb = intel.get("next_best") or {}
    if nb.get("title"):
        parts.append(f"next best move: {nb.get('next_action')} on '{nb.get('title')}'")
    return ", ".join(parts)


def _build_goal() -> str:
    week = datetime.now(timezone.utc).strftime("week of %d %b %Y")
    perf = outcomes.highlight(days=30)
    lines = [f"Content plan for the {week}."]
    if perf.get("line"):
        lines.append(perf["line"])
    best = perf.get("best") or {}
    if best.get("format") or best.get("title"):
        seed = best.get("format") or "the best-performing format"
        lines.append(f"Lean into what worked: more like {seed}.")
    lines.append(f"Pipeline: {_pipeline_line()}.")
    sector = _sector_headline()
    if sector:
        lines.append(f"Sector signal: {sector}")
    lines.append("Propose three concrete pieces for next week, each with a format, "
                 "a topic, and a one-line hook, and move any blocked production forward.")
    return " ".join(lines)


def propose_weekly_plan(actor: str = "daemon") -> dict[str, Any]:
    """Draft next week's content plan as a paused, blocked-task proposal."""
    goal = _build_goal()
    result = operating.generate_plan_from_goal(
        goal, title=f"Weekly initiative: {datetime.now(timezone.utc).strftime('%d %b')}",
        project="content_strategy", owner=actor, workflow="content_studio", create=True,
    )
    plan = result.get("plan") or {}
    plan_id = plan.get("plan_id")
    if not plan_id:
        return {"ok": False, "reason": "plan was not created", "goal": goal}

    # Make it inert until Aaron approves: pause the plan and block its tasks so
    # the daemon's task picker (which pulls any 'todo') leaves them alone.
    operating.update_plan(plan_id, status="paused")
    tasks = operating.list_tasks(plan_id=plan_id, limit=500)
    for task in tasks:
        if task.get("status") in ("todo", "doing"):
            operating.update_task(task["task_id"], status="blocked")

    return {"ok": True, "plan_id": plan_id, "goal": goal,
            "task_count": len(tasks), "workflow": result.get("workflow")}
