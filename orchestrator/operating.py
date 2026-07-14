"""Autonomous operating layer: plans, task state, daily brief, project memory."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

_DB_PATH = Path(os.getenv("MEDIA_DB_PATH", "data/media.db"))

PLAN_STATUSES = ("active", "paused", "complete", "archived")
TASK_STATUSES = ("todo", "doing", "blocked", "waiting_approval", "done", "cancelled")
TASK_TYPES = ("agent", "approval", "production", "memory", "manual")

WORKFLOW_TEMPLATES: dict[str, list[dict[str, Any]]] = {
    "content_studio": [
        {"key": "brief", "title": "Clarify brief, audience, channel, and success measure", "type": "agent", "priority": 5},
        {"key": "research", "title": "Gather source evidence and project memory", "type": "agent", "priority": 5, "depends_on": ["brief"]},
        {"key": "script", "title": "Draft script or narrative structure", "type": "production", "priority": 4, "depends_on": ["research"]},
        {"key": "storyboard", "title": "Create storyboard and asset plan", "type": "production", "priority": 4, "depends_on": ["script"]},
        {"key": "render", "title": "Render draft asset or content package", "type": "production", "priority": 3, "depends_on": ["storyboard"]},
        {"key": "review", "title": "Review factual claims, rights, and approvals", "type": "approval", "priority": 5, "depends_on": ["render"]},
        {"key": "publish", "title": "Publish and record outcome", "type": "manual", "priority": 3, "depends_on": ["review"]},
    ],
    "deployment": [
        {"key": "scope", "title": "Confirm deployment scope, owners, and rollback point", "type": "manual", "priority": 5},
        {"key": "migrate", "title": "Run migrations and verify schema status", "type": "agent", "priority": 5, "depends_on": ["scope"]},
        {"key": "backup", "title": "Create database backup and release snapshot", "type": "agent", "priority": 5, "depends_on": ["migrate"]},
        {"key": "rehearse", "title": "Run restore and rollback dry runs", "type": "agent", "priority": 5, "depends_on": ["backup"]},
        {"key": "monitor", "title": "Check monitoring, traces, and approval queues", "type": "agent", "priority": 4, "depends_on": ["rehearse"]},
        {"key": "promote", "title": "Promote release only after rehearsal passes", "type": "approval", "priority": 5, "depends_on": ["monitor"]},
    ],
    "incident": [
        {"key": "triage", "title": "Triage impact, symptoms, and affected services", "type": "agent", "priority": 5},
        {"key": "contain", "title": "Contain the issue and pause risky automation", "type": "manual", "priority": 5, "depends_on": ["triage"]},
        {"key": "diagnose", "title": "Inspect traces, recent changes, and dependency health", "type": "agent", "priority": 5, "depends_on": ["contain"]},
        {"key": "recover", "title": "Apply fix, restore, or rollback path", "type": "manual", "priority": 5, "depends_on": ["diagnose"]},
        {"key": "verify", "title": "Verify recovery with tests and monitoring", "type": "agent", "priority": 4, "depends_on": ["recover"]},
        {"key": "postmortem", "title": "Record root cause and prevention memory", "type": "memory", "priority": 3, "depends_on": ["verify"]},
    ],
    "general": [
        {"key": "define", "title": "Define the outcome, constraints, and decision owner", "type": "manual", "priority": 5},
        {"key": "context", "title": "Collect relevant context, memory, and existing state", "type": "agent", "priority": 4, "depends_on": ["define"]},
        {"key": "plan", "title": "Break work into deliverables and acceptance checks", "type": "agent", "priority": 4, "depends_on": ["context"]},
        {"key": "execute", "title": "Execute the first deliverable", "type": "manual", "priority": 3, "depends_on": ["plan"]},
        {"key": "review", "title": "Review output, blockers, and next action", "type": "approval", "priority": 4, "depends_on": ["execute"]},
        {"key": "close", "title": "Close the loop and update project memory", "type": "memory", "priority": 3, "depends_on": ["review"]},
    ],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operating_plans (
            plan_id    TEXT PRIMARY KEY,
            title      TEXT NOT NULL,
            project    TEXT,
            goal       TEXT,
            status     TEXT NOT NULL,
            owner      TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            meta       TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS operating_tasks (
            task_id    TEXT PRIMARY KEY,
            plan_id    TEXT,
            title      TEXT NOT NULL,
            type       TEXT NOT NULL,
            status     TEXT NOT NULL,
            assignee   TEXT,
            priority   INTEGER NOT NULL DEFAULT 3,
            due        TEXT,
            target_id  TEXT,
            note       TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            meta       TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS project_memories (
            memory_id  TEXT PRIMARY KEY,
            project    TEXT NOT NULL,
            content    TEXT NOT NULL,
            source     TEXT,
            created_at TEXT NOT NULL,
            meta       TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_op_plans_project ON operating_plans(project)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_op_tasks_plan ON operating_tasks(plan_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_op_tasks_status ON operating_tasks(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_project_memory_project ON project_memories(project)")
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _row(row: sqlite3.Row) -> dict:
    data = dict(row)
    if "meta" in data:
        data["meta"] = _loads(data.get("meta"), {})
    return data


def create_plan(
    title: str,
    *,
    project: str | None = None,
    goal: str | None = None,
    owner: str | None = None,
    tasks: list[dict] | None = None,
    meta: dict | None = None,
) -> str:
    plan_id = str(uuid.uuid4())
    now = _now()
    with _db() as conn:
        conn.execute(
            "INSERT INTO operating_plans (plan_id,title,project,goal,status,owner,created_at,updated_at,meta) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (plan_id, title, project, goal, "active", owner, now, now, json.dumps(meta or {})),
        )
    for task in tasks or []:
        add_task(
            plan_id,
            task.get("title") or "Untitled task",
            type=task.get("type") or "manual",
            status=task.get("status") or "todo",
            assignee=task.get("assignee"),
            priority=int(task.get("priority") or 3),
            due=task.get("due"),
            target_id=task.get("target_id"),
            note=task.get("note"),
            meta=task.get("meta") or {},
        )
    return plan_id


def list_plans(*, status: str | None = None, project: str | None = None, limit: int = 100) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("status=?")
        params.append(status)
    if project:
        clauses.append("project=?")
        params.append(project)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM operating_plans {where} ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_with_counts(_row(r)) for r in rows]


def get_plan(plan_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM operating_plans WHERE plan_id=?", (plan_id,)).fetchone()
    if not row:
        return None
    plan = _row(row)
    plan["tasks"] = list_tasks(plan_id=plan_id, limit=500)
    return _with_counts(plan)


def update_plan(plan_id: str, **fields: Any) -> bool:
    allowed = {"title", "project", "goal", "status", "owner", "meta"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"field {key!r} is not editable")
        if key == "status" and value not in PLAN_STATUSES:
            raise ValueError(f"status must be one of {PLAN_STATUSES}")
        if key == "meta":
            value = json.dumps(value or {})
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(_now())
    params.append(plan_id)
    with _db() as conn:
        cur = conn.execute(f"UPDATE operating_plans SET {', '.join(sets)} WHERE plan_id=?", params)
    return cur.rowcount > 0


def add_task(
    plan_id: str | None,
    title: str,
    *,
    type: str = "manual",
    status: str = "todo",
    assignee: str | None = None,
    priority: int = 3,
    due: str | None = None,
    target_id: str | None = None,
    note: str | None = None,
    meta: dict | None = None,
) -> str:
    if type not in TASK_TYPES:
        raise ValueError(f"type must be one of {TASK_TYPES}")
    if status not in TASK_STATUSES:
        raise ValueError(f"status must be one of {TASK_STATUSES}")
    task_id = str(uuid.uuid4())
    now = _now()
    with _db() as conn:
        if plan_id and not conn.execute("SELECT 1 FROM operating_plans WHERE plan_id=?", (plan_id,)).fetchone():
            raise KeyError("plan not found")
        conn.execute(
            "INSERT INTO operating_tasks (task_id,plan_id,title,type,status,assignee,priority,due,target_id,note,created_at,updated_at,meta) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (task_id, plan_id, title, type, status, assignee, priority, due, target_id, note, now, now, json.dumps(meta or {})),
        )
        if plan_id:
            conn.execute("UPDATE operating_plans SET updated_at=? WHERE plan_id=?", (now, plan_id))
    return task_id


def list_tasks(*, plan_id: str | None = None, status: str | None = None, project: str | None = None, limit: int = 200) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    joins = ""
    if plan_id:
        clauses.append("t.plan_id=?")
        params.append(plan_id)
    if status:
        clauses.append("t.status=?")
        params.append(status)
    if project:
        joins = "LEFT JOIN operating_plans p ON p.plan_id=t.plan_id"
        clauses.append("p.project=?")
        params.append(project)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(
            f"SELECT t.* FROM operating_tasks t {joins} {where} "
            "ORDER BY CASE t.status WHEN 'blocked' THEN 0 WHEN 'waiting_approval' THEN 1 WHEN 'doing' THEN 2 ELSE 3 END, "
            "t.priority DESC, t.updated_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_row(r) for r in rows]


def update_task(task_id: str, **fields: Any) -> bool:
    allowed = {"title", "type", "status", "assignee", "priority", "due", "target_id", "note", "meta"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"field {key!r} is not editable")
        if key == "type" and value not in TASK_TYPES:
            raise ValueError(f"type must be one of {TASK_TYPES}")
        if key == "status" and value not in TASK_STATUSES:
            raise ValueError(f"status must be one of {TASK_STATUSES}")
        if key == "meta":
            value = json.dumps(value or {})
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(_now())
    params.append(task_id)
    with _db() as conn:
        cur = conn.execute(f"UPDATE operating_tasks SET {', '.join(sets)} WHERE task_id=?", params)
    return cur.rowcount > 0


def add_project_memory(project: str, content: str, *, source: str = "operator", meta: dict | None = None) -> str:
    memory_id = str(uuid.uuid4())
    with _db() as conn:
        conn.execute(
            "INSERT INTO project_memories (memory_id,project,content,source,created_at,meta) VALUES (?,?,?,?,?,?)",
            (memory_id, project, content, source, _now(), json.dumps(meta or {})),
        )
    return memory_id


def list_project_memory(project: str | None = None, limit: int = 100) -> list[dict]:
    params: list[Any] = []
    where = ""
    if project:
        where = "WHERE project=?"
        params.append(project)
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM project_memories {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_row(r) for r in rows]


def infer_workflow(goal: str, workflow: str | None = None) -> dict[str, Any]:
    """Classify a goal into a planner workflow using transparent heuristics."""
    requested = (workflow or "").strip().lower()
    if requested in WORKFLOW_TEMPLATES:
        return {"workflow": requested, "confidence": 1.0, "reason": "workflow requested explicitly"}

    text = goal.lower()
    scores = {
        "content_studio": sum(1 for word in ("content", "script", "storyboard", "render", "publish", "campaign", "video") if word in text),
        "deployment": sum(1 for word in ("deploy", "deployment", "release", "rollback", "backup", "migration", "hardening", "rehearsal", "monitoring") if word in text),
        "incident": sum(1 for word in ("incident", "outage", "broken", "failure", "recover", "urgent", "error", "degraded") if word in text),
    }
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return {"workflow": "general", "confidence": 0.45, "reason": "no specialised workflow keywords found"}
    confidence = min(0.95, 0.55 + (scores[best] * 0.12))
    return {"workflow": best, "confidence": round(confidence, 2), "reason": f"matched {scores[best]} workflow signal(s)"}


def _risk_flags(goal: str, workflow: str) -> list[str]:
    text = goal.lower()
    flags = []
    if workflow == "deployment" or any(word in text for word in ("deploy", "release", "rollback", "backup", "migration")):
        flags.extend(["state_change", "rollback_required"])
    if workflow == "content_studio" or any(word in text for word in ("publish", "public", "client", "generated image")):
        flags.extend(["approval_required", "rights_review"])
    if workflow == "incident" or any(word in text for word in ("urgent", "incident", "outage", "failure")):
        flags.extend(["time_sensitive", "service_impact"])
    return sorted(set(flags))


def _task_specs_for_goal(goal: str, workflow: str, context: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    context = context or {}
    risks = _risk_flags(goal, workflow)
    specs = []
    for index, template in enumerate(WORKFLOW_TEMPLATES.get(workflow, WORKFLOW_TEMPLATES["general"]), start=1):
        meta = {
            "planner": {
                "key": template["key"],
                "sequence": index,
                "depends_on_keys": template.get("depends_on", []),
                "success_criteria": _success_criteria(template["key"], workflow),
                "risk_flags": risks,
            }
        }
        if context:
            meta["planner"]["context"] = context
        specs.append({
            "title": template["title"],
            "type": template["type"],
            "status": "todo",
            "assignee": context.get("assignee") or context.get("owner"),
            "priority": template["priority"],
            "note": f"Generated from goal: {goal}",
            "meta": meta,
        })
    return specs


def _success_criteria(key: str, workflow: str) -> list[str]:
    criteria = {
        "brief": ["brief has audience, channel, format, and outcome"],
        "research": ["evidence and source assets are linked or recorded"],
        "script": ["script is ready for review"],
        "storyboard": ["scenes and required assets are listed"],
        "render": ["draft render or package exists"],
        "review": ["approval gates are cleared or blockers are recorded"],
        "publish": ["published output and outcome are recorded"],
        "scope": ["scope and rollback point are confirmed"],
        "migrate": ["schema status is current"],
        "backup": ["database backup and release snapshot paths are recorded"],
        "rehearse": ["restore and rollback dry runs pass"],
        "monitor": ["monitoring summary is readable and acceptable"],
        "promote": ["release decision is explicitly approved"],
        "triage": ["impact and affected services are known"],
        "contain": ["risky automation is paused or constrained"],
        "diagnose": ["probable cause is identified"],
        "recover": ["recovery action is applied"],
        "verify": ["tests or monitoring confirm recovery"],
        "postmortem": ["root cause and prevention are saved as memory"],
    }
    return criteria.get(key, [f"{workflow} step '{key}' has a clear done state"])


def generate_plan_from_goal(
    goal: str,
    *,
    title: str | None = None,
    project: str | None = None,
    owner: str | None = None,
    workflow: str | None = None,
    context: dict[str, Any] | None = None,
    create: bool = True,
) -> dict[str, Any]:
    if not goal.strip():
        raise ValueError("goal is required")
    inferred = infer_workflow(goal, workflow)
    selected_workflow = inferred["workflow"]
    context = {"owner": owner, **(context or {})}
    task_specs = _task_specs_for_goal(goal, selected_workflow, context)
    planner_meta = {
        "planner": {
            "generated": True,
            "workflow": selected_workflow,
            "confidence": inferred["confidence"],
            "reason": inferred["reason"],
            "risk_flags": _risk_flags(goal, selected_workflow),
        }
    }
    if not create:
        return {
            "created": False,
            "workflow": selected_workflow,
            "confidence": inferred["confidence"],
            "rationale": inferred["reason"],
            "plan": {"title": title or _title_from_goal(goal), "project": project, "goal": goal, "meta": planner_meta},
            "tasks": task_specs,
            "next_action": task_specs[0] if task_specs else None,
        }

    plan_id = create_plan(
        title or _title_from_goal(goal),
        project=project,
        goal=goal,
        owner=owner,
        meta=planner_meta,
    )
    key_to_task_id: dict[str, str] = {}
    for spec in task_specs:
        planner = spec["meta"]["planner"]
        task_id = add_task(
            plan_id,
            spec["title"],
            type=spec["type"],
            status=spec["status"],
            assignee=spec.get("assignee"),
            priority=spec["priority"],
            note=spec["note"],
            meta=spec["meta"],
        )
        key_to_task_id[planner["key"]] = task_id
    for task in list_tasks(plan_id=plan_id, limit=500):
        planner = (task.get("meta") or {}).get("planner") or {}
        dep_ids = [key_to_task_id[k] for k in planner.get("depends_on_keys", []) if k in key_to_task_id]
        if dep_ids:
            meta = task.get("meta") or {}
            meta["planner"] = {**planner, "depends_on": dep_ids}
            update_task(task["task_id"], meta=meta)

    plan = get_plan(plan_id)
    return {
        "created": True,
        "workflow": selected_workflow,
        "confidence": inferred["confidence"],
        "rationale": inferred["reason"],
        "plan": plan,
        "tasks": plan.get("tasks", []) if plan else [],
        "next_action": recommend_next_action(plan_id=plan_id).get("task"),
    }


def _title_from_goal(goal: str) -> str:
    clean = " ".join(goal.strip().split())
    if len(clean) <= 72:
        return clean
    return clean[:69].rstrip() + "..."


def recommend_next_action(*, plan_id: str | None = None, project: str | None = None) -> dict[str, Any]:
    tasks = list_tasks(plan_id=plan_id, project=project, limit=500)
    by_id = {task["task_id"]: task for task in tasks}
    blocked = []
    candidates = []
    for task in tasks:
        if task["status"] not in ("todo", "doing", "waiting_approval"):
            continue
        planner = (task.get("meta") or {}).get("planner") or {}
        deps = planner.get("depends_on") or []
        unmet = [dep for dep in deps if by_id.get(dep, {}).get("status") != "done"]
        if unmet:
            blocked.append({"task": task, "unmet_dependencies": unmet})
            continue
        candidates.append(task)
    candidates.sort(key=lambda t: (
        0 if t["status"] == "waiting_approval" else 1 if t["status"] == "doing" else 2,
        -int(t.get("priority") or 0),
        t.get("created_at") or "",
    ))
    task = candidates[0] if candidates else None
    return {
        "task": task,
        "reason": _next_action_reason(task) if task else "No unblocked actionable task found.",
        "blocked": blocked[:10],
    }


def _next_action_reason(task: dict | None) -> str:
    if not task:
        return "No task selected."
    if task.get("status") == "waiting_approval":
        return "Approval is the highest-leverage unblocker."
    if task.get("status") == "doing":
        return "Task is already in progress and should be driven to completion."
    return "Task has no unmet dependencies and the highest current priority."


def sync_approval_tasks() -> list[dict]:
    from . import governance

    created: list[dict] = []
    pending = governance.pending().get("items", [])
    pending_keys = {
        f"{item.get('target_id') or item.get('production_id')}::{item.get('gate')}"
        for item in pending
    }
    waiting = [
        task for task in list_tasks(status="waiting_approval", limit=500)
        if task.get("type") == "approval"
        and task.get("target_id")
        and (task.get("meta") or {}).get("gate")
    ]
    existing = {
        f"{task.get('target_id')}::{(task.get('meta') or {}).get('gate')}"
        for task in waiting
    }
    for task in waiting:
        gate = (task.get("meta") or {}).get("gate")
        key = f"{task.get('target_id')}::{gate}"
        if key in pending_keys:
            continue
        meta = dict(task.get("meta") or {})
        meta["approval_sync"] = {
            "status": "cleared",
            "resolved_at": _now(),
        }
        note = (task.get("note") or "").rstrip()
        resolution = "Governance gate cleared; approval task closed automatically."
        if resolution not in note:
            note = f"{note}\n\n{resolution}".strip()
        update_task(task["task_id"], status="done", note=note, meta=meta)
    for item in pending:
        key = f"{item.get('target_id') or item.get('production_id')}::{item.get('gate')}"
        if key in existing:
            continue
        task_id = add_task(
            None,
            f"Approve {item.get('gate')} for {item.get('title') or item.get('target_id')}",
            type="approval",
            status="waiting_approval",
            assignee="operator",
            priority=5,
            target_id=item.get("target_id") or item.get("production_id"),
            note=item.get("reason"),
            meta={"gate": item.get("gate"), "production_id": item.get("production_id")},
        )
        created.append({"task_id": task_id, **item})
    return created


def _task_priority_from_production(value: Any) -> int:
    try:
        score = int(value or 0)
    except (TypeError, ValueError):
        score = 0
    if score >= 85:
        return 5
    if score >= 70:
        return 4
    if score >= 55:
        return 3
    return 2


def sync_production_tasks() -> list[dict]:
    """Mirror actionable production recommendations into operating tasks."""
    from . import production

    created: list[dict] = []
    active_statuses = {"todo", "doing", "blocked", "waiting_approval"}
    existing = {
        (
            t.get("target_id"),
            ((t.get("meta") or {}).get("production") or {}).get("state"),
            ((t.get("meta") or {}).get("production") or {}).get("next_action"),
        )
        for t in list_tasks(limit=500)
        if t.get("type") == "production" and t.get("target_id") and t.get("status") in active_statuses
    }
    for prod in production.list_productions(limit=500):
        state = prod.get("state")
        if state in {"publish", "measure"}:
            continue
        intel = prod.get("intelligence") or {}
        if intel.get("gate_status") == "blocked":
            continue
        next_action = intel.get("next_action") or "Review production"
        key = (prod.get("production_id"), state, next_action)
        if key in existing:
            continue
        priority = _task_priority_from_production(intel.get("priority"))
        task_id = add_task(
            None,
            f"{next_action} for {prod.get('title')}",
            type="production",
            status="todo",
            assignee=intel.get("next_actor") or prod.get("owner") or "operator",
            priority=priority,
            target_id=prod.get("production_id"),
            note=intel.get("next_reason") or f"Production is in {state}.",
            meta={
                "production": {
                    "production_id": prod.get("production_id"),
                    "title": prod.get("title"),
                    "project": prod.get("project"),
                    "format": prod.get("format"),
                    "state": state,
                    "next_action": next_action,
                    "next_state": intel.get("next_state"),
                    "asset_status": intel.get("asset_status"),
                    "confidence": intel.get("confidence"),
                    "readiness": intel.get("readiness"),
                    "priority_score": intel.get("priority"),
                }
            },
        )
        created.append({"task_id": task_id, "production_id": prod.get("production_id"), "next_action": next_action})
    return created


def overview() -> dict[str, Any]:
    sync_approval_tasks()
    sync_production_tasks()
    plans = list_plans(status="active", limit=50)
    tasks = list_tasks(limit=100)
    waiting = [t for t in tasks if t["status"] == "waiting_approval"]
    blocked = [t for t in tasks if t["status"] == "blocked"]
    doing = [t for t in tasks if t["status"] == "doing"]
    production_tasks = [t for t in tasks if t["type"] == "production" and t["status"] in ("todo", "doing")]
    return {
        "stats": {
            "active_plans": len(plans),
            "doing": len(doing),
            "blocked": len(blocked),
            "waiting_approval": len(waiting),
            "production_tasks": len(production_tasks),
        },
        "plans": plans,
        "tasks": tasks,
        "approval_tasks": waiting,
        "production_tasks": production_tasks,
        "project_memory": list_project_memory(limit=20),
    }


def daily_brief() -> dict[str, Any]:
    from . import governance, production

    sync_approval_tasks()
    sync_production_tasks()
    active_tasks = [t for t in list_tasks(limit=200) if t["status"] in ("todo", "doing", "blocked", "waiting_approval")]
    productions = production.list_productions(limit=20)
    pending = governance.pending().get("items", [])
    lines = []
    if pending:
        lines.append(f"{len(pending)} approval gate(s) need attention.")
    doing = [t for t in active_tasks if t["status"] == "doing"]
    if doing:
        lines.append(f"{len(doing)} task(s) are currently in motion.")
    production_tasks = [t for t in active_tasks if t["type"] == "production" and t["status"] in ("todo", "doing")]
    if production_tasks:
        lines.append(f"{len(production_tasks)} production next-action task(s) are queued.")
    review = [p for p in productions if p.get("state") == "review"]
    if review:
        lines.append(f"{len(review)} production(s) are waiting in review.")
    if not lines:
        lines.append("No urgent operating blockers recorded.")
    return {
        "date": _now()[:10],
        "summary": " ".join(lines),
        "priorities": active_tasks[:8],
        "pending_approvals": pending,
        "productions": productions[:8],
        "project_memory": list_project_memory(limit=8),
    }


def _with_counts(plan: dict) -> dict:
    if "tasks" in plan:
        tasks = plan["tasks"]
    else:
        tasks = list_tasks(plan_id=plan["plan_id"], limit=500)
    plan["task_counts"] = {
        status: sum(1 for task in tasks if task.get("status") == status)
        for status in TASK_STATUSES
    }
    return plan
