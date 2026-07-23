"""
Dashboard endpoints for the Command Centre's pages.

Each source is authored in the Obsidian vault as one note per item, under
`<vault>/13_Command Centre/<Source>/`. The YAML frontmatter of each note holds
the structured fields the UI needs; the note body is free text for detail.

    13_Command Centre/
      Deliverables/      -> GET /deliverables
      Content Pipeline/  -> GET /content/pipeline   (grouped by frontmatter `col`)
      Engagements/       -> GET /engagements
      Sector Intel/      -> GET /intel/feed
      Scheduled Runs/    -> GET /schedule/list
      Knowledge Base/    -> GET /kb/overview         (sources + derived stats)
      Memory/            -> GET /memory/overview     (grouped by frontmatter `g`)
      Routing/           -> GET /trace/recent

The vault location comes from OBSIDIAN_VAULT_PATH (the same variable the RAG
indexer uses), falling back to the value in the repo .env. If the vault or a
source folder is missing, the endpoint returns built-in seed data so no page
errors. Manage content by adding, editing, or deleting notes in Obsidian.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from common.security import require_admin, audit_log, backup_file

router = APIRouter(tags=["dashboard"])

CC_FOLDER = "13_Command Centre"
WRITING_PIPELINE_FOLDER = "09_Writing Pipline"
WRITING_PIPELINE_FALLBACKS = [
    "09_Writing Pipline",
    "09_Writing Pipeline",
]
WRITING_STAGES = [
    ("00_Ideas", "Ideas"),
    ("01_Drafts", "Drafts"),
    ("02_Editing", "Editing"),
    ("03_Ready", "Ready"),
    ("04_Published", "Published"),
]
CONTENT_ACTION_STAGE = {
    "brief": "00_Ideas",
    "evidence": "00_Ideas",
    "draft": "01_Drafts",
    "voice": "02_Editing",
    "review": "03_Ready",
    "publish": "04_Published",
}
ASSISTED_ACTION_SECTIONS = {
    "expand_idea": "Idea Expansion Plan",
    "find_evidence": "Evidence Improvement Plan",
    "recommend_format": "Format Recommendation Plan",
    "improve_hook": "Hook Improvement Plan",
    "voice_check": "Voice Improvement Plan",
    "move_to_editing": "Editing Readiness Plan",
    "tighten_voice": "Voice Tightening Plan",
    "prepare_qa": "QA Preparation Plan",
    "resolve_gaps": "Gap Resolution Plan",
    "qa_review": "QA Action Plan",
    "publish": "Publish Action Plan",
    "prepare_publish_handoff": "Publish Handoff Plan",
    "record_outcome": "Outcome Tracking Plan",
    "archive_learning": "Archive Learning Plan",
}

# ---------------------------------------------------------------------------
# Vault location + frontmatter reading
# ---------------------------------------------------------------------------

def _vault_root() -> Path | None:
    p = os.getenv("OBSIDIAN_VAULT_PATH")
    if not p:
        try:
            env = Path(__file__).parent.parent / ".env"
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("OBSIDIAN_VAULT_PATH="):
                    p = line.split("=", 1)[1].strip()
                    break
        except Exception:
            pass
    return Path(p) if p else None


def _base() -> Path | None:
    root = _vault_root()
    return (root / CC_FOLDER) if root else None


_NUM_INT = re.compile(r"-?\d+")
_NUM_FLOAT = re.compile(r"-?\d+\.\d+")


def _coerce(v: str) -> Any:
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        return v[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if _NUM_FLOAT.fullmatch(v):
        return float(v)
    if _NUM_INT.fullmatch(v):
        return int(v)
    if v in ("true", "false"):
        return v == "true"
    return v


def _parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end].strip("\n")
    out: dict[str, Any] = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line or ":" not in line or line.lstrip().startswith("#"):
            continue
        key, val = line.split(":", 1)
        out[key.strip()] = _coerce(val.strip())
    return out


def _load_items(folder: str) -> list[dict[str, Any]] | None:
    """Read every note in a source folder into a list of frontmatter dicts."""
    base = _base()
    if not base:
        return None
    d = base / folder
    if not d.is_dir():
        return None
    items: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.md")):
        try:
            fm = _parse_frontmatter(p.read_text(encoding="utf-8"))
            if fm:
                items.append(fm)
        except Exception:
            continue
    return items or None


def create_deliverable_from_production(production: dict[str, Any], *, actor: str = "operator", note: str = "") -> dict[str, Any]:
    """Create a vault-backed Deliverables note from a completed production."""
    base = _base()
    vault = _vault_root()
    if not base or not vault:
        raise RuntimeError("OBSIDIAN_VAULT_PATH is not configured")
    folder = base / "Deliverables"
    folder.mkdir(parents=True, exist_ok=True)
    title = production.get("title") or "Untitled production"
    path = folder / f"{_slugify_title(title)}.md"
    i = 2
    while path.exists():
        path = folder / f"{_slugify_title(title)}-{i}.md"
        i += 1
    state = production.get("state") or ""
    review = production.get("review") or {}
    evidence = "Strong evidence" if production.get("research") else "Evidence not recorded"
    fm = {
        "title": title,
        "cap": "Content Studio",
        "type": production.get("format") or "Production",
        "status": "Reviewed" if state in ("publish", "measure") else state,
        "st": "st-good" if state in ("publish", "measure") else "st-warn",
        "meta": f"Production {state}",
        "production_id": production.get("production_id"),
        "project": production.get("project") or "",
        "source": "Production",
        "readiness": "Client-ready" if state in ("publish", "measure") else "Internal-ready",
        "evidence": evidence,
        "confidence": (production.get("intelligence") or {}).get("confidence") or "Medium",
        "next_action": "Prepare client handoff",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    fm = {k: v for k, v in fm.items() if v not in ("", None)}
    body = "\n".join([
        f"# {title}",
        "",
        "## Production Handoff",
        "",
        f"- Production ID: {production.get('production_id')}",
        f"- State: {state}",
        f"- Format: {production.get('format')}",
        f"- Project: {production.get('project') or 'Not recorded'}",
        f"- Actor: {actor}",
        f"- Note: {note or 'Not recorded'}",
        "",
        "## Readiness",
        "",
        f"- Evidence: {evidence}",
        f"- Review recorded: {'yes' if review else 'no'}",
        f"- Linked assets: {len(production.get('linked_assets') or [])}",
        "",
        "## Source Summary",
        "",
        "This deliverable was created from a Content Studio production record. "
        "Use the production ID to inspect the full brief, research, script, asset plan, review, approvals, and events.",
        "",
    ])
    _write_note(path, fm, body)
    audit_log("production.deliverable.create", {
        "production_id": production.get("production_id"),
        "path": path.relative_to(vault).as_posix(),
        "actor": actor,
    })
    return {
        "ok": True,
        "path": path.relative_to(vault).as_posix(),
        "item": fm,
    }


def _writing_root(create: bool = False) -> Path | None:
    root = _vault_root()
    if not root:
        return None
    configured = os.getenv("WRITING_PIPELINE_PATH", "").strip()
    candidates = [configured] if configured else []
    candidates.extend(p for p in WRITING_PIPELINE_FALLBACKS if p not in candidates)
    pipeline = root / candidates[0]
    for rel in candidates:
        candidate = root / rel
        if candidate.is_dir():
            pipeline = candidate
            break
    if create:
        for folder, _ in WRITING_STAGES:
            (pipeline / folder).mkdir(parents=True, exist_ok=True)
    return pipeline


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4 :].lstrip("\n")


def _slugify_title(title: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", title.lower()).strip("-")
    return slug[:80] or "content-item"


def _yaml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value or "").replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _note_frontmatter(item: dict[str, Any], stage_folder: str) -> dict[str, Any]:
    title = item.get("t") or item.get("title") or "Untitled content"
    pipeline = _writing_root(create=False)
    vault = _vault_root()
    pipeline_name = WRITING_PIPELINE_FOLDER
    if pipeline and vault:
        try:
            pipeline_name = pipeline.relative_to(vault).as_posix()
        except ValueError:
            pipeline_name = pipeline.name
    fm = {
        "title": title,
        "pipeline": pipeline_name,
        "stage": stage_folder,
        "col": dict(WRITING_STAGES).get(stage_folder, "Ideas"),
        "p": item.get("p") or item.get("platform") or item.get("channel") or "LinkedIn",
        "pillar": item.get("pillar") or "",
        "audience": item.get("audience") or "",
        "intent": item.get("intent") or "",
        "format": item.get("format") or "",
        "priority": item.get("priority") or item.get("score") or 50,
        "confidence": item.get("confidence") or "Medium",
        "effort": item.get("effort") or "Medium",
        "signal": item.get("signal") or "",
        "source": item.get("source") or "Command Centre",
        "evidence": item.get("evidence") or "",
        "next_action": item.get("next_action") or "",
        "updated": datetime.now().isoformat(timespec="seconds"),
    }
    return {k: v for k, v in fm.items() if v not in ("", None)}


def _frontmatter_block(fm: dict[str, Any]) -> str:
    return "---\n" + "\n".join(f"{k}: {_yaml_value(v)}" for k, v in fm.items()) + "\n---\n"


def _write_note(path: Path, fm: dict[str, Any], body: str) -> None:
    path.write_text(_frontmatter_block(fm) + body.lstrip("\n"), encoding="utf-8")


def _content_note_from_path(path: Path, stage_folder: str, stage_label: str, vault: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    fm = _parse_frontmatter(text)
    title = fm.get("title") or fm.get("t") or path.stem
    body = _strip_frontmatter(text)
    rel = path.relative_to(vault).as_posix()
    item = {
        **fm,
        "id": rel,
        "path": rel,
        "file": path.name,
        "t": title,
        "title": title,
        "stage": stage_folder,
        "col": stage_label,
        "body_excerpt": body.strip()[:420],
    }
    if "p" not in item:
        item["p"] = fm.get("channel") or fm.get("platform") or "LinkedIn"
    return item


def _load_writing_pipeline() -> dict[str, list[dict[str, Any]]] | None:
    vault = _vault_root()
    pipeline = _writing_root(create=False)
    if not vault or not pipeline or not pipeline.is_dir():
        return None
    cols: dict[str, list[dict[str, Any]]] = {label: [] for _, label in WRITING_STAGES}
    for stage_folder, stage_label in WRITING_STAGES:
        stage_dir = pipeline / stage_folder
        if not stage_dir.is_dir():
            continue
        for path in sorted(stage_dir.glob("*.md")):
            item = _content_note_from_path(path, stage_folder, stage_label, vault)
            if item:
                cols[stage_label].append(item)
    return cols if any(cols.values()) else None


def _resolve_content_note(item: dict[str, Any], stage_folder: str) -> tuple[Path, bool]:
    vault = _vault_root()
    pipeline = _writing_root(create=True)
    if not vault or not pipeline:
        raise HTTPException(status_code=503, detail="OBSIDIAN_VAULT_PATH is not configured")

    raw_path = item.get("path") or item.get("id")
    if raw_path:
        candidate = (vault / str(raw_path)).resolve()
        try:
            candidate.relative_to(vault.resolve())
        except ValueError:
            raise HTTPException(status_code=403, detail="Content note path is outside the vault")
        if candidate.is_file():
            return candidate, False

    title = item.get("t") or item.get("title") or "Untitled content"
    target_dir = pipeline / stage_folder
    target_dir.mkdir(parents=True, exist_ok=True)
    candidate = target_dir / f"{_slugify_title(title)}.md"
    i = 2
    while candidate.exists():
        candidate = target_dir / f"{_slugify_title(title)}-{i}.md"
        i += 1
    fm = _note_frontmatter(item, stage_folder)
    body = f"# {title}\n\n## Working Notes\n\nCreated from the Command Centre content pipeline.\n"
    _write_note(candidate, fm, body)
    audit_log("content.note.create", {"path": str(candidate)})
    return candidate, True


def _move_content_note(path: Path, target_stage: str, item: dict[str, Any]) -> Path:
    vault = _vault_root()
    pipeline = _writing_root(create=True)
    if not vault or not pipeline:
        raise HTTPException(status_code=503, detail="OBSIDIAN_VAULT_PATH is not configured")
    target_dir = pipeline / target_stage
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    if path.resolve() == target.resolve():
        return path
    i = 2
    while target.exists():
        target = target_dir / f"{path.stem}-{i}{path.suffix}"
        i += 1
    backup_file(path)
    shutil.move(str(path), str(target))
    audit_log("content.note.move", {"from": str(path), "to": str(target), "stage": target_stage})
    return target


def _append_agent_output(path: Path, action: str, result: str, stage_folder: str, item: dict[str, Any]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    body = _strip_frontmatter(text)
    fm = _parse_frontmatter(text)
    fm.update(_note_frontmatter({**fm, **item}, stage_folder))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    section_title = {
        "brief": "Content Brief",
        "evidence": "Evidence Plan",
        "draft": "Draft",
        "voice": "Voice Edit",
        "review": "QA Review",
        "publish": "Publication Record",
    }.get(action, action.title())
    addition = f"\n\n## {section_title}\n\n_Generated {stamp}_\n\n{result.strip()}\n"
    backup_file(path)
    _write_note(path, fm, body.rstrip() + addition)


def _append_content_section(path: Path, section_title: str, result: str, item: dict[str, Any]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    body = _strip_frontmatter(text)
    fm = _parse_frontmatter(text)
    stage_folder = str(fm.get("stage") or item.get("stage") or "00_Ideas")
    fm.update(_note_frontmatter({**fm, **item}, stage_folder))
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    addition = f"\n\n## {section_title}\n\n_Generated {stamp}_\n\n{result.strip()}\n"
    backup_file(path)
    _write_note(path, fm, body.rstrip() + addition)


def _intel_items_for_ideas(limit: int = 8, query: str | None = None) -> list[dict[str, Any]]:
    items = _load_items("Sector Intel") or SEED_INTEL
    if query:
        q = query.lower()
        items = [
            it for it in items
            if q in " ".join(str(v) for v in it.values()).lower()
        ]
    return items[:limit]


def _format_intel_context(items: list[dict[str, Any]]) -> str:
    lines = []
    for i, it in enumerate(items, 1):
        title = it.get("head") or it.get("title") or it.get("headline") or "Untitled signal"
        source = it.get("src") or it.get("source") or "Unknown source"
        date_s = it.get("date") or it.get("updated") or ""
        why = it.get("so") or it.get("why") or it.get("summary") or ""
        lines.append(f"{i}. {title}\n   Source: {source} {date_s}\n   Signal: {why}")
    return "\n".join(lines)


def _fallback_ideas_from_intel(items: list[dict[str, Any]], count: int) -> list[dict[str, str]]:
    ideas = []
    for it in items[:count]:
        title = it.get("head") or it.get("title") or it.get("headline") or "Sector signal"
        source = it.get("src") or it.get("source") or "sector intel"
        ideas.append({
            "title": f"What {title} changes for university leaders",
            "notes": f"Source signal: {title}\nSource: {source}\nWhy it matters: {it.get('so') or it.get('summary') or 'Not recorded.'}",
        })
    return ideas


def _ideas_from_agent_text(text: str, count: int) -> list[dict[str, str]]:
    ideas: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        title_match = re.match(r"^(?:[-*]|\d+[.)])\s*(?:title:\s*)?[\"']?(.+?)[\"']?$", line, flags=re.I)
        label_match = re.match(r"^title:\s*(.+)$", line, flags=re.I)
        if label_match or (title_match and len(ideas) < count and len(line) < 180):
            if current:
                ideas.append(current)
                if len(ideas) >= count:
                    break
            title = (label_match.group(1) if label_match else title_match.group(1)).strip()
            current = {"title": title, "notes": ""}
            continue
        if current:
            current["notes"] = (current["notes"] + "\n" + line).strip()
    if current and len(ideas) < count:
        ideas.append(current)
    return [x for x in ideas if x.get("title")][:count]


# ---------------------------------------------------------------------------
# Seed data — returned only if the vault or a folder is unavailable
# ---------------------------------------------------------------------------

SEED_DELIVERABLES = [
    {"title": "Online retention: sector benchmark and drivers", "cap": "Sector Intelligence & Evidence",
     "type": "PDF briefing", "status": "Reviewed", "st": "st-good", "meta": "18 pages · 12 Jun"},
    {"title": "MPH curriculum map and outcomes alignment", "cap": "Learning & Curriculum Design",
     "type": "Curriculum map", "status": "Draft for review", "st": "st-warn", "meta": "9 Jun"},
]
SEED_CONTENT = {
    "Ideas": [
        {"t": "The micro-credential demand gap nobody costs", "p": "Article",
         "pillar": "Micro-credentials and workforce demand", "audience": "University executives",
         "intent": "Educate", "format": "Long-form article", "priority": 91,
         "confidence": "Medium", "effort": "High",
         "next_action": "Build the cost model and add a concrete workforce-demand example.",
         "signal": "Strategic gap", "source": "Idea backlog", "evidence": "Partial",
         "due": "Next week"},
    ],
    "Drafts": [
        {"t": "Adaptive versus technical: where education leaders misread the problem",
         "p": "LinkedIn", "m": "draft 2", "pillar": "AI in academic operations",
         "audience": "Education leaders", "intent": "Provoke", "format": "LinkedIn post",
         "priority": 88, "confidence": "High", "effort": "Low",
         "next_action": "Tighten the hook and add a sharper contrast between systems change and tooling.",
         "signal": "Ready to sharpen", "source": "Draft", "evidence": "Strong",
         "due": "Today"},
    ],
    "Editing": [
        {"t": "What AI agents already do in academic operations", "p": "LinkedIn",
         "m": "Tue 8:00am", "pillar": "AI in academic operations",
         "audience": "Operations leaders", "intent": "Educate", "format": "LinkedIn post",
         "priority": 76, "confidence": "High", "effort": "Low",
         "next_action": "Prepare a follow-up prompt for comments and save useful replies.",
         "signal": "Good timing", "source": "Calendar", "evidence": "Strong",
         "due": "Tue 8:00am"},
    ],
    "Ready": [
        {"t": "Andragogy is not a footnote", "p": "Article", "m": "1.2k reads",
         "pillar": "Teaching quality and regulation", "audience": "Learning leaders",
         "intent": "Educate", "format": "Article", "priority": 69,
         "confidence": "High", "effort": "Medium",
         "next_action": "Run final QA review before publish.",
         "signal": "Ready for QA", "source": "Writing pipeline",
         "evidence": "Performance signal", "views": "1.2k reads",
         "engagement": "Steady"},
    ],
}
SEED_ENG = [
    {"cap": "Sector Intelligence & Evidence", "title": "First-year online retention, read and recommendation",
     "lead": "Insights Strategist", "milestone": "Recommendation ready for sign-off",
     "st": "st-warn", "stl": "Awaiting review", "pct": 90},
]
SEED_INTEL = [
    {"src": "TEQSA", "date": "11 Jun",
     "head": "Consultation opens on teaching-qualification expectations for academic staff",
     "so": "The forcing function you have been writing about."},
]
SEED_SCHED = [
    {"name": "Morning email triage", "when": "Daily · 7:00am", "next": "Tomorrow 7:00am", "st": "st-good", "stl": "OK"},
]
SEED_KB_SOURCES = [
    {"name": "WijerCo capability briefs", "chunks": 210, "fresh": "Current", "st": "st-good"},
    {"name": "TEQSA & AQF reference set", "chunks": 340, "fresh": "Current", "st": "st-good"},
]
SEED_MEMORY_GROUPS = [
    {"g": "Identity", "items": ["Aaron Wijeratne, Academic Director at OES, Swinburne Online"]},
    {"g": "Preferences", "items": ["Lead with the point; no preamble or filler"]},
]
SEED_TRACE = [
    {"q": "Draft a proposal for a micro-credential in data literacy", "dept": "Learning & Curriculum Design",
     "model": "claude-opus-4.8", "conf": 91, "sources": 6, "ms": 4200, "cost": 0.021},
]


class ContentActionRequest(BaseModel):
    action: str = Field(..., min_length=1)
    query: str = Field(..., min_length=1, max_length=8192)
    department: str | None = None
    subagent: str | None = None
    item: dict[str, Any] = Field(default_factory=dict)
    conversation_history: list[dict[str, Any]] = Field(default_factory=list)


class ContentIdeaCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=220)
    p: str = "LinkedIn"
    pillar: str | None = None
    audience: str | None = None
    intent: str | None = None
    format: str | None = None
    priority: int = 50
    confidence: str = "Medium"
    effort: str = "Medium"
    signal: str = "New idea"
    evidence: str = "Needs source"
    next_action: str | None = None
    notes: str | None = None


class ContentIntelIdeasRequest(BaseModel):
    count: int = Field(3, ge=1, le=8)
    pillar: str | None = None
    audience: str | None = None
    p: str = "Article"
    source_query: str | None = None
    use_agent: bool = True


class ContentAssistRequest(BaseModel):
    action: str = Field(..., min_length=1)
    label: str = Field(..., min_length=1)
    detail: str = ""
    item: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/deliverables")
async def deliverables() -> dict[str, Any]:
    return {"items": _load_items("Deliverables") or SEED_DELIVERABLES}


@router.get("/content/pipeline")
async def content_pipeline() -> Any:
    pipeline = _load_writing_pipeline()
    if pipeline:
        return pipeline

    items = _load_items("Content Pipeline")
    if not items:
        return SEED_CONTENT
    cols: dict[str, list] = {label: [] for _, label in WRITING_STAGES}
    legacy_map = {
        "Ideas": "Ideas",
        "Drafting": "Drafts",
        "Scheduled": "Editing",
        "Published": "Ready",
    }
    for it in items:
        col = legacy_map.get(str(it.get("col", "Ideas")), "Ideas")
        cols.setdefault(col, [])
        cols[col].append({k: v for k, v in it.items() if k != "col"})
    return cols


@router.post("/content/action", dependencies=[Depends(require_admin)])
async def content_action(req: ContentActionRequest) -> dict[str, Any]:
    action = req.action.strip()
    target_stage = CONTENT_ACTION_STAGE.get(action)
    if not target_stage:
        raise HTTPException(status_code=422, detail=f"unknown content action: {action}")

    from .wijerco_agent import call_wijerco_agent

    result = await call_wijerco_agent(
        department=req.department or "content_studio",
        query=req.query,
        conversation_history=req.conversation_history,
        subagent=req.subagent,
    )
    answer = result.get("answer") or ""
    if not answer.strip():
        raise HTTPException(status_code=502, detail=result.get("error") or "Agent returned no content")

    note, created = _resolve_content_note(req.item, target_stage)
    note = _move_content_note(note, target_stage, req.item)
    _append_agent_output(note, action, answer, target_stage, req.item)
    persisted_text = note.read_text(encoding="utf-8")
    if answer.strip() not in persisted_text:
        raise HTTPException(status_code=500, detail="Agent output was not persisted to the content note")

    vault = _vault_root()
    rel = note.relative_to(vault).as_posix() if vault else str(note)
    audit_log("content.action.execute", {
        "action": action,
        "department": req.department,
        "subagent": req.subagent,
        "path": rel,
        "created": created,
    })
    return {
        "ok": True,
        "answer": answer,
        "path": rel,
        "stage": target_stage,
        "created": created,
        "persisted": True,
        "department": result.get("department"),
        "subagent": req.subagent,
        "model": result.get("model_key"),
        "model_label": result.get("model_label"),
        "provider": result.get("provider"),
        "error": result.get("error"),
    }


@router.post("/content/ideas", dependencies=[Depends(require_admin)])
async def create_content_idea(req: ContentIdeaCreateRequest) -> dict[str, Any]:
    item = {
        "t": req.title.strip(),
        "p": req.p,
        "pillar": req.pillar or "Unassigned",
        "audience": req.audience or "Education leaders",
        "intent": req.intent or "Educate",
        "format": req.format or req.p,
        "priority": req.priority,
        "confidence": req.confidence,
        "effort": req.effort,
        "signal": req.signal,
        "evidence": req.evidence,
        "next_action": req.next_action or "Build content brief and find evidence.",
        "source": "Command Centre",
    }
    note, created = _resolve_content_note(item, "00_Ideas")
    if not created:
        raise HTTPException(status_code=409, detail="A writing idea with this path already exists")
    if req.notes:
        _append_agent_output(note, "notes", req.notes, "00_Ideas", item)
    vault = _vault_root()
    rel = note.relative_to(vault).as_posix() if vault else str(note)
    audit_log("content.idea.create", {"path": rel, "title": req.title})
    return {"ok": True, "path": rel, "item": {**item, "id": rel, "path": rel, "stage": "00_Ideas", "col": "Ideas"}}


@router.post("/content/ideas/from-intel", dependencies=[Depends(require_admin)])
async def create_content_ideas_from_intel(req: ContentIntelIdeasRequest) -> dict[str, Any]:
    intel = _intel_items_for_ideas(limit=max(req.count, 8), query=req.source_query)
    if not intel:
        raise HTTPException(status_code=404, detail="No sector intel signals found")
    context = _format_intel_context(intel)
    ideas: list[dict[str, str]] = []
    agent_answer = ""

    if req.use_agent:
        from .wijerco_agent import call_wijerco_agent

        prompt = (
            "Generate article ideas from these sector-intelligence signals.\n"
            "Return one idea per line. Each line should be a strong article title, followed by a short rationale after a dash.\n"
            f"Audience: {req.audience or 'education leaders'}\n"
            f"Strategic pillar: {req.pillar or 'choose the strongest matching pillar'}\n"
            f"Format: {req.p}\n"
            f"Number of ideas: {req.count}\n\n"
            f"Sector intel:\n{context}"
        )
        result = await call_wijerco_agent(
            department="research_intelligence",
            subagent="insights-strategist",
            query=prompt,
            conversation_history=[],
        )
        agent_answer = result.get("answer") or ""
        ideas = _ideas_from_agent_text(agent_answer, req.count)

    if not ideas:
        ideas = _fallback_ideas_from_intel(intel, req.count)

    created_items = []
    for idea in ideas[: req.count]:
        item = {
            "t": idea["title"],
            "p": req.p,
            "pillar": req.pillar or "Sector intelligence",
            "audience": req.audience or "Education leaders",
            "intent": "Educate",
            "format": req.p,
            "priority": 72,
            "confidence": "Medium",
            "effort": "Medium",
            "signal": "Generated from sector intel",
            "evidence": "Needs source",
            "next_action": "Build content brief and find evidence.",
            "source": "Sector Intel",
        }
        note, _ = _resolve_content_note(item, "00_Ideas")
        notes = (
            f"Generated from sector intelligence.\n\n"
            f"Rationale:\n{idea.get('notes') or 'Not recorded.'}\n\n"
            f"Source signals:\n{context}"
        )
        _append_agent_output(note, "intel", notes, "00_Ideas", item)
        vault = _vault_root()
        rel = note.relative_to(vault).as_posix() if vault else str(note)
        created_items.append({**item, "id": rel, "path": rel, "stage": "00_Ideas", "col": "Ideas"})

    audit_log("content.ideas.from_intel", {"count": len(created_items), "source_query": req.source_query})
    return {
        "ok": True,
        "items": created_items,
        "agent_answer": agent_answer,
        "source_count": len(intel),
    }


@router.post("/content/assist", dependencies=[Depends(require_admin)])
async def content_assist(req: ContentAssistRequest) -> dict[str, Any]:
    item = req.item or {}
    path, _ = _resolve_content_note(item, str(item.get("stage") or "00_Ideas"))
    text = path.read_text(encoding="utf-8")
    body = _strip_frontmatter(text).strip()
    fm = _parse_frontmatter(text)

    from .wijerco_agent import call_wijerco_agent

    prompt = (
        "Read the article draft below and produce an actionable improvement plan. "
        "Do not rewrite the whole article. Give concrete edits the writer should make, "
        "including where in the article the issue appears and what kind of change is needed.\n\n"
        f"Action: {req.label}\n"
        f"Action detail: {req.detail}\n"
        f"Title: {item.get('t') or fm.get('title') or path.stem}\n"
        f"Stage: {item.get('stage') or fm.get('stage') or ''}\n"
        f"Audience: {item.get('audience') or fm.get('audience') or 'Not recorded'}\n"
        f"Channel: {item.get('p') or fm.get('p') or 'Not recorded'}\n\n"
        "Return sections:\n"
        "1. Diagnosis\n"
        "2. Priority fixes\n"
        "3. Specific edit instructions\n"
        "4. Suggested replacement hook or sample language, if relevant\n"
        "5. Done-when checklist\n\n"
        f"Article Markdown:\n{body[:12000]}"
    )
    result = await call_wijerco_agent(
        department="content_studio",
        subagent="qa-brand-reviewer",
        query=prompt,
        conversation_history=[],
    )
    answer = result.get("answer") or ""
    if not answer.strip():
        raise HTTPException(status_code=502, detail=result.get("error") or "Agent returned no improvement plan")

    section = ASSISTED_ACTION_SECTIONS.get(req.action, f"Improvement Plan - {req.label}")
    _append_content_section(path, section, answer, item)
    persisted_text = path.read_text(encoding="utf-8")
    if answer.strip() not in persisted_text:
        raise HTTPException(status_code=500, detail="Improvement plan was not persisted to the content note")

    vault = _vault_root()
    rel = path.relative_to(vault).as_posix() if vault else str(path)
    audit_log("content.assist.execute", {"action": req.action, "label": req.label, "path": rel})
    return {
        "ok": True,
        "answer": answer,
        "path": rel,
        "persisted": True,
        "model": result.get("model_key"),
        "model_label": result.get("model_label"),
        "provider": result.get("provider"),
        "error": result.get("error"),
    }


@router.get("/engagements")
async def engagements() -> dict[str, Any]:
    return {"items": _load_items("Engagements") or SEED_ENG}


@router.get("/intel/feed")
async def intel_feed() -> dict[str, Any]:
    return {"items": _load_items("Sector Intel") or SEED_INTEL}


# ---------------------------------------------------------------------------
# Sector Intel benchmarking dataset (Phase 2 pipeline output). Read-only.
# ---------------------------------------------------------------------------
_SECTOR_PATHS = [
    Path(__file__).resolve().parent.parent / "sector-intel" / "data" / "published" / "sector_intel.json",
    Path(__file__).resolve().parent.parent / "ui" / "sector_intel.json",
]
_SEED_SECTOR = {
    "meta": {"sample": True, "status": "unavailable",
             "note": "Sector dataset not found on the server. Run the Phase 2 pipeline "
                     "(python -m sector-intel.src.run --fixtures --publish).",
             "years": [], "groups": ["Go8", "ATN", "IRU", "RUN", "Unaligned"],
             "institution_count": 0, "metric_count": 0, "row_count": 0},
    "metrics": [], "domains": [], "institutions": [], "rows": [],
}


@router.get("/sector/dataset")
async def sector_dataset() -> Any:
    """Serve the published Sector Intel benchmarking dataset (Phase 2 output).

    Tries the pipeline's published store first, then the copy beside the command
    centre, then a seed so the page never errors. Same seed-fallback contract as
    the other dashboard endpoints.
    """
    for candidate in _SECTOR_PATHS:
        try:
            if candidate.exists() and candidate.stat().st_size > 0:
                return json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
    return _SEED_SECTOR


class SectorBriefingRequest(BaseModel):
    institution_id: str = Field(..., min_length=1)
    year: int | None = None
    audience: str = "partner leadership"
    save: bool = True


@router.post("/sector/briefing")
async def sector_briefing(req: SectorBriefingRequest) -> dict[str, Any]:
    """Phase 4: generate a client briefing for one institution.

    Routes to the Sector Intelligence analyst for prose in Aaron's voice, given
    the benchmarked evidence pack; falls back to a computed, data-driven briefing
    so the button always returns something. Optionally saves the result as a
    Deliverables note in the vault.
    """
    from . import sector_briefing as sbrief

    dataset = sbrief.load_dataset()
    ev = sbrief.build_evidence(dataset, req.institution_id, req.year)
    if not ev:
        raise HTTPException(status_code=404, detail="No sector data for that institution; run the pipeline first.")

    markdown, source = "", "computed"
    try:
        from .wijerco_agent import call_wijerco_agent
        result = await call_wijerco_agent(
            department="research_intelligence",
            query=sbrief.agent_query(ev, req.audience),
            subagent="sector-intelligence-analyst",
        )
        answer = (result or {}).get("answer") or ""
        if answer.strip():
            markdown, source = answer.strip(), "agent"
    except Exception:  # noqa: BLE001 — agent optional; computed fallback always works
        markdown = ""
    if not markdown:
        markdown = sbrief.computed_briefing(ev, req.audience)

    inst = ev["institution"]
    saved: dict[str, Any] = {"ok": False, "reason": "not requested"}
    if req.save:
        saved = _save_sector_briefing(inst, ev["year"], markdown, source)

    return {
        "institution_id": inst["institution_id"],
        "institution_name": inst["institution_name"],
        "year": ev["year"],
        "generated": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "sample": bool(ev["meta"].get("sample")),
        "markdown": markdown,
        "evidence": ev["metrics"],
        "saved": saved,
    }


def _save_sector_briefing(inst: dict[str, Any], year: Any, markdown: str, source: str) -> dict[str, Any]:
    base = _base(); vault = _vault_root()
    if not base or not vault:
        return {"ok": False, "reason": "OBSIDIAN_VAULT_PATH not configured"}
    try:
        folder = base / "Deliverables"; folder.mkdir(parents=True, exist_ok=True)
        title = f"Sector briefing: {inst['institution_name']} ({year})"
        path = folder / f"{_slugify_title(title)}.md"
        i = 2
        while path.exists():
            path = folder / f"{_slugify_title(title)}-{i}.md"; i += 1
        fm = {
            "title": title,
            "cap": "Sector Intelligence & Evidence",
            "type": "Client briefing",
            "status": "Draft",
            "st": "st-warn",
            "meta": f"{inst.get('mission_group','')} · {inst.get('state','')} · {year}",
            "source": "Sector intel",
            "readiness": "Internal-ready",
            "briefing_source": source,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        fm = {k: v for k, v in fm.items() if v not in ("", None)}
        _write_note(path, fm, markdown)
        audit_log("sector.briefing.save", {"institution_id": inst["institution_id"],
                                           "path": path.relative_to(vault).as_posix(), "source": source})
        return {"ok": True, "path": path.relative_to(vault).as_posix()}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": str(e)}


@router.get("/schedule/list")
async def schedule_list() -> dict[str, Any]:
    return {"items": _load_items("Scheduled Runs") or SEED_SCHED}


# ---------------------------------------------------------------------------
# Knowledge base — real corpus stats from Qdrant, no hand-typed numbers.
# Sources are derived from the top-level folder of each indexed file path.
# ---------------------------------------------------------------------------

KB_QDRANT_URL   = os.getenv("QDRANT_URL", "http://localhost:6333")
KB_INDEXER_URL  = os.getenv("INDEXER_URL", "http://localhost:8005")
_KB_DATA_DIR    = Path(__file__).resolve().parent.parent / "data"
KB_RUNS_PATH    = Path(os.getenv("KB_RUNS_PATH", str(_KB_DATA_DIR / "kb_index_runs.jsonl")))
KB_MISS_LOG     = Path(os.getenv("KB_MISS_LOG", str(_KB_DATA_DIR / "kb_misses.jsonl")))
KB_QUALITY_PATH = Path(os.getenv("KB_QUALITY_PATH", str(_KB_DATA_DIR / "kb_quality.json")))
KB_FRESH_DAYS   = int(os.getenv("KB_FRESH_DAYS", "30"))
KB_STALE_DAYS   = int(os.getenv("KB_STALE_DAYS", "90"))
KB_CACHE_TTL    = int(os.getenv("KB_CACHE_TTL", "300"))

KB_COLLECTIONS = [
    (os.getenv("QDRANT_COLLECTION", "obsidian_vault"), "Obsidian vault"),
    (os.getenv("WIJERCO_COLLECTION", "wijerco_knowledge"), "WijerCo"),
    (os.getenv("UPLOADS_COLLECTION", "uploaded_docs"), "Uploaded documents"),
]

# Cache: {"ts": monotonic, "data": overview dict, "files": {collection: {file: {chunks, newest}}}}
_kb_cache: dict[str, Any] = {"ts": 0.0, "data": None, "files": {}}


async def _kb_scroll_files(client: httpx.AsyncClient, collection: str) -> dict[str, dict[str, Any]]:
    """Map file -> {chunks, newest} for one collection via payload-only scroll."""
    files: dict[str, dict[str, Any]] = {}
    offset = None
    while True:
        body: dict[str, Any] = {"limit": 512, "with_payload": ["file", "modified_at"], "with_vector": False}
        if offset is not None:
            body["offset"] = offset
        resp = await client.post(
            f"{KB_QDRANT_URL}/collections/{collection}/points/scroll", json=body, timeout=20.0)
        if resp.status_code != 200:
            return files
        data = resp.json().get("result", {}) or {}
        for pt in data.get("points", []):
            pl = pt.get("payload", {}) or {}
            f = pl.get("file") or "(unknown)"
            m = pl.get("modified_at") or ""
            rec = files.setdefault(f, {"chunks": 0, "newest": ""})
            rec["chunks"] += 1
            if m > rec["newest"]:
                rec["newest"] = m
        offset = data.get("next_page_offset")
        if offset is None:
            return files


def _kb_freshness(newest_iso: str) -> tuple[str, str, int | None]:
    """(label, status_class, age_days) from the newest file modification."""
    if not newest_iso:
        return ("Unknown", "st-mute", None)
    try:
        newest = datetime.fromisoformat(newest_iso.replace("Z", "+00:00"))
        age = (datetime.now(newest.tzinfo) - newest).days
    except Exception:
        return ("Unknown", "st-mute", None)
    if age <= KB_FRESH_DAYS:
        return ("Current", "st-good", age)
    if age <= KB_STALE_DAYS:
        return (f"Stale · {age}d", "st-warn", age)
    return (f"Gap · {age}d since update", "st-fail", age)


def _kb_last_runs() -> dict[str, dict[str, Any]]:
    """Last index-run record per collection, written by rag/indexer.py."""
    runs: dict[str, dict[str, Any]] = {}
    try:
        for line in KB_RUNS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("collection"):
                runs[rec["collection"]] = rec
    except Exception:
        pass
    return runs


def _kb_top_folder(path: str) -> str:
    norm = path.replace("\\", "/")
    return norm.split("/", 1)[0] if "/" in norm else "(root)"


def _kb_fmt_dt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%d %b %Y, %H:%M")
    except Exception:
        return iso or "—"


async def _kb_snapshot() -> dict[str, Any] | None:
    """Live corpus snapshot from Qdrant. None when Qdrant is unreachable."""
    if _kb_cache["data"] is not None and time.monotonic() - _kb_cache["ts"] < KB_CACHE_TTL:
        return _kb_cache["data"]
    runs = _kb_last_runs()
    collections: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    files_by_coll: dict[str, dict[str, dict[str, Any]]] = {}
    total_docs = total_chunks = 0
    reachable = False
    try:
        async with httpx.AsyncClient() as client:
            for coll, label in KB_COLLECTIONS:
                try:
                    resp = await client.post(
                        f"{KB_QDRANT_URL}/collections/{coll}/points/count",
                        json={"exact": True}, timeout=10.0)
                except httpx.HTTPError:
                    return None
                if resp.status_code != 200:
                    continue   # collection missing; Qdrant itself is up
                reachable = True
                count = int((resp.json().get("result", {}) or {}).get("count", 0) or 0)
                files = await _kb_scroll_files(client, coll)
                files_by_coll[coll] = files
                run = runs.get(coll, {})
                collections.append({
                    "collection": coll, "label": label,
                    "docs": len(files), "chunks": count,
                    "last_indexed": run.get("ts", ""),
                    "last_indexed_label": _kb_fmt_dt(run["ts"]) if run.get("ts") else "No run recorded",
                })
                total_docs += len(files)
                total_chunks += count
                groups: dict[str, dict[str, Any]] = {}
                for f, rec in files.items():
                    g = groups.setdefault(_kb_top_folder(f), {"docs": 0, "chunks": 0, "newest": ""})
                    g["docs"] += 1
                    g["chunks"] += rec["chunks"]
                    if rec["newest"] > g["newest"]:
                        g["newest"] = rec["newest"]
                for name, g in sorted(groups.items()):
                    fresh, st, age = _kb_freshness(g["newest"])
                    sources.append({
                        "name": name, "collection": coll, "collection_label": label,
                        "docs": g["docs"], "chunks": g["chunks"], "newest": g["newest"],
                        "fresh": fresh, "st": st, "age_days": age,
                    })
    except Exception:
        return None
    if not reachable:
        return None
    latest = max((r.get("ts", "") for r in runs.values()), default="")
    snap = {
        "stats": {"docs": total_docs, "chunks": total_chunks,
                  "updated": _kb_fmt_dt(latest) if latest else "No run recorded"},
        "collections": collections,
        "sources": sources,
        "demo": False,
    }
    _kb_cache.update(ts=time.monotonic(), data=snap, files=files_by_coll)
    return snap


@router.get("/kb/overview")
async def kb_overview() -> dict[str, Any]:
    snap = await _kb_snapshot()
    if snap:
        return snap
    # Qdrant unreachable: demo data, flagged so it is never mistaken for a live corpus.
    sources = [dict(s, docs=None, collection="demo", collection_label="Demo") for s in SEED_KB_SOURCES]
    chunks = sum(int(s.get("chunks", 0) or 0) for s in sources)
    return {"stats": {"docs": len(sources), "chunks": chunks, "updated": "Demo data"},
            "collections": [], "sources": sources, "demo": True}


@router.get("/kb/source/{name}")
async def kb_source(name: str, collection: str = "") -> dict[str, Any]:
    snap = await _kb_snapshot()
    if not snap:
        raise HTTPException(status_code=503, detail="Qdrant unreachable; no live corpus to inspect")
    out: list[dict[str, Any]] = []
    for coll, files in (_kb_cache.get("files") or {}).items():
        if collection and coll != collection:
            continue
        for f, rec in files.items():
            if _kb_top_folder(f) == name:
                fresh, st, _age = _kb_freshness(rec["newest"])
                out.append({"file": f, "collection": coll, "chunks": rec["chunks"],
                            "modified": rec["newest"], "modified_label": _kb_fmt_dt(rec["newest"]),
                            "fresh": fresh, "st": st})
    out.sort(key=lambda r: r["modified"] or "", reverse=True)
    return {"name": name, "files": out, "count": len(out)}


class KBReindexRequest(BaseModel):
    target: str = "wijerco"   # "vault" | "wijerco"


@router.post("/kb/reindex", dependencies=[Depends(require_admin)])
async def kb_reindex(req: KBReindexRequest) -> dict[str, Any]:
    if req.target not in ("vault", "wijerco"):
        raise HTTPException(status_code=400, detail="target must be 'vault' or 'wijerco'")
    path = "/index/wijerco" if req.target == "wijerco" else "/index"
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{KB_INDEXER_URL}{path}", json={}, timeout=900.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Indexer service unreachable at {KB_INDEXER_URL}: {exc}")
    _kb_cache["data"] = None   # force fresh stats on next overview
    audit_log("kb_reindex", {"target": req.target})
    return resp.json()


@router.get("/kb/quality")
async def kb_quality() -> dict[str, Any]:
    try:
        return json.loads(KB_QUALITY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"status": "never_run", "recall_at_5": None, "cases": []}


@router.post("/kb/quality/run", dependencies=[Depends(require_admin)])
async def kb_quality_run() -> dict[str, Any]:
    from harness.recall_set import RECALL_CASES
    from rag.retriever import search as rag_search
    coll = os.getenv("WIJERCO_COLLECTION", "wijerco_knowledge")
    cases: list[dict[str, Any]] = []
    hits = 0
    for case in RECALL_CASES:
        try:
            results = await rag_search(case.query, top_k=5, collection=coll)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Retrieval failed: {exc}")
        got: list[str] = []
        for r in results:
            f = r.get("file")
            if f and f not in got:
                got.append(f)
        ok = any(f in got for f in case.expected_files)
        hits += 1 if ok else 0
        cases.append({"query": case.query, "expected": case.expected_files, "pass": ok, "retrieved": got})
    out = {
        "status": "ok",
        "ts": datetime.now().astimezone().isoformat(),
        "ts_label": datetime.now().astimezone().strftime("%d %b %Y, %H:%M"),
        "collection": coll,
        "recall_at_5": round(hits / len(RECALL_CASES), 2) if RECALL_CASES else None,
        "cases_total": len(RECALL_CASES),
        "cases_passed": hits,
        "cases": cases,
    }
    try:
        KB_QUALITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        KB_QUALITY_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
    except Exception:
        pass
    audit_log("kb_quality_run", {"recall_at_5": out["recall_at_5"]})
    return out


@router.get("/kb/misses")
async def kb_misses(limit: int = 20) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    try:
        for line in KB_MISS_LOG.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                items.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    items.reverse()
    for it in items:
        it["ts_label"] = _kb_fmt_dt(it.get("ts", ""))
    return {"items": items, "count": len(items)}


@router.get("/memory/overview")
async def memory_overview() -> dict[str, Any]:
    items = _load_items("Memory")
    if not items:
        return {"groups": SEED_MEMORY_GROUPS}
    order: list[str] = []
    groups: dict[str, list[str]] = {}
    for it in items:
        g = it.get("g", "Other")
        if g not in groups:
            groups[g] = []
            order.append(g)
        if it.get("item"):
            groups[g].append(it["item"])
    return {"groups": [{"g": g, "items": groups[g]} for g in order]}


@router.get("/trace/recent")
async def trace_recent(limit: int = 20) -> dict[str, Any]:
    return {"items": (_load_items("Routing") or SEED_TRACE)[:limit]}


# ===========================================================================
# Productivity cockpit  —  GET /productivity/overview, POST /productivity/capture
# ---------------------------------------------------------------------------
# Unlike the eight pages above, the productivity page reads the *rest* of the
# vault live, not the 13_Command Centre folder. It pulls:
#   - Tasks      : every open checkbox across the vault, bucketed by the same
#                  emoji-date logic the daily/weekly dataviewjs uses.
#   - Life metrics: daily-note frontmatter (weight, protein, workout, etc.)
#                  rolled up against the 2026 OKR targets.
#   - OKRs       : the five 02_OKRs domain notes (focus, status, task progress).
#   - Projects   : 05_Projects notes (status, next step, task progress).
#   - Weekly     : the latest 03_Weekly note (focus bullets + finance snapshot).
# Quick-capture appends a task to today's daily note or writes a capture note
# to 07_Thinking. If the vault is missing, the overview returns seed data.
# ===========================================================================

DAILY_FOLDER = "04_Daily"
OKR_FOLDER = "02_OKRs"
PROJECTS_FOLDER = "05_Projects"
WEEKLY_FOLDER = "03_Weekly"
THINKING_FOLDER = "07_Thinking"

# Top-level folders not scanned for loose tasks (templates / dashboard / assets).
PROD_EXCLUDE_DIRS = {
    "00_System", "13_Command Centre", "Graphs & Visualisations",
    ".obsidian", ".trash",
}

# Obsidian Tasks-plugin grammar
_PRIORITY = {"🔺": ("Highest", 5), "⏫": ("High", 4), "🔼": ("Medium", 3),
             "🔽": ("Low", 2), "⏬": ("Lowest", 1)}
_TASK_DATE_RE = re.compile(r"([🛫⏳🗓️📅]?)\s*:?[\s(]*(\d{4}-\d{2}-\d{2})[\s)]*")
_OPEN_TASK_RE = re.compile(r"^\s*[-*]\s+\[ \]\s+(.*\S)\s*$")
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]")
_DONE_RE = re.compile(r"^\s*[-*]\s+\[(x|X)\]")
_STRIP_RE = re.compile(r"[🛫⏳🗓️📅✅➕🔁⏫🔼🔽🔺⏬]")
_DAILY_NAME_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _to_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def _task_priority(text: str) -> tuple[str, int]:
    best = ("", 0)
    for marker, (label, rank) in _PRIORITY.items():
        if marker in text and rank > best[1]:
            best = (label, rank)
    return best


def _task_dates(text: str) -> tuple[date | None, date | None]:
    due = sched = None
    for m in _TASK_DATE_RE.finditer(text):
        prefix, ds = m.group(1), m.group(2)
        d = _to_date(ds)
        if prefix in ("🛫", "⏳"):
            sched = d
        elif prefix in ("🗓️", "📅"):
            due = d
        elif due is None:
            due = d
    return due, sched


def _clean_task(text: str) -> str:
    t = _TASK_DATE_RE.sub("", text)
    t = _STRIP_RE.sub("", t)
    t = re.sub(r"#\w[\w/-]*", "", t)          # inline tags
    t = re.sub(r"\^[\w-]+$", "", t)           # block ids
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip(" -·")


def _count_tasks(text: str) -> tuple[int, int]:
    """(total, done) literal checkbox tasks in a note."""
    total = done = 0
    for line in text.splitlines():
        if _CHECKBOX_RE.match(line):
            total += 1
            if _DONE_RE.match(line):
                done += 1
    return total, done


def _collect_tasks() -> dict[str, list] | None:
    root = _vault_root()
    if not root or not root.is_dir():
        return None
    today = date.today()
    end7 = today + timedelta(days=7)
    overdue, due_today, priority, week = [], [], [], []
    for md in root.rglob("*.md"):
        rel = md.relative_to(root).parts
        if rel and rel[0] in PROD_EXCLUDE_DIRS:
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        for line in text.splitlines():
            m = _OPEN_TASK_RE.match(line)
            if not m:
                continue
            raw = m.group(1)
            if "🔁" in raw:                       # recurring — skip
                continue
            if "daily" in raw.lower():            # template scaffolding
                continue
            due, _sched = _task_dates(raw)
            plabel, prank = _task_priority(raw)
            item = {
                "text": _clean_task(raw) or raw.strip(),
                "src": md.stem,
                "due": due.isoformat() if due else None,
                "prio": plabel,
                "path": md.relative_to(root).as_posix(),
                "raw": line,
                "_rank": prank,
                "_due": due,
            }
            if due and due < today:
                overdue.append(item)
            elif due and due == today:
                due_today.append(item)
            elif prank:
                priority.append(item)
            elif due and today < due <= end7:
                week.append(item)

    def _fin(rows, key):
        rows.sort(key=key)
        for r in rows:
            r.pop("_rank", None)
            r.pop("_due", None)
        return rows[:40]

    far = date.max
    return {
        "overdue": _fin(overdue, lambda r: r["_due"] or far),
        "today":   _fin(due_today, lambda r: -r["_rank"]),
        "priority": _fin(priority, lambda r: (-r["_rank"], r["_due"] or far)),
        "week":    _fin(week, lambda r: r["_due"] or far),
    }



# ---------------------------------------------------------------------------
# Productivity extras: WIP limit, goal balance, weekly review
# These re-express the dashboard usability features against the live vault,
# using the same Obsidian Tasks-plugin grammar as _collect_tasks.
# ---------------------------------------------------------------------------

WIP_LIMIT = int(os.getenv("PROD_WIP_LIMIT", "3"))           # cap on open high-priority tasks
GOAL_TAGS = [("OES", "#1f4d3f"), ("Product", "#c28f1e"), ("Profile", "#3f6f8f")]
_DONE_DATE_RE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")  # Tasks-plugin done date


def _task_goal(text: str) -> str | None:
    low = text.lower()
    for tag, _c in GOAL_TAGS:
        if re.search(r"(?:^|\s)#" + re.escape(tag.lower()) + r"\b", low):
            return tag
    return None


def _prod_extras() -> dict[str, Any] | None:
    """WIP count, company-goal balance, and the weekly-review buckets.

    Single pass over the vault, same exclusions as _collect_tasks.
    WIP   : open tasks at High or Highest priority.
    goals : open-task counts per company goal (#OES / #Product / #Profile).
    review: closed this week (done date in last 7 days), waiting too long
            (started or scheduled in the past, or #waiting), and needs
            attention (overdue, or high priority with no due date).
    """
    root = _vault_root()
    if not root or not root.is_dir():
        return None
    today = date.today()
    wk_ago = today - timedelta(days=7)
    wip = 0
    goal_counts = {g: 0 for g, _ in GOAL_TAGS}
    untagged = 0
    closed: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    for md in root.rglob("*.md"):
        rel = md.relative_to(root).parts
        if rel and rel[0] in PROD_EXCLUDE_DIRS:
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        for line in text.splitlines():
            if _DONE_RE.match(line):                       # completed task
                dm = _DONE_DATE_RE.search(line)
                if dm:
                    dd = _to_date(dm.group(1))
                    if dd and wk_ago <= dd <= today:
                        closed.append({"text": _clean_task(line) or line.strip(),
                                       "src": md.stem, "done": dd.isoformat(),
                                       "path": md.relative_to(root).as_posix(), "raw": line, "_d": dd})
                continue
            m = _OPEN_TASK_RE.match(line)
            if not m:
                continue
            raw = m.group(1)
            if "\U0001F501" in raw or "daily" in raw.lower():   # recurring / template
                continue
            due, sched = _task_dates(raw)
            plabel, prank = _task_priority(raw)
            g = _task_goal(raw)
            if g:
                goal_counts[g] += 1
            else:
                untagged += 1
            if prank >= 4:                                  # High / Highest -> WIP
                wip += 1
            text_clean = _clean_task(raw) or raw.strip()
            if (sched and sched < today) or re.search(r"(?:^|\s)#waiting\b", raw.lower()):
                age = (today - sched).days if sched else None
                waiting.append({"text": text_clean, "src": md.stem,
                                "sub": (f"started {age}d ago" if age is not None else "waiting"),
                                "path": md.relative_to(root).as_posix(), "raw": line,
                                "_age": age if age is not None else 0})
            if due and due < today:
                attention.append({"text": text_clean, "src": md.stem,
                                  "sub": f"overdue {(today - due).days}d",
                                  "path": md.relative_to(root).as_posix(), "raw": line, "_w": (today - due).days})
            elif prank >= 4 and not due:
                attention.append({"text": text_clean, "src": md.stem,
                                  "sub": f"{plabel} priority, no date",
                                  "path": md.relative_to(root).as_posix(), "raw": line, "_w": 0})

    closed.sort(key=lambda r: r.get("_d") or date.min, reverse=True)
    waiting.sort(key=lambda r: r.get("_age", 0), reverse=True)
    attention.sort(key=lambda r: r.get("_w", 0), reverse=True)
    for r in closed:    r.pop("_d", None)
    for r in waiting:   r.pop("_age", None)
    for r in attention: r.pop("_w", None)

    goals = [{"tag": g, "count": goal_counts[g], "color": c} for g, c in GOAL_TAGS]
    return {
        "wip": {"count": wip, "limit": WIP_LIMIT, "over": wip > WIP_LIMIT},
        "goals": {"items": goals, "untagged": untagged, "total": sum(goal_counts.values())},
        "review": {"closed": closed[:30], "waiting": waiting[:30], "attention": attention[:30]},
    }



def _collect_productivity():
    """One pass over the vault -> buckets, inbox, WIP, goals, review, counts.

    Replaces the old two-scan path. Buckets fold in start (start) and scheduled
    dates, not only due. WIP is what you have actually started (a start date on or
    before today, or a #doing tag), kept separate from the count of open
    high-priority tasks. Goals are weighted by priority. Review tracks closed this
    week against the prior week for momentum.
    """
    root = _vault_root()
    if not root or not root.is_dir():
        return None
    today = date.today()
    end7 = today + timedelta(days=7)
    wk_ago = today - timedelta(days=7)
    wk_ago2 = today - timedelta(days=14)
    overdue, due_today, priority, week, inbox = [], [], [], [], []
    wip_started = 0
    wip_flagged = 0
    goal_counts = {g: 0 for g, _ in GOAL_TAGS}
    goal_weight = {g: 0 for g, _ in GOAL_TAGS}
    untagged = 0
    closed, waiting, attention = [], [], []
    closed_prev = 0
    total_open = 0
    for md in root.rglob("*.md"):
        rel = md.relative_to(root).parts
        if rel and rel[0] in PROD_EXCLUDE_DIRS:
            continue
        try:
            text = md.read_text(encoding="utf-8")
        except Exception:
            continue
        relpath = md.relative_to(root).as_posix()
        for line in text.splitlines():
            if _DONE_RE.match(line):
                dm = _DONE_DATE_RE.search(line)
                if dm:
                    cd = _to_date(dm.group(1))
                    if cd and wk_ago <= cd <= today:
                        closed.append({"text": _clean_task(line) or line.strip(), "src": md.stem,
                                       "done": cd.isoformat(), "path": relpath, "raw": line, "_d": cd})
                    elif cd and wk_ago2 <= cd < wk_ago:
                        closed_prev += 1
                continue
            m = _OPEN_TASK_RE.match(line)
            if not m:
                continue
            raw = m.group(1)
            if "\U0001F501" in raw or "daily" in raw.lower():
                continue
            total_open += 1
            due, sched = _task_dates(raw)
            plabel, prank = _task_priority(raw)
            g = _task_goal(raw)
            started = bool((sched and sched <= today) or re.search(r"(?:^|\s)#doing\b", raw.lower()))
            if g:
                goal_counts[g] += 1
                goal_weight[g] += (prank or 1)
            else:
                untagged += 1
            if prank >= 4:
                wip_flagged += 1
            if started:
                wip_started += 1
            base = {"text": _clean_task(raw) or raw.strip(), "src": md.stem,
                    "due": due.isoformat() if due else None, "prio": plabel,
                    "path": relpath, "raw": line}
            eff = due or sched
            if due and due < today:
                overdue.append({**base, "_k": due})
            elif eff and eff == today:
                due_today.append({**base, "_k": -prank})
            elif prank:
                priority.append({**base, "_k": (-prank, due or date.max)})
            elif eff and today < eff <= end7:
                week.append({**base, "_k": eff})
            elif not due and not sched and not prank:
                inbox.append(dict(base))
            if (sched and sched < today) or re.search(r"(?:^|\s)#waiting\b", raw.lower()):
                age = (today - sched).days if sched else None
                waiting.append({**base, "sub": (f"started {age}d ago" if age is not None else "waiting"),
                                "_age": age if age is not None else 0})
            if due and due < today:
                attention.append({**base, "sub": f"overdue {(today - due).days}d", "_w": (today - due).days})
            elif prank >= 4 and not due:
                attention.append({**base, "sub": f"{plabel} priority, no date", "_w": 0})

    def _fin(rows, key, n=40):
        rows.sort(key=key)
        for r in rows:
            for kk in ("_k", "_age", "_w", "_d"):
                r.pop(kk, None)
        return rows[:n]

    tasks = {
        "overdue": _fin(overdue, lambda r: r["_k"]),
        "today": _fin(due_today, lambda r: r["_k"]),
        "priority": _fin(priority, lambda r: r["_k"]),
        "week": _fin(week, lambda r: r["_k"]),
    }
    closed.sort(key=lambda r: r.get("_d") or date.min, reverse=True)
    waiting.sort(key=lambda r: r.get("_age", 0), reverse=True)
    attention.sort(key=lambda r: r.get("_w", 0), reverse=True)
    for r in closed:
        r.pop("_d", None)
    for r in waiting:
        r.pop("_age", None)
    for r in attention:
        r.pop("_w", None)
    goals = [{"tag": g, "count": goal_counts[g], "weight": goal_weight[g], "color": c} for g, c in GOAL_TAGS]
    surfaced = sum(len(v) for v in tasks.values())
    return {
        "tasks": tasks,
        "inbox": inbox[:50],
        "counts": {"open": total_open, "surfaced": surfaced, "inbox": len(inbox)},
        "wip": {"count": wip_started, "limit": WIP_LIMIT, "over": wip_started > WIP_LIMIT, "flagged": wip_flagged},
        "goals": {"items": goals, "untagged": untagged, "total": sum(goal_counts.values())},
        "review": {"closed": closed[:30], "waiting": waiting[:30], "attention": attention[:30],
                   "momentum": {"this": len(closed), "prev": closed_prev}},
        "vault": root.name,
    }


_CAP_PRIO_NUM = {"1": "\U0001F53A", "2": "⏫", "3": "\U0001F53C", "4": "\U0001F53D", "5": "⏬"}
_CAP_PRIO_NAME = {"highest": "\U0001F53A", "high": "⏫", "medium": "\U0001F53C", "low": "\U0001F53D", "lowest": "⏬"}
_WEEKDAYS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2, "wednesday": 2,
             "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4, "friday": 4,
             "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}


def _next_weekday(wd: int) -> date:
    today = date.today()
    ahead = (wd - today.weekday()) % 7
    if ahead == 0:
        ahead = 7
    return today + timedelta(days=ahead)


def _parse_capture(text: str) -> str:
    """Turn inline modifiers into Tasks-plugin grammar.

    !1..!5 or !high etc -> priority emoji; today / tomorrow / <weekday> / +Nd /
    YYYY-MM-DD -> a due date; #tags pass through untouched.
    """
    prio = None
    due = None
    keep = []
    for w in text.split():
        lw = w.lower()
        mp = re.fullmatch(r"!(p?[1-5]|highest|high|medium|low|lowest)", lw)
        if mp:
            tok = mp.group(1).lstrip("p")
            prio = _CAP_PRIO_NUM.get(tok) or _CAP_PRIO_NAME.get(tok)
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", w):
            due = w
            continue
        if lw in ("today", "tod"):
            due = date.today().isoformat()
            continue
        if lw in ("tomorrow", "tmr", "tom"):
            due = (date.today() + timedelta(days=1)).isoformat()
            continue
        md2 = re.fullmatch(r"\+(\d+)d", lw)
        if md2:
            due = (date.today() + timedelta(days=int(md2.group(1)))).isoformat()
            continue
        if lw in _WEEKDAYS:
            due = _next_weekday(_WEEKDAYS[lw]).isoformat()
            continue
        keep.append(w)
    clean = " ".join(keep).strip()
    suffix = ""
    if due:
        suffix += f" \U0001F4C5 {due}"
    if prio:
        suffix += f" {prio}"
    return (clean + suffix).strip()



PRIORITY_EMOJI = {"Highest": "🔺", "High": "⏫", "Medium": "🔼", "Low": "🔽", "Lowest": "⏬"}


def _rewrite_task_line(raw: str, op: str, value: str) -> str | None:
    """Apply one edit to a single Obsidian task line, preserving its grammar."""
    m = re.match(r"^(\s*[-*]\s+\[)([ xX])(\]\s?)(.*)$", raw)
    if not m:
        return None
    pre, box, _mid, body = m.group(1), m.group(2), m.group(3), m.group(4)
    today = date.today().isoformat()

    def collapse(b: str) -> str:
        return re.sub(r"\s{2,}", " ", b).strip()

    if op == "toggle_done":
        if box == " ":
            box = "x"
            if not _DONE_DATE_RE.search(body):
                body = collapse(body) + f" ✅ {today}"
        else:
            box = " "
            body = collapse(_DONE_DATE_RE.sub("", body).replace("✅", ""))
    elif op == "set_priority":
        for em in _PRIORITY:
            body = body.replace(em, "")
        body = collapse(body)
        if value in PRIORITY_EMOJI:
            body = collapse(body + " " + PRIORITY_EMOJI[value])
    elif op == "set_due":
        body = collapse(re.sub(r"\s*[🗓️📅]\s*\d{4}-\d{2}-\d{2}", "", body))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
            body = collapse(body + f" 📅 {value}")
    elif op == "toggle_tag":
        if not re.fullmatch(r"[\w/-]+", value or ""):
            return None
        tag = "#" + value
        pat = r"(?<!\S)" + re.escape(tag) + r"(?![\w/-])"
        if re.search(pat, body, re.I):
            body = collapse(re.sub(pat, "", body, flags=re.I))
        else:
            body = collapse(body + " " + tag)
    elif op == "set_text":
        tokens: list[str] = []
        for mm in re.finditer(r"[🛫⏳🗓️📅✅]\s*\d{4}-\d{2}-\d{2}", body):
            tokens.append(re.sub(r"\s+", " ", mm.group(0)))
        for em in _PRIORITY:
            if em in body:
                tokens.append(em)
        tokens += re.findall(r"(?<!\S)#[\w/-]+", body)
        bid = re.search(r"\^[\w-]+$", body.strip())
        newbody = (value or "").strip()
        if tokens:
            newbody += " " + " ".join(tokens)
        if bid:
            newbody += " " + bid.group(0)
        body = collapse(newbody)
    else:
        return None

    return f"{pre}{box}] {body}".rstrip()


# ---- Life metrics from daily notes ----------------------------------------

def _daily_frontmatter(text: str) -> dict[str, Any]:
    """Frontmatter reader that also grabs the first item of a YAML list value."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, Any] = {}
    cur: str | None = None
    for line in text[3:end].splitlines():
        if not line.strip():
            continue
        if re.match(r"^\s*-\s", line) and cur is not None:
            if cur not in out:
                out[cur] = line.strip()[1:].strip().strip('"')
            continue
        if ":" in line and not line.lstrip().startswith("#"):
            k, v = line.split(":", 1)
            k, v = k.strip(), v.strip().strip('"')
            if v:
                out[k] = v
                cur = None
            else:
                cur = k
    return out


def _num(v: Any) -> float | None:
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except Exception:
        return None


def _daily_records() -> list[dict[str, Any]]:
    root = _vault_root()
    recs: list[dict[str, Any]] = []
    if not root:
        return recs
    base = root / DAILY_FOLDER
    if not base.is_dir():
        return recs
    for md in base.rglob("*.md"):
        m = _DAILY_NAME_RE.search(md.stem)
        if not m:
            continue
        d = _to_date(m.group(1))
        if not d:
            continue
        try:
            fm = _daily_frontmatter(md.read_text(encoding="utf-8"))
        except Exception:
            continue
        fm["_date"] = d
        recs.append(fm)
    recs.sort(key=lambda r: r["_date"])
    return recs


# ── Daily-note schedule ──────────────────────────────────────────────────────
# Reads the "## Schedule" section of the most recent dated note (<= today) and
# turns its timed bullets into [{time, title, meta, now}]. Tolerant of bullets,
# checkboxes, bold, and time ranges. The current block is flagged only when the
# chosen note is today's.
_SCHED_LINE = re.compile(
    r"^\s*(\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?"          # start time
    r"(?:\s*[-–—]\s*\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?)?)"  # optional end time
    r"\s*(.*)$",
    re.I,
)
_SCHED_FIRST_TIME = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", re.I)
_SCHED_24H = re.compile(r"(\d{1,2}):(\d{2})")


def _sched_minutes(timestr: str) -> int | None:
    """Minutes-since-midnight for the first time token in a schedule line."""
    m = _SCHED_FIRST_TIME.search(timestr)
    if m:
        h = int(m.group(1)) % 12
        if m.group(3).lower() == "p":
            h += 12
        return h * 60 + int(m.group(2) or 0)
    m = _SCHED_24H.search(timestr)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h * 60 + mi
    m = re.match(r"^\s*(\d{1,2})\s*$", timestr)        # bare hour, e.g. "9"
    if m:
        return int(m.group(1)) * 60
    return None


def _daily_schedule() -> list[dict[str, Any]]:
    root = _vault_root()
    if not root:
        return []
    base = root / DAILY_FOLDER
    if not base.is_dir():
        return []
    today = date.today()
    chosen: tuple[date, Path] | None = None
    for md in base.rglob("*.md"):
        m = _DAILY_NAME_RE.search(md.stem)
        if not m:
            continue
        d = _to_date(m.group(1))
        if not d or d > today:                 # ignore future-dated notes
            continue
        if chosen is None or d > chosen[0]:
            chosen = (d, md)
    if chosen is None:
        return []
    note_date, note_path = chosen
    try:
        text = note_path.read_text(encoding="utf-8")
    except Exception:
        return []

    # Collect the body of the first "Schedule" heading until the next heading.
    body: list[str] = []
    in_section = False
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            head = line.lstrip("#").strip().lower()
            if not in_section and head.startswith("schedule"):
                in_section = True
                continue
            if in_section:                     # next heading ends the section
                break
        elif in_section:
            body.append(line)

    items: list[dict[str, Any]] = []
    for raw in body:
        line = raw.strip()
        if not line:
            continue
        # strip list markers, checkboxes, and bold/code wrappers
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\[[ xX]\]\s+", "", line)
        line = line.replace("**", "").replace("`", "").strip()
        m = _SCHED_LINE.match(line)
        if not m:
            continue
        timestr = re.sub(r"\s+", " ", m.group(1)).strip()
        rest = m.group(2).strip().lstrip(":-–—·| ").strip()
        if not rest:
            continue
        title, meta = rest, ""
        for sep in (" — ", " – ", " · ", " | "):
            if sep in rest:
                title, meta = rest.split(sep, 1)
                break
        else:
            paren = re.search(r"\s*\(([^)]+)\)\s*$", rest)
            if paren:
                title, meta = rest[: paren.start()].strip(), paren.group(1).strip()
            elif " - " in rest:
                title, meta = rest.split(" - ", 1)
        items.append({
            "time": timestr,
            "title": title.strip(),
            "meta": meta.strip(),
            "_min": _sched_minutes(timestr),
            "now": False,
        })

    # Flag the current/most-recent block, only for today's note.
    if note_date == today:
        now_min = datetime.now().hour * 60 + datetime.now().minute
        cur = None
        for i, it in enumerate(items):
            if it["_min"] is not None and it["_min"] <= now_min:
                cur = i
        if cur is not None:
            items[cur]["now"] = True
    for it in items:
        it.pop("_min", None)
    return items


# ── Time tracked ─────────────────────────────────────────────────────────────
# Aggregates deep-work and meeting hours from each daily note's frontmatter for
# Monday-Friday of the current week. Field names default to deep_work / meetings
# (hours) and can be overridden with PROD_DEEP_FIELD / PROD_MEET_FIELD.
_DEEP_FIELDS = ([os.getenv("PROD_DEEP_FIELD")] if os.getenv("PROD_DEEP_FIELD") else []) + \
    ["deep_work", "deepwork", "deep_work_hours", "deep_hours", "deep"]
_MEET_FIELDS = ([os.getenv("PROD_MEET_FIELD")] if os.getenv("PROD_MEET_FIELD") else []) + \
    ["meetings", "meeting_hours", "meetings_hours", "meeting", "meets"]


def _field_hours(rec: dict[str, Any], fields: list[str]) -> float:
    for f in fields:
        v = _num(rec.get(f))
        if v is not None:
            return v
    return 0.0


def _time_tracked() -> dict[str, Any]:
    recs = _daily_records()
    by_date = {r["_date"]: r for r in recs}
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    labels = ["M", "T", "W", "T", "F"]
    days: list[dict[str, Any]] = []
    deep_total = meet_total = 0.0
    for i in range(5):
        rec = by_date.get(monday + timedelta(days=i), {})
        deep = _field_hours(rec, _DEEP_FIELDS)
        meet = _field_hours(rec, _MEET_FIELDS)
        deep_total += deep
        meet_total += meet
        days.append({"d": labels[i], "deep": round(deep, 1), "meet": round(meet, 1)})
    return {
        "days": days,
        "deepHours": round(deep_total, 1),
        "meetHours": round(meet_total, 1),
        "weekHours": round(deep_total + meet_total, 1),
    }


# ── Reading list ─────────────────────────────────────────────────────────────
# Parses the "## Reading" section of a reading note into [{title, source, pct}].
# Location: PROD_READING_PATH (vault-relative) if set, else 13_Command Centre/
# Reading.md, else the first Reading.md found anywhere in the vault. Each line:
#   - Title | Source | 64%      (source and progress optional; [x] = done/100%)
_READING_PCT = re.compile(r"(\d{1,3})\s*%")


def _reading_note_path(root: Path) -> Path | None:
    env = os.getenv("PROD_READING_PATH")
    if env:
        p = root / env
        return p if p.is_file() else None
    default = root / CC_FOLDER / "Reading.md"
    if default.is_file():
        return default
    # tolerant fallback: first Reading*.md anywhere (covers stray .md.md, etc.)
    for md in sorted(root.rglob("Reading*.md")):
        return md
    return None


def _reading() -> list[dict[str, Any]]:
    root = _vault_root()
    if not root:
        return []
    path = _reading_note_path(root)
    if not path:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return []

    body: list[str] = []
    found = False
    in_section = False
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            head = line.lstrip("#").strip().lower()
            if head.startswith("reading"):     # (re)start at any Reading heading; last wins
                found = True
                in_section = True
                body = []
                continue
            in_section = False                 # any other heading ends the section
            continue
        if in_section:
            body.append(line)
    # If there is no Reading heading at all, fall back to the whole file.
    if not found:
        body = text.splitlines()

    items: list[dict[str, Any]] = []
    for raw in body:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        done = bool(re.match(r"^[-*+]\s+\[[xX]\]", line))
        line = re.sub(r"^[-*+]\s+", "", line)
        line = re.sub(r"^\[[ xX]\]\s+", "", line)
        line = line.replace("**", "").replace("`", "").strip()
        if not line:
            continue
        pct: int | None = 100 if done else None
        mpct = _READING_PCT.search(line)
        if mpct:
            pct = max(0, min(100, int(mpct.group(1))))
            line = (line[: mpct.start()] + line[mpct.end():]).strip()
        # split into title | source on the first recognised delimiter
        title, source = line, ""
        for sep in ("|", " — ", " – ", " :: ", " - "):
            if sep in line:
                parts = [p.strip() for p in line.split(sep)]
                title = parts[0]
                source = next((p for p in parts[1:] if p), "")
                break
        title = title.strip(" |:-–—").strip()
        source = source.strip(" |:-–—").strip()
        if not title:
            continue
        items.append({"title": title, "source": source, "pct": pct})
    return items


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "yes", "1", "✅")


def _metric_cards(recs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def last(field: str) -> float | None:
        for r in reversed(recs):
            v = _num(r.get(field))
            if v is not None:
                return v
        return None

    def spark(field: str, n: int = 14) -> list[float]:
        vals = [_num(r.get(field)) for r in recs if _num(r.get(field)) is not None]
        return vals[-n:]

    last7 = recs[-7:]

    def count7(field: str) -> int:
        return sum(1 for r in last7 if _truthy(r.get(field)))

    def avg7(field: str) -> float | None:
        vals = [_num(r.get(field)) for r in last7 if _num(r.get(field)) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    def sum_field(field: str, rows: list[dict]) -> float:
        return round(sum(_num(r.get(field)) or 0 for r in rows), 1)

    def band(v, good, warn, higher_good=True):
        if v is None:
            return "st-mute"
        if higher_good:
            return "st-good" if v >= good else "st-warn" if v >= warn else "st-fail"
        return "st-good" if v <= good else "st-warn" if v <= warn else "st-fail"

    weight = last("weight")
    protein = last("Protein_grams")
    sleep = avg7("sleep_hours")
    energy = last("Energy_score")
    steps = avg7("Steps")
    workouts = count7("workout_done")
    alcfree = count7("Alcohol_free")
    strat = sum_field("strategy_hours", last7)
    rev = sum_field("consultancy_revenue", last7)

    cards = [
        {"key": "weight", "label": "Weight", "value": weight, "unit": "kg",
         "target": "Target 85kg", "st": band(weight, 87, 92, higher_good=False),
         "spark": spark("weight")},
        {"key": "protein", "label": "Protein (latest)", "value": protein, "unit": "g",
         "target": "Target 180g", "st": band(protein, 180, 140),
         "spark": spark("Protein_grams")},
        {"key": "workout", "label": "Workouts", "value": workouts, "unit": "/7d",
         "target": "Target 6/wk", "st": band(workouts, 6, 4), "spark": []},
        {"key": "alcfree", "label": "Alcohol-free", "value": alcfree, "unit": "/7d",
         "target": "Target 4/wk", "st": band(alcfree, 4, 2), "spark": []},
        {"key": "sleep", "label": "Sleep (avg)", "value": sleep, "unit": "h",
         "target": "Target 7.5h", "st": band(sleep, 7.5, 6.5), "spark": spark("sleep_hours")},
        {"key": "energy", "label": "Energy (latest)", "value": energy, "unit": "/10",
         "target": "Daily check-in", "st": band(energy, 7, 4), "spark": spark("Energy_score")},
        {"key": "strategy", "label": "Strategy hours", "value": strat or None, "unit": "h/7d",
         "target": "Strategy > ops", "st": "st-good" if strat else "st-mute", "spark": []},
        {"key": "revenue", "label": "Consultancy", "value": rev or None, "unit": "$/7d",
         "target": "Passive-income goal", "st": "st-good" if rev else "st-mute", "spark": []},
    ]
    return cards


# ---- OKRs, projects, weekly -----------------------------------------------

_STATUS_CLASS = {
    "not started": "st-mute", "planning": "st-mute", "scheduled": "st-mute",
    "in progress": "st-warn", "in delivery": "st-warn", "active": "st-warn",
    "stablising": "st-warn", "stabilising": "st-warn", "establishing the foundation": "st-warn",
    "done": "st-good", "complete": "st-good", "completed": "st-good", "reviewed": "st-good",
    "blocked": "st-fail", "at risk": "st-fail",
}


def _status_class(label: str) -> str:
    return _STATUS_CLASS.get((label or "").strip().lower(), "st-mute")


def _okr_domains() -> list[dict[str, Any]] | None:
    root = _vault_root()
    if not root:
        return None
    d = root / OKR_FOLDER
    if not d.is_dir():
        return None
    out = []
    for p in sorted(d.glob("0*_*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = _parse_frontmatter(text)
        total, done = _count_tasks(text)
        out.append({
            "domain": re.sub(r"^\d+_", "", p.stem).replace("_", " "),
            "focus": fm.get("current_focus", "") or "",
            "status": fm.get("Goal_Status", "") or "",
            "st": _status_class(fm.get("Goal_Status", "")),
            "pct": round(done / total * 100) if total else 0,
            "done": done, "total": total,
        })
    return out or None


def _projects() -> list[dict[str, Any]] | None:
    root = _vault_root()
    if not root:
        return None
    d = root / PROJECTS_FOLDER
    if not d.is_dir():
        return None
    out = []
    for p in sorted(d.glob("*.md")):
        if "template" in p.stem.lower():
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        fm = _parse_frontmatter(text)
        total, done = _count_tasks(text)
        status = fm.get("Status", "") or ""
        nxt = fm.get("next-step", "") or fm.get("current_focus", "") or ""
        if isinstance(nxt, str) and nxt.startswith("{{"):
            nxt = ""
        out.append({
            "title": p.stem,
            "status": status or "—",
            "st": _status_class(status),
            "next": nxt,
            "pct": round(done / total * 100) if total else 0,
        })
    return out or None


def _weekly() -> dict[str, Any] | None:
    root = _vault_root()
    if not root:
        return None
    d = root / WEEKLY_FOLDER
    if not d.is_dir():
        return None
    files = [f for f in d.glob("*.md") if not f.stem.lower().startswith("untitled")]
    if not files:
        return None
    latest = max(files, key=lambda f: f.stat().st_mtime)
    try:
        text = latest.read_text(encoding="utf-8")
    except Exception:
        return None
    fm = _parse_frontmatter(text)
    # Pull the "focus this week" bullets if present.
    focus: list[str] = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "focus this week" in line.lower():
            for nxt in lines[i + 1:]:
                s = nxt.strip()
                if s.startswith("- "):
                    item = s[2:].strip()
                    if item and item.lower() not in ("none currently", "none"):
                        focus.append(item)
                elif s.startswith("**") or s.startswith("#"):
                    break
            break
    return {
        "title": latest.stem,
        "focus": focus[:6],
        "revenue": fm.get("weekly_consultancy_revenue"),
        "passive": fm.get("weekly_passive_income"),
        "leads": fm.get("new_leads_qualified"),
        "energy": fm.get("Energy"),
    }


SEED_PRODUCTIVITY = {
    "tasks": {
        "overdue": [{"text": "Send Monash follow-up", "src": "2026-06-11", "due": "2026-06-13", "prio": "High"}],
        "today": [{"text": "Draft weekly scorecard", "src": "Week 23 - 2026", "due": None, "prio": "Medium"}],
        "priority": [{"text": "Submit Masters of Education application", "src": "05_Growth", "due": None, "prio": "Highest"}],
        "week": [{"text": "1-on-1 prep for staff reviews", "src": "Staff", "due": "2026-06-18", "prio": ""}],
    },
    "metrics": [
        {"key": "weight", "label": "Weight", "value": 98, "unit": "kg", "target": "Target 85kg", "st": "st-warn", "spark": [101, 100, 99, 98]},
        {"key": "protein", "label": "Protein (latest)", "value": 120, "unit": "g", "target": "Target 180g", "st": "st-mute", "spark": []},
        {"key": "workout", "label": "Workouts", "value": 3, "unit": "/7d", "target": "Target 6/wk", "st": "st-warn", "spark": []},
        {"key": "alcfree", "label": "Alcohol-free", "value": 4, "unit": "/7d", "target": "Target 4/wk", "st": "st-good", "spark": []},
    ],
    "okrs": [
        {"domain": "Professional", "focus": "Establishing the Foundation", "status": "Stablising", "st": "st-warn", "pct": 20, "done": 1, "total": 5},
        {"domain": "Physical", "focus": "Reach 85kg", "status": "Active", "st": "st-warn", "pct": 30, "done": 3, "total": 10},
    ],
    "projects": [
        {"title": "OLA 2.0", "status": "in progress", "st": "st-warn", "next": "Scope the redesign", "pct": 40},
        {"title": "MBA ReDesign", "status": "not started", "st": "st-mute", "next": "", "pct": 0},
    ],
    "weekly": {"title": "Week 23 - 2026", "focus": ["Hold the 5pm hard stop 4/5 days"], "revenue": None, "passive": None, "leads": None, "energy": None},
    "inbox": [{"text": "Idea: andragogy explainer thread", "src": "07_Thinking", "due": None, "prio": ""}],
    "counts": {"open": 18, "surfaced": 6, "inbox": 3},
    "wip": {"count": 4, "limit": 3, "over": True, "flagged": 5},
    "goals": {"items": [{"tag": "OES", "count": 5, "weight": 14, "color": "#1f4d3f"}, {"tag": "Product", "count": 3, "weight": 7, "color": "#c28f1e"}, {"tag": "Profile", "count": 2, "weight": 6, "color": "#3f6f8f"}], "untagged": 4, "total": 10},
    "review": {
        "closed": [{"text": "Ship triage agent v1 to OES", "src": "2026-06-23", "done": "2026-06-23"}],
        "waiting": [{"text": "Platform team API spec", "src": "05_Projects", "sub": "started 9d ago"}],
        "attention": [{"text": "Review assessment-turnaround pilot data", "src": "2026-06-12", "sub": "overdue 5d"}],
        "momentum": {"this": 1, "prev": 3},
    },
    "vault": "Vault",
    "schedule": [
        {"time": "8:00", "title": "Writing block · LinkedIn", "meta": "Deep work · 60 min", "now": False},
        {"time": "9:30", "title": "OES product standup", "meta": "15 min · Delivery", "now": False},
        {"time": "11:00", "title": "Partner review · Swinburne Online", "meta": "45 min", "now": True},
        {"time": "1:00", "title": "1:1 · Course coordinator", "meta": "Capability · 30 min", "now": False},
        {"time": "3:30", "title": "Academic board prep", "meta": "Decision paper · 60 min", "now": False},
    ],
    "timeTracked": {
        "days": [
            {"d": "M", "deep": 3, "meet": 4}, {"d": "T", "deep": 5, "meet": 3},
            {"d": "W", "deep": 2, "meet": 5}, {"d": "T", "deep": 6, "meet": 2},
            {"d": "F", "deep": 1, "meet": 2},
        ],
        "deepHours": 17, "meetHours": 16, "weekHours": 33,
    },
    "reading": [
        {"title": "Leadership Without Easy Answers", "source": "Heifetz", "pct": 64},
        {"title": "The teaching-research nexus, revisited", "source": "Article", "pct": 20},
        {"title": "TEQSA 2026 consultation paper", "source": "Memo", "pct": 0},
        {"title": "Andragogy in practice", "source": "Up next", "pct": None},
    ],
}


@router.get("/productivity/overview")
async def productivity_overview() -> dict[str, Any]:
    prod = _collect_productivity()
    if prod is None:                        # vault unreachable -> seed
        return SEED_PRODUCTIVITY
    recs = _daily_records()
    return {
        **prod,
        "metrics": _metric_cards(recs),
        "okrs": _okr_domains() or [],
        "projects": _projects() or [],
        "weekly": _weekly() or {},
        "schedule": _daily_schedule(),
        "timeTracked": _time_tracked(),
        "reading": _reading(),
    }


class CaptureIn(BaseModel):
    text: str
    type: str = "task"   # 'task' | 'capture'
    dry_run: bool = False


@router.post("/productivity/capture", dependencies=[Depends(require_admin)])
async def productivity_capture(body: CaptureIn) -> dict[str, Any]:
    root = _vault_root()
    if not root or not root.is_dir():
        return {"ok": False, "error": "Vault not found"}
    text = (body.text or "").strip()
    if not text:
        return {"ok": False, "error": "Nothing to capture"}

    if body.type == "capture":
        folder = root / THINKING_FOLDER
        slug = re.sub(r"[^\w \-]", "", text)[:40].strip().replace(" ", "-") or "capture"
        today = date.today().isoformat()
        fn = folder / f"{today}-{slug}.md"
        i = 1
        while fn.exists():
            fn = folder / f"{today}-{slug}-{i}.md"
            i += 1
        if body.dry_run:
            return {"ok": True, "dry_run": True, "mode": "capture",
                    "file": fn.name, "preview": f"# {text[:60]}\n\n{text}"}
        folder.mkdir(parents=True, exist_ok=True)
        fn.write_text(
            f"---\nType: Capture\nDate: {today}\nOwner: Aaron Wijeratne\n"
            f"Status: inbox\nSource: Command Centre\n---\n# {text[:60]}\n\n"
            f"## RAW THOUGHT\n{text}\n\n## WHY THIS MIGHT MATTER\n\n## NEXT STEP\n\n",
            encoding="utf-8",
        )
        audit_log("vault.capture.create",
                  {"path": fn.relative_to(root).as_posix(), "chars": len(text)})
        return {"ok": True, "mode": "capture", "file": fn.name}

    # default: append a task to today's daily note
    today = date.today()
    month_dir = root / DAILY_FOLDER / str(today.year) / today.strftime("%m-%B")
    note = month_dir / f"{today.strftime('%Y-%m-%d-%A')}.md"
    line = f"- [ ] {_parse_capture(text)}"
    heading = "## ⚡ Quick Capture"
    if body.dry_run:
        return {"ok": True, "dry_run": True, "mode": "task",
                "file": note.name, "preview": line}
    if note.exists():
        backup = backup_file(note)            # snapshot before mutating
        content = note.read_text(encoding="utf-8")
        if heading in content:
            content = content.replace(heading, f"{heading}\n{line}", 1)
        else:
            content = content.rstrip() + f"\n\n{heading}\n{line}\n"
        note.write_text(content, encoding="utf-8")
        audit_log("vault.task.append",
                  {"path": note.relative_to(root).as_posix(), "line": line, "backup": backup})
    else:
        month_dir.mkdir(parents=True, exist_ok=True)
        note.write_text(
            f"---\ntags:\n  - \"#daily\"\nCreated: {datetime.now().isoformat(timespec='seconds')}\n---\n"
            f"# {note.stem}\n\n{heading}\n{line}\n",
            encoding="utf-8",
        )
        audit_log("vault.task.append",
                  {"path": note.relative_to(root).as_posix(), "line": line, "backup": None})
    return {"ok": True, "mode": "task", "file": note.name}


class TaskOpIn(BaseModel):
    path: str
    raw: str
    op: str
    value: str = ""
    dry_run: bool = False


@router.post("/productivity/task/update", dependencies=[Depends(require_admin)])
async def task_update(body: TaskOpIn) -> dict[str, Any]:
    """Edit a single task line in its source vault note. Located by exact line match."""
    root = _vault_root()
    if not root or not root.is_dir():
        return {"ok": False, "error": "Vault not found"}
    rel = (body.path or "").strip()
    if not rel:
        return {"ok": False, "error": "No path"}
    target = root / rel
    try:
        target.resolve().relative_to(root.resolve())
    except Exception:
        return {"ok": False, "error": "Path outside vault"}
    if not target.is_file():
        return {"ok": False, "error": "Note not found"}
    try:
        text = target.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"Read failed: {e}"}
    lines = text.split("\n")
    idx = next((i for i, l in enumerate(lines) if l == body.raw), None)
    if idx is None:
        idx = next((i for i, l in enumerate(lines) if l.strip() == (body.raw or "").strip()), None)
    if idx is None:
        return {"ok": False, "error": "Task not found; the note may have changed. Refresh."}
    before = lines[idx]
    if body.op == "delete":
        del lines[idx]
        new_raw = None
    else:
        nl = _rewrite_task_line(lines[idx], body.op, body.value or "")
        if nl is None:
            return {"ok": False, "error": "Unsupported edit"}
        lines[idx] = nl
        new_raw = nl
    if body.dry_run:
        return {"ok": True, "dry_run": True, "op": body.op,
                "before": before, "after": new_raw,
                "path": target.relative_to(root).as_posix()}
    backup = backup_file(target)              # snapshot before mutating
    try:
        target.write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"Write failed: {e}"}
    audit_log("vault.task.update",
              {"path": target.relative_to(root).as_posix(), "op": body.op,
               "before": before, "after": new_raw, "backup": backup})
    return {"ok": True, "raw": new_raw}
