"""
Operating Daemon
================
The execution loop that connects the planner to the executor. Runs as its own
process (`python -m orchestrator.daemon`), polls the operating layer for the
next unblocked task, dispatches it, records the result, and repeats.

Design rules, enforced here and not in the agents:

  * Concurrency 1. One task per cycle. The audit trail stays readable and the
    blast radius stays small.
  * The daemon never approves a governance gate. `approval` tasks notify Aaron
    once and then wait. `manual` tasks do the same.
  * Budget breaker. Before any cloud dispatch the daemon calls
    cost_tracker.budget_status(). At `warn` it notifies once per month; at
    `stop` it dispatches nothing that costs money and says so in the brief.
  * Two strikes. A task that fails twice is marked `blocked` with the error in
    its note, and Aaron is notified. No infinite retry loops.
  * Kill switch. data/daemon_state.json carries `paused`; POST
    /operating/daemon/pause flips it and a restart stays paused.

Every decision appends one JSON line to logs/daemon.jsonl. A heartbeat file
(logs/daemon_heartbeat) is touched every cycle so the systemd watchdog can
restart a wedged process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from . import operating
from . import governance
from .cost_tracker import budget_status
from .wijerco_router import classify_intent

logger = logging.getLogger("daemon")

_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = Path(os.getenv("DAEMON_STATE_PATH", str(_ROOT / "data" / "daemon_state.json")))
LOG_PATH = _ROOT / "logs" / "daemon.jsonl"
HEARTBEAT_PATH = _ROOT / "logs" / "daemon_heartbeat"

INTERVAL_SEC = float(os.getenv("DAEMON_INTERVAL_SEC", "60"))
DRY_RUN = os.getenv("DAEMON_DRY_RUN", "0") in ("1", "true", "yes")
MAX_ATTEMPTS = int(os.getenv("DAEMON_MAX_ATTEMPTS", "2"))
AGENT_MAX_TIER = int(os.getenv("DAEMON_AGENT_MAX_TIER", "2"))
CONSOLIDATION_HOUR = int(os.getenv("CONSOLIDATION_HOUR", "2"))
MEASURE_HOUR = int(os.getenv("MEASURE_HOUR", "3"))
LEARN_DAY = int(os.getenv("LEARN_DAY", "1"))  # day of month for the learning pass
INITIATIVE_WEEKDAY = int(os.getenv("INITIATIVE_WEEKDAY", "0"))  # Monday=0
INITIATIVE_HOUR = int(os.getenv("INITIATIVE_HOUR", "6"))

# Task types the daemon may execute without a human. `approval` and `manual`
# always stop at notification. This is the "auto-run internal, gate external"
# policy in one line.
AUTO_TYPES = ("agent", "production", "memory")


# ---------------------------------------------------------------------------
# State, heartbeat, decision log
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"paused": False, "cycles": 0, "budget_warned_month": "", "notified_tasks": []}


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def pause(actor: str = "operator") -> dict[str, Any]:
    state = load_state()
    state["paused"] = True
    state["paused_at"] = _now_iso()
    state["paused_by"] = actor
    save_state(state)
    _log_decision("pause", {"actor": actor})
    return state


def resume(actor: str = "operator") -> dict[str, Any]:
    state = load_state()
    state["paused"] = False
    state["resumed_at"] = _now_iso()
    state["resumed_by"] = actor
    save_state(state)
    _log_decision("resume", {"actor": actor})
    return state


def status() -> dict[str, Any]:
    state = load_state()
    hb = None
    try:
        hb = datetime.fromtimestamp(HEARTBEAT_PATH.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
    except Exception:
        pass
    return {
        "paused": bool(state.get("paused")),
        "cycles": state.get("cycles", 0),
        "last_heartbeat": hb,
        "last_result": state.get("last_result"),
        "budget": budget_status(),
        "dry_run": DRY_RUN,
        "interval_sec": INTERVAL_SEC,
    }


def _heartbeat() -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_PATH.write_text(_now_iso(), encoding="utf-8")


def _log_decision(action: str, detail: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": _now_iso(), "action": action, **detail}
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")
    except Exception:
        logger.exception("failed to write daemon.jsonl")


async def _notify(title: str, body: str) -> None:
    try:
        from notifications.notifier import notify
        await notify(title=title, body=body)
    except Exception:
        logger.warning("notification failed: %s", title)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

def _attempts(task: dict) -> int:
    meta = task.get("meta") or {}
    return int((meta.get("daemon") or {}).get("attempts", 0))


def _set_daemon_meta(task: dict, **updates: Any) -> dict:
    meta = dict(task.get("meta") or {})
    daemon_meta = dict(meta.get("daemon") or {})
    daemon_meta.update(updates)
    meta["daemon"] = daemon_meta
    return meta


def _plan_context(task: dict) -> str:
    """Assemble compact plan + project memory context for an agent task."""
    parts: list[str] = []
    plan = operating.get_plan(task["plan_id"]) if task.get("plan_id") else None
    project = None
    if plan:
        project = plan.get("project")
        parts.append(f"Plan: {plan.get('title')} (workflow: {plan.get('workflow')}, project: {project})")
        if plan.get("goal"):
            parts.append(f"Goal: {plan['goal']}")
        siblings = operating.list_tasks(plan_id=task["plan_id"], limit=50)
        done = [t["title"] for t in siblings if t["status"] == "done"]
        if done:
            parts.append("Completed so far: " + "; ".join(done[:8]))
    planner = (task.get("meta") or {}).get("planner") or {}
    if planner.get("success_criteria"):
        parts.append("Success criteria: " + "; ".join(planner["success_criteria"]))
    if project:
        memories = operating.list_project_memory(project=project, limit=8)
        if memories:
            parts.append("Project memory:")
            parts.extend(f"- {m.get('content')}" for m in memories)
    return "\n".join(parts)


async def _dispatch_agent(task: dict) -> dict[str, Any]:
    """Run an `agent` task through the WijerCo agent layer."""
    from .wijerco_agent import call_wijerco_agent

    intent = classify_intent(task["title"])
    department = getattr(intent, "department", None) or "research_intelligence"
    context = _plan_context(task)
    query = (
        f"Operating task: {task['title']}\n\n{context}\n\n"
        "Complete this task. Be specific and self-contained; your output is "
        "recorded as the task result and read later without you present."
    )
    result = await call_wijerco_agent(
        department=department,
        query=query,
        max_tier=AGENT_MAX_TIER,
    )
    answer = result.get("answer") or result.get("content") or ""
    return {"ok": bool(answer.strip()), "department": department, "output": answer}


async def _dispatch_production(task: dict) -> dict[str, Any]:
    """Advance the linked production by exactly one state."""
    from . import production

    target = task.get("target_id")
    if not target:
        return {"ok": False, "error": "production task has no target_id"}
    result = await production.advance(target, actor="daemon")
    blocked = bool(result.get("blocked"))
    detail = {k: result.get(k) for k in ("blocked", "gate", "done") if k in result}
    return {"ok": not blocked, "blocked_on_gate": blocked, "result": detail}


async def _dispatch_memory(task: dict) -> dict[str, Any]:
    """Write the task content into project memory and the semantic store."""
    plan = operating.get_plan(task["plan_id"]) if task.get("plan_id") else None
    project = (plan or {}).get("project") or "general"
    content = task.get("note") or task["title"]
    operating.add_project_memory(project, content, source="daemon")
    try:
        from memory.memory_store import store
        await store.add(project, content, source="daemon")
    except Exception:
        pass
    return {"ok": True, "project": project}


async def run_task(task: dict) -> dict[str, Any]:
    """Dispatch one task by type. Returns {ok, ...detail}."""
    kind = task.get("type")
    if kind == "agent":
        return await _dispatch_agent(task)
    if kind == "production":
        return await _dispatch_production(task)
    if kind == "memory":
        return await _dispatch_memory(task)
    return {"ok": False, "error": f"type {kind!r} is not auto-runnable"}


# ---------------------------------------------------------------------------
# One cycle
# ---------------------------------------------------------------------------

async def run_cycle(state: dict[str, Any]) -> dict[str, Any]:
    """One daemon cycle. Returns a summary dict for the state file and log."""
    # Keep approval and production queues in sync before choosing work.
    try:
        operating.sync_approval_tasks()
        operating.sync_production_tasks()
    except Exception:
        logger.exception("sync failed")

    rec = operating.recommend_next_action()
    task = rec.get("task")
    if not task:
        return {"picked": None, "reason": rec.get("reason")}

    kind = task.get("type")
    task_id = task["task_id"]

    # Gated and manual work: notify once, never execute.
    if kind in ("approval", "manual") or task.get("status") == "waiting_approval":
        notified = state.setdefault("notified_tasks", [])
        if task_id not in notified:
            body = (f"{kind or 'task'}: {task['title']}\nTask {task_id}. "
                    "Approve in the Command Centre or by Telegram.")
            gate = (task.get("meta") or {}).get("gate")
            target = task.get("target_id")
            if gate and target:
                try:
                    from .inbox import approval_links
                    links = approval_links(gate, target)
                    if links:
                        body += (f"\nApprove: {links['approve']}"
                                 f"\nReject: {links['reject']}")
                    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
                    if base:
                        body += f"\nPreview: {base}/productions/{target}/preview"
                except Exception:
                    pass
            await _notify("Waiting on you", body)
            notified.append(task_id)
            state["notified_tasks"] = notified[-200:]
            # Persist immediately. The main loop re-reads state after each
            # cycle so pause/resume changes made mid-cycle win; without this
            # write, that re-read discards the notify-once marker.
            save_state(state)
            _log_decision("notify_waiting", {"task_id": task_id, "type": kind, "title": task["title"]})
        return {"picked": task_id, "action": "waiting_on_human", "type": kind}

    if kind not in AUTO_TYPES:
        return {"picked": task_id, "action": "skipped", "reason": f"type {kind!r} not auto-runnable"}

    # Budget breaker applies to anything that can spend money.
    budget = budget_status()
    if kind in ("agent", "production") and budget["level"] == "stop":
        _log_decision("budget_stop", {"task_id": task_id, **budget})
        return {"picked": task_id, "action": "budget_stop", "budget": budget}
    if budget["level"] == "warn" and state.get("budget_warned_month") != budget["month"]:
        await _notify(
            "Budget warning",
            f"Cloud spend is at {budget['ratio']:.0%} of ${budget['budget_usd']:.2f} "
            f"for {budget['month']} (${budget['spent_usd']:.2f} spent).",
        )
        state["budget_warned_month"] = budget["month"]
        save_state(state)

    if DRY_RUN:
        _log_decision("dry_run", {"task_id": task_id, "type": kind, "title": task["title"]})
        return {"picked": task_id, "action": "dry_run", "type": kind}

    # Execute, two strikes.
    attempts = _attempts(task) + 1
    operating.update_task(task_id, status="doing", meta=_set_daemon_meta(task, attempts=attempts, last_attempt=_now_iso()))
    started = time.time()
    try:
        result = await run_task(task)
    except Exception as exc:
        result = {"ok": False, "error": str(exc)}
    elapsed = round(time.time() - started, 1)

    if result.get("ok"):
        note = (task.get("note") or "").strip()
        output = str(result.get("output") or "")[:4000]
        new_note = (note + "\n\n---\ndaemon result:\n" + output).strip() if output else note
        operating.update_task(
            task_id,
            status="done",
            note=new_note or None,
            meta=_set_daemon_meta(task, attempts=attempts, completed_at=_now_iso(), elapsed_sec=elapsed),
        )
        _log_decision("task_done", {"task_id": task_id, "type": kind, "title": task["title"], "elapsed_sec": elapsed})
        return {"picked": task_id, "action": "done", "type": kind, "elapsed_sec": elapsed}

    if result.get("blocked_on_gate"):
        operating.update_task(task_id, status="waiting_approval", meta=_set_daemon_meta(task, attempts=attempts))
        _log_decision("task_gated", {"task_id": task_id, "title": task["title"]})
        return {"picked": task_id, "action": "gated"}

    error = str(result.get("error") or "unknown failure")[:500]
    if attempts >= MAX_ATTEMPTS:
        operating.update_task(
            task_id,
            status="blocked",
            note=((task.get("note") or "") + f"\n\n---\ndaemon blocked after {attempts} attempts: {error}").strip(),
            meta=_set_daemon_meta(task, attempts=attempts, blocked_at=_now_iso(), error=error),
        )
        await _notify("Task blocked", f"{task['title']}\n{error}\nTask {task_id} needs you.")
        _log_decision("task_blocked", {"task_id": task_id, "error": error, "attempts": attempts})
        return {"picked": task_id, "action": "blocked", "error": error}

    operating.update_task(task_id, status="todo", meta=_set_daemon_meta(task, attempts=attempts, error=error))
    _log_decision("task_retry_queued", {"task_id": task_id, "error": error, "attempts": attempts})
    return {"picked": task_id, "action": "retry_queued", "error": error}




async def _maybe_consolidate(state: dict[str, Any]) -> None:
    """Run nightly memory consolidation once per day at CONSOLIDATION_HOUR."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if state.get("consolidated_date") == today or now.hour < CONSOLIDATION_HOUR:
        return
    try:
        from memory.consolidation import run_consolidation
        summary = await run_consolidation()
        _log_decision("consolidation", summary)
    except Exception as exc:
        _log_decision("consolidation_error", {"error": str(exc)[:300]})
    state["consolidated_date"] = today
    save_state(state)


