"""Persistent content production pipeline."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

STATES = (
    "idea",
    "brief",
    "research",
    "outline",
    "draft",
    "asset_plan",
    "render",
    "review",
    "publish",
    "measure",
)
STATE_ORDER = {state: i for i, state in enumerate(STATES)}
FORMATS = (
    "linkedin_short",
    "explainer_carousel",
    "talking_head_clip",
    "policy_briefing",
    "course_teaser",
    "proposal_walkthrough",
)
JSON_FIELDS = {
    "brief", "research", "script", "asset_plan", "edit_plan", "review",
    "linked_assets", "publish_targets", "gates",
}

_DB_PATH = Path(os.getenv("MEDIA_DB_PATH", "data/media.db"))

_NEXT_ACTIONS = {
    "idea": ("Build brief", "brief-builder", "Turn the idea into a structured production brief."),
    "brief": ("Gather research", "research-producer", "Add evidence, citations, and source material."),
    "research": ("Create outline", "scriptwriter", "Shape the evidence into a narrative outline."),
    "outline": ("Draft script", "scriptwriter", "Write the first production-ready draft."),
    "draft": ("Plan assets", "storyboarder", "Map scenes, moments, and required assets."),
    "asset_plan": ("Prepare render", "editor", "Prepare the edit plan and render package."),
    "render": ("Run QA review", "qa-brand-reviewer", "Review claims, brand, rights, and accessibility."),
    "review": ("Approve publish", "operator", "Clear governance gates before anything goes live."),
    "publish": ("Record measures", "operator", "Capture outcome and performance signals."),
    "measure": ("Complete", "operator", "Production is through the measured workflow."),
}

ACTION_DEFINITIONS = {
    "brief": {"label": "Build brief", "from": "idea", "to": "brief"},
    "research": {"label": "Gather research", "from": "brief", "to": "research"},
    "outline": {"label": "Create outline", "from": "research", "to": "outline"},
    "draft": {"label": "Draft script", "from": "outline", "to": "draft"},
    "assets": {"label": "Plan assets", "from": "draft", "to": "asset_plan"},
    "render": {"label": "Prepare render", "from": "asset_plan", "to": "render"},
    "review": {"label": "Move to review", "from": "render", "to": "review"},
    "publish": {"label": "Approve publish", "from": "review", "to": "publish"},
    "measure": {"label": "Record measures", "from": "publish", "to": "measure"},
}

_TRANSITION_AGENTS = {
    ("idea", "brief"): [("brief-builder", "brief")],
    ("brief", "research"): [("research-producer", "research")],
    ("research", "outline"): [("scriptwriter", "script")],
    ("outline", "draft"): [("scriptwriter", "script")],
    ("draft", "asset_plan"): [("storyboarder", "asset_plan"), ("visual-director", "asset_plan")],
    ("asset_plan", "render"): [("editor", "edit_plan")],
    ("review", "publish"): [("qa-brand-reviewer", "review")],
}


async def call_wijerco_agent(**kwargs):
    from .wijerco_agent import call_wijerco_agent as _call_wijerco_agent

    return await _call_wijerco_agent(**kwargs)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS productions (
            production_id TEXT PRIMARY KEY,
            title         TEXT NOT NULL,
            project       TEXT,
            format        TEXT NOT NULL,
            state         TEXT NOT NULL DEFAULT 'idea',
            brief         TEXT,
            research      TEXT,
            script        TEXT,
            asset_plan    TEXT,
            edit_plan     TEXT,
            review        TEXT,
            linked_assets TEXT,
            publish_targets TEXT,
            gates         TEXT,
            owner         TEXT,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS production_events (
            id            TEXT PRIMARY KEY,
            production_id TEXT NOT NULL,
            at            TEXT NOT NULL,
            from_state    TEXT,
            to_state      TEXT NOT NULL,
            actor         TEXT,
            note          TEXT
        )
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(productions)").fetchall()}
    if "publish_targets" not in columns:
        conn.execute("ALTER TABLE productions ADD COLUMN publish_targets TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_productions_state ON productions(state)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_productions_project ON productions(project)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_production_events_prod ON production_events(production_id,at)")
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


def _row_to_production(row: sqlite3.Row, *, with_events: bool = False) -> dict:
    data = dict(row)
    for field in JSON_FIELDS:
        default = [] if field in {"linked_assets", "publish_targets"} else {}
        data[field] = _loads(data.get(field), default)
    if with_events:
        with _db() as conn:
            events = conn.execute(
                "SELECT * FROM production_events WHERE production_id=? ORDER BY at ASC",
                (data["production_id"],),
            ).fetchall()
        data["events"] = [dict(e) for e in events]
    data["intelligence"] = _intelligence(data)
    return data


def _slice_ready(value: Any) -> bool:
    if isinstance(value, dict):
        return any(v not in ({}, [], "", None) for v in value.values())
    if isinstance(value, list):
        return bool(value)
    return bool(value)


def _asset_status(prod: dict[str, Any]) -> str:
    linked = prod.get("linked_assets") or []
    if linked:
        return f"{len(linked)} linked"
    if _slice_ready(prod.get("asset_plan")):
        return "planned"
    return "none"


def _priority(prod: dict[str, Any], pending_gates: list[dict[str, Any]]) -> int:
    state = prod.get("state") or "idea"
    base = 48 + (STATE_ORDER.get(state, 0) * 5)
    if state in {"review", "render"}:
        base += 12
    if pending_gates:
        base += 10
    if _slice_ready(prod.get("research")):
        base += 4
    if prod.get("project"):
        base += 4
    return max(1, min(100, base))


def _intelligence(prod: dict[str, Any]) -> dict[str, Any]:
    state = prod.get("state") or "idea"
    label, actor, reason = _NEXT_ACTIONS.get(state, ("Review production", "operator", "Check the production state."))
    next_state = STATES[min(STATE_ORDER.get(state, 0) + 1, len(STATES) - 1)] if state in STATE_ORDER else state
    pending_gates: list[dict[str, Any]] = []
    try:
        from . import governance

        pending_gates = governance.pending_gates(prod, next_state)
    except Exception:
        pending_gates = []
    missing = [
        key for key in ("brief", "research", "script", "asset_plan", "edit_plan", "review")
        if not _slice_ready(prod.get(key))
    ]
    readiness = "blocked" if pending_gates else "ready" if state in {"publish", "measure"} else "in_progress"
    confidence = "High" if len(missing) <= 2 else "Medium" if len(missing) <= 4 else "Low"
    return {
        "next_action": label,
        "next_actor": actor,
        "next_reason": reason,
        "next_state": next_state,
        "gate_status": "blocked" if pending_gates else "clear",
        "pending_gates": pending_gates,
        "asset_status": _asset_status(prod),
        "missing_slices": missing,
        "readiness": readiness,
        "confidence": confidence,
        "priority": _priority(prod, pending_gates),
    }


def _parse_agent_output(text: Any) -> dict:
    if isinstance(text, dict):
        if "answer" in text and isinstance(text["answer"], str):
            return _parse_agent_output(text["answer"])
        return text
    raw = str(text or "").strip()
    if not raw:
        return {"_raw": ""}
    fenced = raw
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 3:
            fenced = parts[1].strip()
            if fenced.lower().startswith("json"):
                fenced = fenced[4:].strip()
    try:
        return json.loads(fenced)
    except Exception:
        return {"_raw": raw}


def create_production(
    title: str,
    project: str | None,
    format: str,
    owner: str | None = None,
    publish_targets: list[Any] | None = None,
) -> str:
    if format not in FORMATS:
        raise ValueError(f"format must be one of {FORMATS}")
    pid = str(uuid.uuid4())
    now = _now()
    with _db() as conn:
        conn.execute(
            "INSERT INTO productions (production_id,title,project,format,state,brief,research,script,"
            "asset_plan,edit_plan,review,linked_assets,publish_targets,gates,owner,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, title, project, format, "idea", "{}", "{}", "{}", "{}",
             "{}", "{}", "[]", json.dumps(publish_targets or [], ensure_ascii=False),
             "{}", owner, now, now),
        )
    return pid


def get_production(production_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM productions WHERE production_id=?", (production_id,)).fetchone()
    return _row_to_production(row, with_events=True) if row else None


def list_productions(state: str | None = None, project: str | None = None, limit: int = 200) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if state:
        clauses.append("state=?")
        params.append(state)
    if project:
        clauses.append("project=?")
        params.append(project)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM productions {where} ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_production(row) for row in rows]


def update_production(production_id: str, **fields: Any) -> bool:
    allowed = {
        "title", "project", "format", "state", "brief", "research", "script",
        "asset_plan", "edit_plan", "review", "linked_assets", "publish_targets", "gates", "owner",
    }
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"field {key!r} is not editable")
        if key == "format" and value not in FORMATS:
            raise ValueError(f"format must be one of {FORMATS}")
        if key == "state" and value not in STATES:
            raise ValueError(f"state must be one of {STATES}")
        if key in JSON_FIELDS:
            value = json.dumps(value or ([] if key == "linked_assets" else {}), ensure_ascii=False)
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        return False
    sets.append("updated_at=?")
    params.append(_now())
    params.append(production_id)
    with _db() as conn:
        cur = conn.execute(f"UPDATE productions SET {', '.join(sets)} WHERE production_id=?", params)
    return cur.rowcount > 0


def record_event(production_id: str, from_state: str | None, to_state: str, actor: str, note: str = "") -> str:
    eid = str(uuid.uuid4())
    with _db() as conn:
        conn.execute(
            "INSERT INTO production_events (id,production_id,at,from_state,to_state,actor,note) "
            "VALUES (?,?,?,?,?,?,?)",
            (eid, production_id, _now(), from_state, to_state, actor, note),
        )
    return eid


def transition(production_id: str, to_state: str, actor: str = "operator", note: str = "") -> dict:
    if to_state not in STATES:
        raise ValueError(f"to_state must be one of {STATES}")
    prod = get_production(production_id)
    if not prod:
        raise KeyError("production not found")
    from_state = prod["state"]
    update_production(production_id, state=to_state)
    record_event(production_id, from_state, to_state, actor, note)
    return get_production(production_id) or {}


def _merge_slice(existing: Any, output: dict, from_state: str, to_state: str, subagent: str) -> dict:
    base = existing if isinstance(existing, dict) else {}
    key = "outline" if to_state == "outline" else ("draft" if to_state == "draft" else subagent.replace("-", "_"))
    if not base:
        return output
    merged = dict(base)
    merged[key] = output
    return merged


def _agent_query(prod: dict, from_state: str, to_state: str, subagent: str) -> str:
    return (
        f"Advance this content production from {from_state} to {to_state}.\n"
        f"Subagent: {subagent}.\n"
        "Return JSON that matches your role contract.\n\n"
        f"Production record:\n{json.dumps(prod, ensure_ascii=False, indent=2)}"
    )


async def _run_agent(prod: dict, from_state: str, to_state: str, subagent: str) -> dict:
    result = await call_wijerco_agent(
        department="content_studio",
        query=_agent_query(prod, from_state, to_state, subagent),
        rag_context=[],
        conversation_history=[],
        subagent=subagent,
    )
    return _parse_agent_output(result.get("answer") if isinstance(result, dict) else result)


async def _ping_blocked(production_id: str, gate: str) -> None:
    notifier_url = os.getenv("NOTIFIER_URL", "http://localhost:8004")
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            await client.post(
                f"{notifier_url}/notify",
                json={
                    "title": "Production gate needs approval",
                    "body": f"{gate} is blocking production {production_id}",
                    "tags": ["production", "governance"],
                },
                timeout=5.0,
            )
    except Exception:
        pass


async def advance(production_id: str, actor: str = "operator") -> dict:
    from . import governance
    from media import render as render_service

    prod = get_production(production_id)
    if not prod:
        raise KeyError("production not found")
    state = prod["state"]
    idx = STATE_ORDER[state]
    if idx >= len(STATES) - 1:
        return {"production": prod, "agent_output": {}, "done": True}
    next_state = STATES[idx + 1]

    pending = governance.pending_gates(prod, next_state)
    if pending:
        await _ping_blocked(production_id, pending[0]["gate"])
        return {"blocked": True, "gate": pending[0]["gate"], "pending": pending, "production": prod}

    agent_outputs: list[dict[str, Any]] = []
    for subagent, field in _TRANSITION_AGENTS.get((state, next_state), []):
        output = await _run_agent(prod, state, next_state, subagent)
        current = prod.get(field) or {}
        update_production(production_id, **{field: _merge_slice(current, output, state, next_state, subagent)})
        prod = get_production(production_id) or prod
        agent_outputs.append({"subagent": subagent, "field": field, "output": output})

    if state == "asset_plan" and next_state == "render":
        render_result = render_service.render(production_id, prod["format"], _render_props(prod))
        if render_result.get("asset_id"):
            linked = list(prod.get("linked_assets") or [])
            linked.append(render_result["asset_id"])
            update_production(production_id, linked_assets=linked)
        agent_outputs.append({"subagent": "render-service", "field": "linked_assets", "output": render_result})

    updated = transition(production_id, next_state, actor, f"advanced from {state}")
    if next_state == "publish" and updated.get("publish_targets"):
        from publishers import service as publication_service

        publication_results = await publication_service.publish_targets(updated, actor=actor)
        agent_outputs.append({
            "subagent": "publication-service",
            "field": "publications",
            "output": publication_results,
        })
    return {"production": updated, "agent_output": agent_outputs}


async def run_action(production_id: str, action: str, actor: str = "operator") -> dict:
    action_key = (action or "").strip().lower()
    if action_key not in ACTION_DEFINITIONS:
        raise ValueError(f"action must be one of {tuple(ACTION_DEFINITIONS)}")
    prod = get_production(production_id)
    if not prod:
        raise KeyError("production not found")
    spec = ACTION_DEFINITIONS[action_key]
    if prod.get("state") != spec["from"]:
        raise ValueError(
            f"{spec['label']} can only run from {spec['from']}; "
            f"production is currently {prod.get('state')}"
        )
    result = await advance(production_id, actor=actor)
    result["action"] = {"key": action_key, **spec}
    return result


def _render_props(prod: dict) -> dict:
    from media import render as render_service

    return render_service.build_props(prod)


def board() -> dict[str, list[dict[str, str]]]:
    columns = {"Ideas": [], "Drafting": [], "In Production": [], "Review": [], "Published": []}
    mapping = {
        "idea": "Ideas",
        "brief": "Ideas",
        "research": "Drafting",
        "outline": "Drafting",
        "draft": "Drafting",
        "asset_plan": "In Production",
        "render": "In Production",
        "review": "Review",
        "publish": "Published",
        "measure": "Published",
    }
    for prod in list_productions(limit=500):
        state = prod["state"]
        intel = prod.get("intelligence") or {}
        st = (
            "st-fail" if intel.get("gate_status") == "blocked"
            else "st-good" if state in ("publish", "measure")
            else "st-warn" if state in ("review", "render")
            else "st-mute"
        )
        columns[mapping[state]].append({
            "title": prod["title"],
            "t": prod["title"],
            "cap": prod["format"],
            "p": prod["format"],
            "status": state,
            "st": st,
            "meta": prod.get("project") or prod.get("owner") or "",
            "m": state,
            "production_id": prod["production_id"],
            "next_action": intel.get("next_action", ""),
            "next_actor": intel.get("next_actor", ""),
            "gate_status": intel.get("gate_status", "clear"),
            "asset_status": intel.get("asset_status", "none"),
            "confidence": intel.get("confidence", "Low"),
            "priority": intel.get("priority", 1),
            "readiness": intel.get("readiness", "in_progress"),
            "updated_at": prod.get("updated_at", ""),
        })
    return columns


def intelligence() -> dict[str, Any]:
    """Summarise production health and recommend the next operating move."""
    productions = list_productions(limit=500)
    active = [p for p in productions if p.get("state") not in {"publish", "measure"}]
    blocked: list[dict[str, Any]] = []
    weak_evidence: list[dict[str, Any]] = []
    asset_gaps: list[dict[str, Any]] = []
    ready_to_generate: list[dict[str, Any]] = []
    ready_actions: list[dict[str, Any]] = []

    for prod in active:
        intel = prod.get("intelligence") or {}
        item = {
            "production_id": prod.get("production_id"),
            "title": prod.get("title"),
            "project": prod.get("project"),
            "format": prod.get("format"),
            "state": prod.get("state"),
            "priority": intel.get("priority", 0),
            "confidence": intel.get("confidence", "Low"),
            "next_action": intel.get("next_action", "Review production"),
            "next_actor": intel.get("next_actor", "operator"),
            "reason": intel.get("next_reason", ""),
            "gate_status": intel.get("gate_status", "clear"),
            "asset_status": intel.get("asset_status", "none"),
        }
        if intel.get("gate_status") == "blocked":
            blocked.append({**item, "pending_gates": intel.get("pending_gates", [])})
            continue
        ready_actions.append(item)
        state_index = STATE_ORDER.get(str(prod.get("state")), 0)
        if state_index >= STATE_ORDER["research"] and not _slice_ready(prod.get("research")):
            weak_evidence.append({**item, "reason": "Research slice is empty for a production beyond the research stage."})
        if prod.get("state") in {"draft", "asset_plan", "render", "review"} and intel.get("asset_status") == "none":
            asset_gaps.append({**item, "reason": "Production is approaching render/review without linked or planned assets."})
        if prod.get("state") in {"asset_plan", "render", "review"} and intel.get("asset_status") == "planned":
            ready_to_generate.append({**item, "reason": "Asset plan exists and can be sent to media generation."})

    ready_actions.sort(key=lambda x: (-int(x.get("priority") or 0), str(x.get("title") or "")))
    blocked.sort(key=lambda x: (-int(x.get("priority") or 0), str(x.get("title") or "")))
    weak_evidence.sort(key=lambda x: (-int(x.get("priority") or 0), str(x.get("title") or "")))
    asset_gaps.sort(key=lambda x: (-int(x.get("priority") or 0), str(x.get("title") or "")))
    ready_to_generate.sort(key=lambda x: (-int(x.get("priority") or 0), str(x.get("title") or "")))

    next_best = ready_actions[0] if ready_actions else (blocked[0] if blocked else None)
    daily_run = []
    if blocked:
        daily_run.append({
            "title": "Clear production approvals",
            "detail": f"{len(blocked)} production(s) are blocked by governance gates.",
            "type": "approval",
        })
    if next_best and next_best not in blocked:
        daily_run.append({
            "title": next_best.get("next_action"),
            "detail": next_best.get("title"),
            "type": "production",
            "production_id": next_best.get("production_id"),
        })
    if weak_evidence:
        daily_run.append({
            "title": "Repair weak evidence",
            "detail": f"{len(weak_evidence)} production(s) need evidence before review.",
            "type": "quality",
        })
    if ready_to_generate:
        daily_run.append({
            "title": "Generate planned media",
            "detail": f"{len(ready_to_generate)} production(s) have an asset plan ready for media tools.",
            "type": "media",
        })
    if asset_gaps:
        daily_run.append({
            "title": "Resolve asset gaps",
            "detail": f"{len(asset_gaps)} production(s) need asset planning or linked assets.",
            "type": "production",
        })

    return {
        "summary": {
            "active": len(active),
            "blocked": len(blocked),
            "weak_evidence": len(weak_evidence),
            "asset_gaps": len(asset_gaps),
            "ready_to_generate": len(ready_to_generate),
            "ready_actions": len(ready_actions),
        },
        "next_best": next_best,
        "blocked": blocked[:10],
        "weak_evidence": weak_evidence[:10],
        "asset_gaps": asset_gaps[:10],
        "ready_to_generate": ready_to_generate[:10],
        "daily_run": daily_run[:6],
    }