async def _maybe_measure(state: dict[str, Any]) -> None:
    """Run the measure sweep once a day at MEASURE_HOUR. Pulls or requests
    outcomes for published work and closes productions past their window."""
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if state.get("measured_date") == today or now.hour < MEASURE_HOUR:
        return
    try:
        from . import measure
        summary = await measure.run_measure_sweep(actor="daemon")
        _log_decision("measure_sweep", summary)
        if summary.get("closed"):
            await _notify("Productions measured",
                          f"{summary['closed']} production(s) closed with outcomes recorded.")
    except Exception as exc:
        _log_decision("measure_error", {"error": str(exc)[:300]})
    state["measured_date"] = today
    save_state(state)


async def _maybe_learn(state: dict[str, Any]) -> None:
    """Run the learning reflection once a month on LEARN_DAY."""
    now = datetime.now()
    month = now.strftime("%Y-%m")
    if state.get("learned_month") == month or now.day < LEARN_DAY:
        return
    try:
        from . import learning
        summary = learning.run_learning_reflection()
        _log_decision("learning_reflection", summary)
        if summary.get("ok"):
            await _notify("Monthly performance review",
                          "\n".join(summary.get("findings") or [])[:1500])
    except Exception as exc:
        _log_decision("learning_error", {"error": str(exc)[:300]})
    state["learned_month"] = month
    save_state(state)


async def _maybe_weekly_initiative(state: dict[str, Any]) -> None:
    """Once a week, propose next week's content plan as a paused proposal."""
    now = datetime.now()
    iso = now.isocalendar()
    week = f"{iso[0]}-W{iso[1]:02d}"
    if state.get("initiative_week") == week:
        return
    if now.weekday() != INITIATIVE_WEEKDAY or now.hour < INITIATIVE_HOUR:
        return
    try:
        from . import initiative
        summary = initiative.propose_weekly_plan(actor="daemon")
        _log_decision("weekly_initiative", summary)
        if summary.get("ok"):
            await _notify(
                "Next week's plan proposed",
                f"{summary.get('task_count')} tasks drafted, paused for your approval.\n"
                f"Approve with: start plan {summary.get('plan_id')}\n\n"
                f"{(summary.get('goal') or '')[:800]}",
            )
    except Exception as exc:
        _log_decision("initiative_error", {"error": str(exc)[:300]})
    state["initiative_week"] = week
    save_state(state)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logger.info("operating daemon starting (interval=%ss, dry_run=%s)", INTERVAL_SEC, DRY_RUN)
    _log_decision("daemon_start", {"interval_sec": INTERVAL_SEC, "dry_run": DRY_RUN})
    while True:
        state = load_state()
        _heartbeat()
        if state.get("paused"):
            await asyncio.sleep(INTERVAL_SEC)
            continue
        try:
            summary = await run_cycle(state)
        except Exception as exc:
            logger.exception("cycle failed")
            summary = {"action": "cycle_error", "error": str(exc)}
            _log_decision("cycle_error", {"error": str(exc)})
        state = load_state()  # re-read in case pause happened mid-cycle
        await _maybe_consolidate(state)
        await _maybe_measure(state)
        await _maybe_learn(state)
        await _maybe_weekly_initiative(state)
        state["cycles"] = int(state.get("cycles", 0)) + 1
        state["last_result"] = summary
        state["last_cycle_at"] = _now_iso()
        save_state(state)
        await asyncio.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    asyncio.run(main())
