"""Deliverables: documents drafted, rendered, reviewed and approved from the
Command Centre.

A deliverable is a note in `<vault>/13_Command Centre/Deliverables/`. Its
frontmatter is the record the library lists; its body is the document itself,
the single source of truth. Files are rendered from the body into
`Deliverables/_files/<id>/<id>-v<N>.<ext>` and are never edited by hand or
overwritten: every render is a new version.

A generated deliverable is also a production (orchestrator/production.py) with
a document format from doc_types.DOC_FORMATS. It moves through the same state
machine as a video, skips asset_plan, renders a file instead of a video, and
stops at review until Aaron approves the client_sensitive gate. Approval, from
the drawer, Operating or Telegram /pending, finalises it: a clean final render,
a PDF through Word with Open Sans embedded, and a Client-ready record. The
system never sends a document to anyone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from common.security import audit_log, require_admin

from . import dashboard as dash
from . import governance, production
from .doc_types import DOC_TYPES, GUARDRAILS, get_type, is_document, public_types

logger = logging.getLogger(__name__)

router = APIRouter(tags=["deliverables"])

FOLDER = "Deliverables"
FILES_DIR = "_files"
ARCHIVE_DIR = "_archive"
GATE = "client_sensitive"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.()'&,-]{0,160}$")
_VERSION_RE = re.compile(r"-v(\d+)\.(docx|pptx|pdf)$", re.I)
_MEDIA_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}

# Next state for a document. asset_plan is skipped; a legacy record sitting in
# asset_plan still moves on to render.
DOC_NEXT = {
    "idea": "brief",
    "brief": "research",
    "research": "outline",
    "outline": "draft",
    "draft": "render",
    "asset_plan": "render",
    "render": "review",
    "review": "publish",
    "publish": "measure",
}
DRAFTING_STATES = ("idea", "brief", "research", "outline", "draft", "asset_plan", "render")
STAGE_LABELS = {
    "idea": "Queued",
    "brief": "Brief",
    "research": "Research",
    "outline": "Outline",
    "draft": "Draft",
    "render": "Rendered",
    "review": "Quality review done",
    "publish": "Approved",
    "measure": "Approved",
    "cancelled": "Cancelled",
}

DRAWER_ACTIONS: dict[str, dict[str, str]] = {
    "summary": {
        "department": "support", "subagent": "responder-agent", "label": "Handoff summary",
        "ask": "Write a short handoff summary for Aaron: purpose, how the client should use it, caveats, "
               "and the next step. Bullets, not an email.",
    },
    "quality": {
        "department": "operations", "subagent": "quality-reviewer", "label": "Quality review",
        "ask": "Review this document. Give a pass or fail signal for evidence, structure, audience fit, "
               "WijerCo voice and client readiness, then the three most important fixes.",
    },
    "evidence": {
        "department": "research_intelligence", "subagent": "research-analyst", "label": "Evidence check",
        "ask": "Check evidence coverage. List claims that need a source, sources that look weak, and what "
               "to attach before handoff.",
    },
    "reuse": {
        "department": "marketing_sales", "subagent": "content-creator", "label": "Reuse paths",
        "ask": "Suggest reuse: a template, content posts, proposal evidence, future client assets and "
               "retrieval tags.",
    },
}

_RUNNING: set[str] = set()
_TASKS: set[asyncio.Task] = set()
_NOTE_LOCKS: dict[str, threading.Lock] = {}
_NOTE_LOCKS_GUARD = threading.Lock()


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%d %B %Y").lstrip("0")


def _one_line(value: Any, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _render_module():
    from media import doc_render

    return doc_render


def _folder(create: bool = False) -> Path | None:
    base = dash._base()
    if not base:
        return None
    folder = base / FOLDER
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder


def _require_folder(create: bool = False) -> Path:
    folder = _folder(create)
    if folder is None:
        raise HTTPException(status_code=503, detail="OBSIDIAN_VAULT_PATH is not configured")
    return folder


def _check_id(did: str) -> str:
    if not did or not _ID_RE.match(did) or ".." in did or "/" in did or "\\" in did:
        raise HTTPException(status_code=400, detail="invalid deliverable id")
    return did


def _note_path(did: str) -> Path:
    folder = _require_folder()
    path = folder / f"{_check_id(did)}.md"
    if path.resolve().parent != folder.resolve():
        raise HTTPException(status_code=400, detail="invalid deliverable id")
    return path


def _files_dir(did: str) -> Path:
    return _require_folder() / FILES_DIR / _check_id(did)


def _vault_rel(path: Path) -> str:
    vault = dash._vault_root()
    try:
        return path.resolve().relative_to(vault.resolve()).as_posix() if vault else path.name
    except ValueError:
        return path.name


def _note_lock(did: str) -> threading.Lock:
    with _NOTE_LOCKS_GUARD:
        return _NOTE_LOCKS.setdefault(did, threading.Lock())


def read_note(did: str) -> tuple[Path, dict[str, Any], str]:
    path = _note_path(did)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="deliverable not found")
    text = path.read_text(encoding="utf-8")
    return path, dash._parse_frontmatter(text), dash._strip_frontmatter(text)


def update_note(
    did: str,
    fm: dict[str, Any] | None = None,
    body: Callable[[str, str], tuple[str, str]] | None = None,
) -> dict[str, Any]:
    """Read-modify-write a note under a per-note lock. `body` receives and
    returns (document markdown, working notes)."""
    dr = _render_module()
    with _note_lock(did):
        path, current, text = read_note(did)
        merged = dict(current)
        for key, value in (fm or {}).items():
            if value is None:
                merged.pop(key, None)
            elif isinstance(value, str):
                merged[key] = _one_line(value, 600)
            else:
                merged[key] = value
        merged["updated_at"] = _now()
        doc_md, notes = dr.split_working_notes(text)
        if body is not None:
            doc_md, notes = body(doc_md, notes)
        dash._write_note(path, merged, dr.join_working_notes(doc_md, notes))
        return merged


def _append_note(title: str, content: str) -> Callable[[str, str], tuple[str, str]]:
    entry = f"### {title}, {_today()}\n\n{(content or '').strip() or 'No output.'}"

    def apply(doc_md: str, notes: str) -> tuple[str, str]:
        return doc_md, (notes.rstrip() + "\n\n" + entry).strip()
    return apply


def _set_document(markdown: str) -> Callable[[str, str], tuple[str, str]]:
    def apply(_doc_md: str, notes: str) -> tuple[str, str]:
        return markdown, notes
    return apply


def _add_cost(fm: dict[str, Any], cost: float) -> float:
    try:
        return round(float(fm.get("cost_usd") or 0) + float(cost or 0), 4)
    except (TypeError, ValueError):
        return round(float(cost or 0), 4)


def _latest_approval(pid: str) -> dict[str, Any] | None:
    items = governance.list_approvals(target_id=pid, limit=20)
    for row in items:
        if row.get("gate") == GATE:
            return row
    return None


# ---------------------------------------------------------------------------
# Live state: readiness comes from the production and the gate, never from a
# status word. Client-ready means approved and finalised, nothing else.
# ---------------------------------------------------------------------------

def live_state(fm: dict[str, Any], did: str) -> dict[str, Any]:
    pid = fm.get("production_id")
    if not pid or not is_document(fm.get("doc_type")):
        return {}
    prod = production.get_production(str(pid))
    if not prod:
        return {}
    state = prod.get("state") or "idea"
    running = did in _RUNNING
    approval = _latest_approval(str(pid))
    decision = (approval or {}).get("status")
    out: dict[str, Any] = {"stage": state, "stage_label": STAGE_LABELS.get(state, state), "running": running}
    if state == "cancelled":
        out.update(status="Cancelled", st="st-mute", readiness="Cancelled", gate_status="none")
    elif state in DRAFTING_STATES:
        if fm.get("status") == "Failed" and not running:
            out.update(status="Failed", st="st-fail", readiness="Blocked", gate_status="none")
        else:
            out.update(status="Drafting", st="st-warn", readiness="Drafting", gate_status="none")
    elif state == "review":
        if decision == "approved":
            out.update(status="Approved, finalising", st="st-warn", readiness="Finalising", gate_status="approved")
        elif decision == "rejected":
            out.update(status="Changes requested", st="st-fail", readiness="Changes requested", gate_status="rejected")
        else:
            out.update(status="Awaiting approval", st="st-warn", readiness="Awaiting approval", gate_status="pending")
    elif state in ("publish", "measure"):
        if decision == "approved":
            out.update(status="Approved", st="st-good", readiness="Client-ready", gate_status="approved")
        else:
            out.update(status="Published without approval", st="st-fail", readiness="Blocked", gate_status="missing")
    return out


def public_item(did: str, fm: dict[str, Any]) -> dict[str, Any]:
    item = {**fm, "id": did, "deliverable_id": fm.get("deliverable_id") or did}
    try:
        item.update(live_state(fm, did))
    except Exception as exc:  # noqa: BLE001 - a bad record must not break the list
        logger.warning("deliverable %s live state failed: %s", did, exc)
    if not is_document(fm.get("doc_type")):
        item.setdefault("legacy", True)
    return item


def list_items() -> dict[str, Any]:
    folder = _folder()
    if folder is None:
        return {"items": [], "demo": False, "error": "OBSIDIAN_VAULT_PATH is not configured"}
    if not folder.is_dir():
        return {"items": [], "demo": False, "source": "vault"}
    items: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.md")):
        try:
            fm = dash._parse_frontmatter(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if fm:
            items.append(public_item(path.stem, fm))
    items.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
    return {"items": items, "demo": False, "source": "vault"}


def versions(did: str) -> list[dict[str, Any]]:
    folder = _files_dir(did)
    if not folder.is_dir():
        return []
    by_version: dict[int, dict[str, Any]] = {}
    for path in folder.iterdir():
        m = _VERSION_RE.search(path.name)
        if not m or not path.name.startswith(f"{did}-v"):
            continue
        n, ext = int(m.group(1)), m.group(2).lower()
        stat = path.stat()
        by_version.setdefault(n, {"version": n})[ext] = {
            "name": path.name,
            "bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        }
    return [by_version[k] for k in sorted(by_version, reverse=True)]


def _next_version(did: str) -> int:
    existing = versions(did)
    return (existing[0]["version"] + 1) if existing else 1


def _deliverable_for(prod: dict[str, Any]) -> str | None:
    brief = prod.get("brief") or {}
    did = ((brief.get("input") or {}) if isinstance(brief, dict) else {}).get("deliverable_id")
    if did:
        return str(did)
    folder = _folder()
    if folder and folder.is_dir():
        for path in folder.glob("*.md"):
            try:
                if dash._parse_frontmatter(path.read_text(encoding="utf-8")).get("production_id") == prod.get("production_id"):
                    return path.stem
            except Exception:  # noqa: BLE001
                continue
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _meta_for(did: str, fm: dict[str, Any], version: int, final: bool) -> dict[str, Any]:
    spec = DOC_TYPES.get(str(fm.get("doc_type") or ""), {})
    return {
        "title": fm.get("title") or did,
        "type_label": spec.get("label") or fm.get("type") or "Deliverable",
        "client": fm.get("client") or fm.get("project") or "",
        "audience": fm.get("audience") or "",
        "date": _today(),
        "version": version,
        "status": "final" if final else "draft",
        "deliverable_id": did,
    }


def render_version(did: str, *, final: bool = False) -> dict[str, Any]:
    """Render the note body as the next version. Never overwrites."""
    dr = _render_module()
    _path, fm, body = read_note(did)
    ext = "pptx" if fm.get("doc_type") == "slide_deck" else "docx"
    version = _next_version(did)
    out = _files_dir(did) / f"{did}-v{version}.{ext}"
    result = dr.render(body, out, _meta_for(did, fm, version, final))
    rel = _vault_rel(out)
    update_note(did, {"doc_version": version, "doc_file": rel, "doc_format": ext})
    return {"version": version, "file": rel, "path": str(out), "render": result}


def _pdf_for(did: str, version: int, source: Path) -> dict[str, Any]:
    from media import office_convert

    pdf = source.with_suffix(".pdf")
    result = office_convert.convert(source, pdf, embed_fonts=True)
    if result.get("ok"):
        update_note(did, {"pdf_file": _vault_rel(pdf)})
    return result


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

async def _call_agent(department: str, subagent: str | None, query: str,
                      rag_context: list[dict] | None = None, force_model_key: str | None = None) -> dict[str, Any]:
    from .wijerco_agent import call_wijerco_agent

    return await call_wijerco_agent(
        department=department,
        query=query,
        rag_context=rag_context or [],
        conversation_history=[],
        subagent=subagent,
        force_model_key=force_model_key,
    )


async def _agent(department: str, subagent: str | None, query: str,
                 rag_context: list[dict] | None = None, model: str | None = None) -> tuple[str, float]:
    if model:
        result = await _call_agent(department, subagent, query, rag_context, force_model_key=model)
    else:
        result = await _call_agent(department, subagent, query, rag_context)
    if not isinstance(result, dict):
        return str(result or ""), 0.0
    answer = str(result.get("answer") or "")
    if not answer.strip():
        raise RuntimeError(result.get("error") or f"{department}/{subagent} returned no output")
    return answer, float(result.get("cost_usd") or 0.0)


async def _retrieve(query: str, top_k: int = 8) -> list[dict[str, Any]]:
    if os.getenv("DELIVERABLES_RETRIEVAL", "on").strip().lower() in ("off", "0", "false"):
        return []
    def _search_in_own_loop() -> list[dict[str, Any]]:
        # The reranker and BM25 are CPU-bound and the first call loads a model.
        # Running them on a worker thread keeps the Command Centre responsive.
        from rag.retriever import search

        return list(asyncio.run(search(query, top_k=top_k)))

    try:
        return await asyncio.wait_for(asyncio.to_thread(_search_in_own_loop), timeout=90)
    except Exception as exc:  # noqa: BLE001 - retrieval is best effort
        logger.info("deliverables retrieval skipped: %s", exc)
        return []


def _clean_markdown(text: str, title: str) -> str:
    md = (text or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", md, re.S)
    if fence:
        md = fence.group(1).strip()
    heading = re.search(r"(?m)^#{1,6} ", md)
    if heading and heading.start() > 0:
        md = md[heading.start():]  # drop any preamble before the first heading
    md = _house_style(md)
    if not md.startswith("# "):
        md = f"# {title}\n\n{md}"
    return md.rstrip() + "\n"


_FEE_RE = re.compile(r"(\$\s?\d|\bAUD\s?\d|\bfees?\b|\bpric(?:e|es|ing)\b|\bday rate\b|\bper hour\b|\bcost estimate\b)", re.I)


def _inputs(prod: dict[str, Any]) -> dict[str, Any]:
    brief = prod.get("brief") or {}
    return dict((brief.get("input") or {}) if isinstance(brief, dict) else {})


def _context_block(prod: dict[str, Any], spec: dict[str, Any]) -> str:
    inp = _inputs(prod)
    lines = [
        f"Document type: {spec['label']} ({spec['length']})",
        f"Title: {prod.get('title')}",
        f"Client: {inp.get('client') or 'Not recorded'}",
        f"Audience: {inp.get('audience') or 'Not recorded'}",
    ]
    if inp.get("due"):
        lines.append(f"Due: {inp['due']}")
    if spec["sections"]:
        lines.append("Required sections, in order: " + "; ".join(spec["sections"]))
    lines.append(f"Guidance: {spec['guidance']}")
    lines.append("")
    lines.append("Brief from Aaron:")
    lines.append(inp.get("brief") or "Not recorded")
    if inp.get("sources"):
        lines.append("")
        lines.append("Sources Aaron listed:")
        lines.append(inp["sources"])
    return "\n".join(lines)


def _parse_json(text: Any) -> Any:
    """Agent JSON, tolerating a preamble or a code fence anywhere in the reply."""
    parsed = production._parse_agent_output(text)
    if not (isinstance(parsed, dict) and "_raw" in parsed):
        return parsed
    raw = str(parsed.get("_raw") or "")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
    candidates = [fenced.group(1)] if fenced else []
    if "{" in raw and "}" in raw:
        candidates.append(raw[raw.find("{"): raw.rfind("}") + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return parsed


def _as_json(value: Any, limit: int = 12000) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)[:limit]


# ---------------------------------------------------------------------------
# Pipeline steps. Each returns {"note": str, "cost": float, ...}.
# ---------------------------------------------------------------------------

async def _step_brief(prod: dict, did: str, spec: dict) -> dict[str, Any]:
    dept, sub = spec["drafting"]
    query = (
        f"{_context_block(prod, spec)}\n\n{GUARDRAILS}\n\n"
        "Task: turn this into a structured brief for the writer. Return only JSON:\n"
        '{"purpose": "...", "audience": "...", "decision_or_ask": "...", "key_questions": ["..."], '
        '"sections": [{"heading": "...", "intent": "..."}], "must_include": ["..."], "open_questions": ["..."]}'
    )
    answer, cost = await _agent(dept, sub, query)
    parsed = _parse_json(answer)
    production.update_production(prod["production_id"], brief={"input": _inputs(prod), "brief": parsed})
    return {"note": f"brief by {dept}/{sub}", "cost": cost}


async def _step_research(prod: dict, did: str, spec: dict) -> dict[str, Any]:
    inp = _inputs(prod)
    if spec.get("needs_source"):
        src = inp.get("source_deliverable_id")
        text = ""
        if src:
            try:
                _p, _fm, body = read_note(str(src))
                text = _render_module().split_working_notes(body)[0]
            except HTTPException:
                text = ""
        if not text.strip():
            raise RuntimeError("the source deliverable for this deck has no document text")
        production.update_production(prod["production_id"], research={"source_document": src, "excerpt": text[:20000]})
        return {"note": f"source document {src} loaded", "cost": 0.0}

    brief = (prod.get("brief") or {}).get("brief") or {}
    questions = " ".join(str(q) for q in (brief.get("key_questions") or [])[:5]) if isinstance(brief, dict) else ""
    chunks = await _retrieve(f"{prod.get('title')}. {inp.get('brief', '')[:400]} {questions}".strip())
    dept, sub = spec.get("research") or spec["drafting"]
    query = (
        f"{_context_block(prod, spec)}\n\nStructured brief:\n{_as_json(brief)}\n\n"
        "Retrieved context, if any, is in your system prompt numbered [1] onwards.\n\n"
        f"{GUARDRAILS}\n\n"
        "Task: list the evidence the writer may use. Use only the retrieved context and the sources Aaron "
        "listed. Return only JSON:\n"
        '{"facts": [{"claim": "...", "source": "[n] or source name"}], '
        '"sources": [{"ref": "[n]", "title": "...", "detail": "file, section or URL"}], '
        '"gaps": ["evidence that is missing"]}'
    )
    answer, cost = await _agent(dept, sub, query, rag_context=chunks)
    parsed = _parse_json(answer)
    retrieved = [
        {"file": c.get("file") or c.get("source"), "section": c.get("section"), "score": c.get("score")}
        for c in chunks
    ]
    production.update_production(prod["production_id"], research={"retrieved": retrieved, "evidence": parsed})
    gaps = parsed.get("gaps") if isinstance(parsed, dict) else None
    if gaps:
        update_note(did, body=_append_note("Evidence gaps", "\n".join(f"- {g}" for g in gaps[:12])))
    return {"note": f"research by {dept}/{sub}, {len(chunks)} retrieved chunks", "cost": cost}


async def _step_outline(prod: dict, did: str, spec: dict) -> dict[str, Any]:
    dept, sub = spec["drafting"]
    research = prod.get("research") or {}
    if spec.get("needs_source"):
        evidence = {"source_document_excerpt": str(research.get("excerpt") or "")[:12000]}
        shape = '{"slides": [{"title": "...", "message": "one sentence", "points": ["..."]}]}'
        ask = "Plan a deck of 8 to 12 slides, one message per slide, drawn only from the source document."
    else:
        evidence = research.get("evidence") or {}
        shape = '{"sections": [{"heading": "...", "points": ["..."]}]}'
        ask = "Outline the document section by section, following the required sections."
    query = (
        f"{_context_block(prod, spec)}\n\nStructured brief:\n{_as_json((prod.get('brief') or {}).get('brief'))}\n\n"
        f"Evidence:\n{_as_json(evidence)}\n\n{GUARDRAILS}\n\nTask: {ask} Return only JSON:\n{shape}"
    )
    answer, cost = await _agent(dept, sub, query)
    parsed = _parse_json(answer)
    production.update_production(prod["production_id"], script={"outline": parsed})
    return {"note": f"outline by {dept}/{sub}", "cost": cost}


_SECTION_WEIGHT = {
    "summary": 0.6, "decision sought": 0.5, "next steps": 0.5, "team": 0.7, "risks": 0.9,
    "findings": 1.6, "options": 1.7, "approach": 1.6, "deliverables and timeline": 1.3,
    "recommendation": 1.1, "recommendations": 1.1,
}


def _word_range(spec: dict) -> tuple[int, int]:
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", str(spec.get("words") or ""))]
    if len(nums) >= 2:
        return nums[0], nums[1]
    return (nums[0], nums[0]) if nums else (1200, 2000)


def _section_plan(prod: dict, spec: dict) -> list[dict[str, Any]]:
    """Required sections (Sources excluded, it is built from the evidence),
    each with the outline's points and a word budget."""
    outline = (prod.get("script") or {}).get("outline") or {}
    entries = outline.get("sections") if isinstance(outline, dict) else None
    entries = [e for e in (entries or []) if isinstance(e, dict)]
    by_heading = {_norm_heading(e.get("heading")): e for e in entries}
    headings = [h for h in spec["sections"] if h.lower() != "sources"]
    low, high = _word_range(spec)
    total = (low + high) / 2
    weights = [_SECTION_WEIGHT.get(h.lower(), 1.0) for h in headings]
    plan = []
    for i, heading in enumerate(headings):
        entry = by_heading.get(_norm_heading(heading)) or (entries[i] if i < len(entries) else {})
        points = entry.get("points") if isinstance(entry.get("points"), list) else []
        words = max(120, int(round(total * weights[i] / sum(weights), -1)))
        plan.append({"heading": heading, "points": [str(p) for p in points][:8], "words": words})
    return plan


def _norm_heading(value: Any) -> str:
    return re.sub(r"[^a-z]+", " ", str(value or "").lower()).strip()


def _house_style(md: str) -> str:
    md = re.sub(r"[ \t]*—[ \t]*", ", ", md or "")  # house style: no em dashes
    md = re.sub(r"[ \t]+–[ \t]+", ", ", md)  # nor spaced en dashes used the same way; ranges keep theirs
    # Source lines ("[1] ...") and open items ("[To confirm: ...]") arrive one
    # per line; as bullets they stay separate in Word instead of running on.
    lines = []
    for line in md.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("|") and re.match(r"^\[(\d+|To confirm)", stripped):
            line = "- " + stripped
        lines.append(line)
    return "\n".join(lines)


def _clean_section(text: str, heading: str) -> str:
    md = (text or "").strip()
    fence = re.match(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", md, re.S)
    if fence:
        md = fence.group(1).strip()
    first = re.search(r"(?m)^#{1,6} ", md)
    if first and first.start() > 0 and not md[:first.start()].strip().startswith(("-", "|", "*")):
        md = md[first.start():]  # drop any preamble before the section heading
    md = "\n".join(line for line in md.split("\n") if not re.match(r"^# ", line.strip()))
    cut = re.search(r"(?mi)^#{1,3}\s*(sources|references)\s*$", md)
    if cut:
        md = md[:cut.start()]
    md = md.strip()
    head = re.match(r"^##\s+.*$", md.split("\n", 1)[0]) if md else None
    if head:
        md = f"## {heading}" + ("\n" + md.split("\n", 1)[1] if "\n" in md else "")
    else:
        md = f"## {heading}\n\n{md}"
    return _house_style(md).rstrip() + "\n"


def _cited_numbers(markdown: str) -> set[int]:
    cited: set[int] = set()
    for group in re.findall(r"\[(\d+(?:\s*[,;\-\u2013]\s*\d+)*)\]", markdown or ""):
        for n in re.findall(r"\d+", group):
            cited.add(int(n))
    return cited


def _sources_block(prod: dict, body: str = "") -> str:
    """The Sources section, built from the research step's source list and
    limited to sources the body actually cites."""
    evidence = (prod.get("research") or {}).get("evidence") or {}
    items = evidence.get("sources") if isinstance(evidence, dict) else None
    cited = _cited_numbers(body)
    lines = []
    for i, source in enumerate(items or [], start=1):
        if isinstance(source, dict):
            ref = str(source.get("ref") or f"[{i}]").strip()
            if not ref.startswith("["):
                ref = f"[{ref}]"
            title = str(source.get("title") or "").strip()
            if title.startswith("---") or "cap:" in title:
                title = "Retrieved vault note"  # a note's raw frontmatter is not a title
            text = ", ".join(x for x in (title, str(source.get("detail") or "").strip()) if x)
        else:
            ref, text = f"[{i}]", str(source).strip()
        number = re.search(r"\d+", ref)
        if cited and number and int(number.group(0)) not in cited:
            continue
        lines.append(f"- {ref} {text or 'Source recorded without a title'}")
    if not lines:
        lines = ["- [To confirm: no sources were recorded for this draft]"]
    return "## Sources\n\n" + "\n".join(lines) + "\n"


def _draft_model() -> str | None:
    """DELIVERABLES_DRAFT_MODEL pins the model that writes sections, for
    example google/gemini-2.5-flash or anthropic/claude-sonnet-4-6. Unset,
    the normal cost-ordered routing applies."""
    value = os.getenv("DELIVERABLES_DRAFT_MODEL", "").strip()
    return value or None


async def _write_section(prod: dict, spec: dict, section: dict, plan: list[dict],
                         shared: str, written: str) -> dict[str, Any]:
    dept, sub = spec["drafting"]
    heading, words = section["heading"], section["words"]
    others = "; ".join(s["heading"] for s in plan if s["heading"] != heading)
    points = "\n".join(f"- {p}" for p in section["points"]) or "- Follow the brief and the evidence."
    earlier = (
        "\n\nAlready written above. Do not restate its figures or points; build on them and refer back "
        f"briefly where needed:\n{written[-7000:]}"
        if written.strip() else ""
    )
    ask = (
        f"Write only the \"## {heading}\" section of this {spec['label'].lower()}. About {words} words. "
        f"Start with the line \"## {heading}\". Cover:\n{points}\n"
        f"Other sections ({others}) are written separately: do not repeat their content, and do not add a "
        "title or a sources list. Cite evidence inline as [n] using the numbered sources. Use a markdown "
        "table if this section compares options or figures. Use short paragraphs and specific detail from "
        "the evidence. Return only the markdown for this section."
        + (" This is a proposal: include no fees, prices, rates or cost estimates at all."
           if prod.get("format") == "proposal" else "")
    )
    model = _draft_model()
    cost = 0.0
    text, c = await _agent(dept, sub, f"{shared}{earlier}\n\nTask: {ask}", model=model)
    cost += c
    md = _clean_section(text, heading)
    got = _render_module().word_count(md)
    expanded = False
    if got < 0.6 * words:
        more, c = await _agent(
            dept, sub,
            f"{shared}{earlier}\n\nThis draft of the \"## {heading}\" section is {got} words; it needs about {words}. "
            "Expand it with more specific detail, examples and implications drawn from the evidence and the "
            "brief. Keep every citation and [To confirm] marker, do not invent facts, and do not add a title "
            f"or a sources list. Return only the expanded section.\n\n{md}",
            model=model,
        )
        cost += c
        longer = _clean_section(more, heading)
        if _render_module().word_count(longer) > got:
            md, got, expanded = longer, _render_module().word_count(longer), True
    return {"heading": heading, "target": words, "words": got, "expanded": expanded, "markdown": md, "cost": cost}


async def _step_draft(prod: dict, did: str, spec: dict) -> dict[str, Any]:
    dept, sub = spec["drafting"]
    title = prod.get("title") or did
    research = prod.get("research") or {}
    outline = (prod.get("script") or {}).get("outline")
    brief_json = _as_json((prod.get("brief") or {}).get("brief"))
    cost = 0.0
    sections_meta: list[dict[str, Any]] = []
    if spec.get("needs_source"):
        fmt = (
            "Write the deck in this exact markdown format:\n"
            f"# {title}\nOne-line subtitle\n\n## The slide's message as a short title (no slide numbers)\n"
            "- up to five short bullets\n"
            "Notes: two to four sentences the presenter says out loud.\n\n"
            "Repeat the ## block for each of 8 to 12 slides. Every slide must have a Notes: line. "
            "Use only facts from the source document."
        )
        query = (
            f"{_context_block(prod, spec)}\n\nStructured brief:\n{brief_json}\n\n"
            f"Source document:\n{str(research.get('excerpt') or '')[:16000]}\n\nOutline:\n{_as_json(outline)}\n\n"
            f"{GUARDRAILS}\n\nTask: {fmt}\nReturn only the markdown document, with no preamble."
        )
        answer, cost = await _agent(dept, sub, query)
        markdown = _clean_markdown(answer, title)
        detail = "deck"
    else:
        # One call per section, each with its own word budget, written in
        # order so each section sees the ones before it and does not repeat
        # them. A single call for the whole document came back at about half
        # the target length.
        plan = _section_plan(prod, spec)
        shared = (
            f"{_context_block(prod, spec)}\n\nStructured brief:\n{brief_json}\n\n"
            f"Evidence (cite as [n]):\n{_as_json(research.get('evidence') or {}, 9000)}\n\n"
            f"Outline of the whole document:\n{_as_json(outline, 5000)}\n\n{GUARDRAILS}"
        )
        results = []
        for section in plan:
            written = "\n".join(r["markdown"] for r in results)
            results.append(await _write_section(prod, spec, section, plan, shared, written))
        cost = sum(r["cost"] for r in results)
        parts = [f"# {title}\n"] + [r["markdown"] for r in results]
        if any(h.lower() == "sources" for h in spec["sections"]):
            parts.append(_sources_block(prod, "\n".join(r["markdown"] for r in results)))
        markdown = "\n".join(p.rstrip() + "\n" for p in parts)
        sections_meta = [{k: r[k] for k in ("heading", "target", "words", "expanded")} for r in results]
        expanded = sum(1 for r in results if r["expanded"])
        detail = f"{len(results)} sections" + (f", {expanded} expanded" if expanded else "")
    polished_by = ""
    if spec.get("polish"):
        pdept, psub = spec["polish"]
        before = _render_module().word_count(markdown)
        ptext, pcost = await _agent(
            pdept, psub,
            f"Edit this {spec['label'].lower()} for clarity and flow. Keep its length (within 10 percent), "
            "every heading, table, citation and [To confirm] marker. Do not add facts, fees or prices. "
            f"Return only the revised markdown.\n\n{GUARDRAILS}\n\n{markdown}",
        )
        cost += pcost
        polished = _clean_markdown(ptext, title)
        if _render_module().word_count(polished) >= 0.85 * before:
            markdown, polished_by = polished, f", polished by {pdept}/{psub}"
    words = _render_module().word_count(markdown)
    script = dict(prod.get("script") or {})
    script["draft"] = markdown
    if sections_meta:
        script["draft_sections"] = sections_meta
    production.update_production(prod["production_id"], script=script)
    update_note(did, {"words": words}, body=_set_document(markdown))
    if prod.get("format") == "proposal":
        hits = sorted({m.group(0).strip() for m in _FEE_RE.finditer(markdown)})
        if hits:
            update_note(did, body=_append_note(
                "Check before approval",
                "The proposal should carry no fees, but the draft mentions: " + ", ".join(hits[:8]),
            ))
    return {"note": f"draft by {dept}/{sub}: {detail}, {words} words{polished_by}", "cost": cost}


async def _step_render(prod: dict, did: str, spec: dict) -> dict[str, Any]:
    rendered = await asyncio.to_thread(render_version, did, final=False)
    production.update_production(prod["production_id"], edit_plan={"doc": {
        "version": rendered["version"], "file": rendered["file"], "final": False,
    }})
    skipped = " (asset_plan skipped for documents)" if prod.get("state") == "draft" else ""
    return {"note": f"rendered v{rendered['version']}{skipped}", "cost": 0.0, "render": rendered}


async def _step_review(prod: dict, did: str, spec: dict) -> dict[str, Any]:
    dept, sub = spec["reviewer"]
    _p, fm, body = read_note(did)
    doc_md = _render_module().split_working_notes(body)[0]
    sources = ((prod.get("research") or {}).get("evidence") or {}).get("sources") if isinstance(
        (prod.get("research") or {}).get("evidence"), dict) else None
    required = "; ".join(spec["sections"]) or "title slide, 8 to 12 slides, notes on every slide"
    query = (
        f"Review this {spec['label'].lower()} before Aaron approves it for a client.\n"
        f"Check: evidence (every figure tied to a source), structure (required: {required}), audience fit "
        "(audience: " + str(_inputs(prod).get("audience") or "not recorded") + "), WijerCo voice (Australian "
        "English, short sentences, no em dashes), "
        + ("no fees, prices or rates anywhere (this is a proposal), " if prod.get("format") == "proposal"
           else "costs or prices stated only when a source supports them, ")
        + "no claims that anything was sent or agreed, and any [To confirm] markers left.\n"
        "Return only JSON: {\"verdict\": \"pass\" or \"revise\", \"summary\": \"two sentences\", "
        "\"checks\": {\"evidence\": \"pass or fail: why\", \"structure\": \"...\", \"audience_fit\": \"...\", "
        "\"voice\": \"...\", \"costs\": \"...\"}, \"issues\": [\"specific fix\"], "
        "\"to_confirm\": [\"open item\"]}\n\n"
        f"Sources available:\n{_as_json(sources or [], 4000)}\n\nDocument:\n{doc_md[:24000]}"
    )
    answer, cost = await _agent(dept, sub, query)
    parsed = _parse_json(answer)
    production.update_production(prod["production_id"], review=parsed)
    if isinstance(parsed, dict) and "_raw" not in parsed:
        checks = parsed.get("checks") or {}
        lines = [str(parsed.get("summary") or "").strip(), ""]
        lines.append(f"Verdict: {parsed.get('verdict') or 'not given'}")
        for key, value in checks.items():
            lines.append(f"- {key.replace('_', ' ').capitalize()}: {value}")
        issues = parsed.get("issues") or []
        if issues:
            lines += ["", "Fixes:"] + [f"- {x}" for x in issues[:10]]
        confirm = parsed.get("to_confirm") or []
        if confirm:
            lines += ["", "To confirm:"] + [f"- {x}" for x in confirm[:10]]
        text = "\n".join(lines).strip()
        evidence = str(checks.get("evidence") or "")
    else:
        text = str(parsed.get("_raw") if isinstance(parsed, dict) else parsed)
        evidence = ""
    ev_label = ("Strong evidence" if evidence.lower().startswith("pass")
                else "Check evidence" if evidence else "Not checked")
    update_note(did, {"evidence": ev_label}, body=_append_note(f"Quality review by {dept}/{sub}", text))
    return {"note": f"quality review by {dept}/{sub}: {(parsed or {}).get('verdict') if isinstance(parsed, dict) else 'done'}",
            "cost": cost}


async def _step_publish(prod: dict, did: str, spec: dict) -> dict[str, Any]:
    """Finalise after approval: clean final render, PDF, Client-ready record."""
    rendered = await asyncio.to_thread(render_version, did, final=True)
    pdf: dict[str, Any] = {"ok": False, "skipped": True}
    try:
        pdf = await asyncio.to_thread(_pdf_for, did, rendered["version"], Path(rendered["path"]))
    except Exception as exc:  # noqa: BLE001 - a PDF failure must not block approval
        pdf = {"ok": False, "error": str(exc)}
    approval = _latest_approval(prod["production_id"]) or {}
    fm = update_note(did, {
        "approved_at": approval.get("at") or _now(),
        "approved_by": approval.get("actor") or "operator",
        "status": "Approved",
        "st": "st-good",
        "readiness": "Client-ready",
        "gate_status": "approved",
        "next_action": "Send it to the client yourself",
        "pdf_error": None if pdf.get("ok") or pdf.get("skipped") else _one_line(pdf.get("error"), 200),
    })
    production.update_production(prod["production_id"], edit_plan={"doc": {
        "version": rendered["version"], "file": rendered["file"], "final": True,
        "pdf": fm.get("pdf_file") if pdf.get("ok") else None,
    }})
    if prod.get("project"):
        try:
            from . import operating

            operating.add_project_memory(
                prod["project"],
                f"Deliverable approved: {prod.get('title')} ({spec['label']}), v{rendered['version']}.",
                source="deliverables",
                meta={"deliverable_id": did, "production_id": prod["production_id"], "file": rendered["file"]},
            )
        except Exception:  # noqa: BLE001
            pass
    audit_log("deliverable.finalise", {"deliverable_id": did, "production_id": prod["production_id"],
                                       "version": rendered["version"], "pdf": bool(pdf.get("ok"))})
    pdf_note = "PDF ready" if pdf.get("ok") else ("no PDF" if pdf.get("skipped") else "PDF failed")
    return {"note": f"approved and finalised as v{rendered['version']}, {pdf_note}", "cost": 0.0}


async def _step_measure(prod: dict, did: str, spec: dict) -> dict[str, Any]:
    return {"note": "closed", "cost": 0.0}


STEPS: dict[tuple[str, str], Callable[[dict, str, dict], Awaitable[dict[str, Any]]]] = {
    ("idea", "brief"): _step_brief,
    ("brief", "research"): _step_research,
    ("research", "outline"): _step_outline,
    ("outline", "draft"): _step_draft,
    ("draft", "render"): _step_render,
    ("asset_plan", "render"): _step_render,
    ("render", "review"): _step_review,
    ("review", "publish"): _step_publish,
    ("publish", "measure"): _step_measure,
}


async def _notify(title: str, body: str) -> None:
    if os.getenv("DELIVERABLES_NOTIFY", "on").strip().lower() in ("off", "0", "false"):
        return
    url = os.getenv("NOTIFIER_URL", "http://localhost:8004")
    try:
        import httpx

        async with httpx.AsyncClient() as client:
            await client.post(f"{url}/notify", json={"title": title, "body": body,
                                                     "tags": ["deliverables", "governance"]}, timeout=5.0)
    except Exception:  # noqa: BLE001
        pass


async def advance_document(production_id: str, actor: str = "operator") -> dict[str, Any]:
    """Advance a document production by one state. production.advance()
    delegates here for every format in DOC_FORMATS."""
    prod = production.get_production(production_id)
    if not prod:
        raise KeyError("production not found")
    state = prod.get("state") or "idea"
    if state in ("cancelled", "measure") or state not in DOC_NEXT:
        return {"production": prod, "agent_output": [], "done": True}
    nxt = DOC_NEXT[state]
    pending = governance.pending_gates(prod, nxt)
    if pending:
        return {"blocked": True, "gate": pending[0]["gate"], "pending": pending, "production": prod}
    did = _deliverable_for(prod)
    if not did:
        raise ValueError("No Deliverables note is linked to this production. Create documents from the Deliverables section.")
    spec = get_type(prod["format"])
    result = await STEPS[(state, nxt)](prod, did, spec)
    _p, fm, _b = read_note(did)
    update_note(did, {
        "cost_usd": _add_cost(fm, result.get("cost", 0.0)),
        "meta": f"{spec['label']} · {STAGE_LABELS.get(nxt, nxt)}",
        "last_error": None,
        **({"status": "Drafting", "st": "st-warn", "readiness": "Drafting"} if nxt in DRAFTING_STATES else {}),
        **({"status": "Awaiting approval", "st": "st-warn", "readiness": "Awaiting approval",
            "gate_status": "pending", "next_action": "Approve for the client, or edit the note and re-render"}
           if nxt == "review" else {}),
    })
    updated = production.transition(production_id, nxt, actor, result.get("note") or f"advanced from {state}")
    if nxt == "review":
        await _notify("Deliverable ready for approval",
                      f"{prod.get('title')} ({spec['label']}) is ready. Approve it in the Command Centre "
                      "Deliverables drawer or with /pending.")
    return {"production": updated, "agent_output": [{"subagent": "deliverables", "field": nxt, "output": result}]}


async def run_pipeline(did: str, actor: str = "operator") -> dict[str, Any]:
    """Advance a document until it reaches review (or fails or blocks)."""
    if did in _RUNNING:
        return {"ok": False, "reason": "already running"}
    _RUNNING.add(did)
    last: dict[str, Any] = {}
    try:
        _p, fm, _b = read_note(did)
        pid = fm.get("production_id")
        if not pid:
            raise RuntimeError("note has no production")
        for _ in range(12):
            prod = production.get_production(str(pid))
            if not prod or prod.get("state") not in DRAFTING_STATES:
                break
            last = await advance_document(str(pid), actor=actor)
            if last.get("blocked") or last.get("done"):
                break
        return {"ok": True, "state": (production.get_production(str(pid)) or {}).get("state")}
    except Exception as exc:  # noqa: BLE001
        logger.exception("deliverable %s failed", did)
        try:
            _p, fm, _b = read_note(did)
            pid = fm.get("production_id")
            prod = production.get_production(str(pid)) if pid else None
            if prod:
                production.record_event(str(pid), prod["state"], prod["state"], actor,
                                        f"failed at {prod['state']}: {_one_line(exc, 240)}")
            update_note(did, {"status": "Failed", "st": "st-fail", "readiness": "Blocked",
                              "last_error": _one_line(exc, 300),
                              "next_action": "Check the error, then Continue drafting"})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": str(exc)}
    finally:
        _RUNNING.discard(did)


def _spawn(coro: Awaitable[Any]) -> None:
    """Run a coroutine without blocking the caller: on the running loop when
    there is one, otherwise on a background thread."""
    if os.getenv("DELIVERABLES_SYNC") == "1":
        thread = threading.Thread(target=lambda: asyncio.run(coro), daemon=True)
        thread.start()
        thread.join(timeout=300)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        task = loop.create_task(coro)
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)
    else:
        threading.Thread(target=lambda: asyncio.run(coro), daemon=True).start()


def on_gate_decision(row: dict[str, Any]) -> None:
    """Called by governance.approve() for every decision. Finalises a document
    when its client_sensitive gate is approved; records a rejection."""
    if row.get("gate") != GATE:
        return
    prod = production.get_production(str(row.get("target_id") or ""))
    if not prod or not is_document(prod.get("format")):
        return
    did = _deliverable_for(prod)
    if not did:
        return
    if row.get("status") == "rejected":
        update_note(did, {"status": "Changes requested", "st": "st-fail", "readiness": "Changes requested",
                          "gate_status": "rejected",
                          "next_action": "Edit the note, re-render, then request approval again"},
                    body=_append_note(f"Changes requested by {row.get('actor') or 'operator'}",
                                      row.get("note") or "No reason given."))
        return
    if row.get("status") == "approved" and prod.get("state") == "review":
        _spawn(_finalise(prod["production_id"], str(row.get("actor") or "operator")))


async def _finalise(production_id: str, actor: str) -> None:
    try:
        await advance_document(production_id, actor=actor)
    except Exception as exc:  # noqa: BLE001
        logger.exception("finalise failed for %s", production_id)
        prod = production.get_production(production_id)
        did = _deliverable_for(prod) if prod else None
        if did:
            update_note(did, {"last_error": _one_line(f"finalise failed: {exc}", 300)})


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class DeliverableCreate(BaseModel):
    doc_type: str
    title: str = Field(..., min_length=3, max_length=200)
    brief: str = Field("", max_length=8000)
    client: str | None = Field(None, max_length=160)
    project: str | None = Field(None, max_length=160)
    audience: str | None = Field(None, max_length=200)
    sources: str | None = Field(None, max_length=6000)
    due: str | None = Field(None, max_length=40)
    source_deliverable_id: str | None = Field(None, max_length=170)
    run: bool = True
    actor: str = "operator"


class DeliverableAction(BaseModel):
    action: str
    actor: str = "operator"


class DeliverableActor(BaseModel):
    actor: str = "operator"
    note: str = ""


@router.get("/deliverables/types")
async def deliverable_types() -> dict[str, Any]:
    return {"items": public_types()}


@router.post("/deliverables", dependencies=[Depends(require_admin)])
async def create_deliverable(req: DeliverableCreate, background: BackgroundTasks) -> dict[str, Any]:
    if req.doc_type not in DOC_TYPES:
        raise HTTPException(status_code=422, detail=f"doc_type must be one of {tuple(DOC_TYPES)}")
    spec = DOC_TYPES[req.doc_type]
    source_id = None
    if spec.get("needs_source"):
        if not req.source_deliverable_id:
            raise HTTPException(status_code=422, detail="A slide deck is built from an existing deliverable. Pick one.")
        source_id = _check_id(req.source_deliverable_id)
        _p, _fm, body = read_note(source_id)
        if not _render_module().split_working_notes(body)[0].strip():
            raise HTTPException(status_code=422, detail="The source deliverable has no document text yet.")
    elif len(req.brief.strip()) < 10:
        raise HTTPException(status_code=422, detail="Write a short brief: what the document is for and who reads it.")

    folder = _require_folder(create=True)
    base = dash._slugify_title(req.title)
    did, k = base, 2
    while (folder / f"{did}.md").exists() or (folder / FILES_DIR / did).exists():
        did, k = f"{base}-{k}", k + 1

    inputs = {
        "deliverable_id": did,
        "doc_type": req.doc_type,
        "brief": req.brief.strip(),
        "client": _one_line(req.client),
        "project": _one_line(req.project),
        "audience": _one_line(req.audience),
        "sources": (req.sources or "").strip(),
        "due": _one_line(req.due, 40),
        "source_deliverable_id": source_id,
    }
    pid = production.create_production(req.title.strip(), (req.project or req.client or None), req.doc_type,
                                      owner="Command Centre")
    production.update_production(pid, brief={"input": inputs})

    fm = {
        "title": _one_line(req.title, 200),
        "cap": spec["cap"],
        "type": spec["label"],
        "status": "Queued",
        "st": "st-mute",
        "meta": f"{spec['label']} · Queued",
        "deliverable_id": did,
        "doc_type": req.doc_type,
        "production_id": pid,
        "project": inputs["project"],
        "client": inputs["client"],
        "audience": inputs["audience"],
        "due": inputs["due"],
        "source": "Production",
        "source_deliverable": source_id or "",
        "readiness": "Drafting",
        "evidence": "Not checked yet",
        "confidence": "Medium",
        "next_action": "Drafting runs automatically",
        "doc_version": 0,
        "cost_usd": 0.0,
        "gate_status": "none",
        "created_at": _now(),
        "updated_at": _now(),
    }
    fm = {k: v for k, v in fm.items() if v not in ("", None)}
    brief_note = f"### Brief, {_today()}\n\n{inputs['brief'] or 'Built from ' + str(source_id)}"
    if inputs["sources"]:
        brief_note += f"\n\nSources to use:\n\n{inputs['sources']}"
    dr = _render_module()
    placeholder = f"# {req.title.strip()}\n\n_Drafting has not finished. This note becomes the document once the draft is written._\n"
    dash._write_note(folder / f"{did}.md", fm, dr.join_working_notes(placeholder, brief_note))
    audit_log("deliverable.create", {"deliverable_id": did, "production_id": pid, "doc_type": req.doc_type,
                                    "actor": req.actor})
    if req.run:
        background.add_task(run_pipeline, did, req.actor)
    return {"ok": True, "item": public_item(did, fm), "production_id": pid, "running": req.run}


@router.get("/deliverables/{did}")
async def get_deliverable(did: str) -> dict[str, Any]:
    path, fm, body = read_note(did)
    doc_md, notes = _render_module().split_working_notes(body)
    pid = fm.get("production_id")
    prod = production.get_production(str(pid)) if pid else None
    summary = None
    gates: dict[str, Any] = {"pending": [], "approvals": []}
    if prod:
        summary = {
            "production_id": prod["production_id"],
            "state": prod.get("state"),
            "format": prod.get("format"),
            "review": prod.get("review") or {},
            "events": [
                {k: e.get(k) for k in ("at", "from_state", "to_state", "actor", "note")}
                for e in (prod.get("events") or [])[-30:]
            ],
        }
        gates = {
            "pending": governance.pending_gates(prod) if prod.get("state") == "review" else [],
            "approvals": governance.list_approvals(target_id=prod["production_id"], limit=10),
        }
    vault = dash._vault_root()
    uri = None
    if vault:
        rel = _vault_rel(path)
        uri = f"obsidian://open?vault={quote(vault.name)}&file={quote(rel[:-3] if rel.endswith('.md') else rel)}"
    return {
        "item": public_item(did, fm),
        "body": doc_md,
        "working_notes": notes,
        "versions": versions(did),
        "production": summary,
        "gates": gates,
        "running": did in _RUNNING,
        "obsidian_uri": uri,
        "path": _vault_rel(path),
    }


@router.post("/deliverables/{did}/render", dependencies=[Depends(require_admin)])
async def rerender_deliverable(did: str, req: DeliverableActor | None = None) -> dict[str, Any]:
    actor = (req.actor if req else None) or "operator"
    _path, fm, body = read_note(did)
    if did in _RUNNING:
        raise HTTPException(status_code=409, detail="Drafting is still running. Wait for it to reach review.")
    prod = production.get_production(str(fm["production_id"])) if fm.get("production_id") else None
    if prod and prod.get("state") in ("publish", "measure"):
        raise HTTPException(status_code=409, detail="This version is approved and locked. Create a new deliverable to change it.")
    if prod and prod.get("state") in ("idea", "brief", "research", "outline"):
        raise HTTPException(status_code=409, detail="There is no draft to render yet.")
    if not _render_module().split_working_notes(body)[0].strip():
        raise HTTPException(status_code=422, detail="The note has no document text to render.")
    try:
        rendered = await asyncio.to_thread(render_version, did, final=False)
    except ImportError as exc:
        raise HTTPException(status_code=503, detail=f"Renderer unavailable on this machine: {exc}")
    if prod:
        production.record_event(prod["production_id"], prod["state"], prod["state"], actor,
                                f"re-rendered v{rendered['version']} from the note")
        if prod.get("state") == "review":
            update_note(did, {"status": "Awaiting approval", "st": "st-warn", "readiness": "Awaiting approval",
                              "gate_status": "pending"})
    audit_log("deliverable.render", {"deliverable_id": did, "version": rendered["version"], "actor": actor})
    return {"ok": True, **{k: rendered[k] for k in ("version", "file")}, "render": rendered["render"],
            "versions": versions(did)}


@router.get("/deliverables/{did}/file/{version}")
async def download_deliverable(did: str, version: int, fmt: str = "docx") -> FileResponse:
    fmt = fmt.lower()
    if fmt not in _MEDIA_TYPES:
        raise HTTPException(status_code=422, detail="fmt must be docx, pptx or pdf")
    path = _files_dir(did) / f"{did}-v{int(version)}.{fmt}"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(path), media_type=_MEDIA_TYPES[fmt], filename=path.name)


@router.post("/deliverables/{did}/action", dependencies=[Depends(require_admin)])
async def deliverable_action(did: str, req: DeliverableAction) -> dict[str, Any]:
    spec = DRAWER_ACTIONS.get(req.action)
    if not spec:
        raise HTTPException(status_code=422, detail=f"action must be one of {tuple(DRAWER_ACTIONS)}")
    _path, fm, body = read_note(did)
    doc_md = _render_module().split_working_notes(body)[0]
    meta = "\n".join(f"{k}: {fm.get(k)}" for k in ("title", "type", "cap", "client", "audience", "status", "evidence")
                     if fm.get(k))
    query = (
        f"Deliverable metadata:\n{meta}\n\nDocument:\n{doc_md[:14000] or '(no document text recorded)'}\n\n"
        "Use only the document and metadata above. Do not invent names, clients, dates, evidence, approvals "
        "or correspondence. If something is not recorded, say so.\n\n" + spec["ask"]
    )
    try:
        answer, cost = await _agent(spec["department"], spec["subagent"], query)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Agent unavailable: {exc}")
    update_note(did, {"cost_usd": _add_cost(fm, cost)},
                body=_append_note(f"{spec['label']} by {spec['department']}/{spec['subagent']}", answer))
    return {"ok": True, "output": answer, "saved": True, "cost_usd": cost}


@router.post("/deliverables/{did}/request-approval", dependencies=[Depends(require_admin)])
async def request_approval(did: str) -> dict[str, Any]:
    _path, fm, _body = read_note(did)
    prod = production.get_production(str(fm.get("production_id") or ""))
    if not prod:
        raise HTTPException(status_code=409, detail="Only generated deliverables go through approval.")
    state = prod.get("state")
    if state in ("publish", "measure"):
        return {"ok": True, "already_approved": True}
    if state != "review":
        raise HTTPException(status_code=409, detail="Still drafting. Approval opens once the quality review is done.")
    update_note(did, {"gate_status": "pending", "status": "Awaiting approval", "readiness": "Awaiting approval",
                      "approval_requested_at": _now()})
    spec = get_type(prod["format"])
    await _notify("Deliverable approval requested",
                  f"{prod.get('title')} ({spec['label']}) is waiting for your approval. Use the Deliverables "
                  "drawer or /pending.")
    return {"ok": True, "pending": governance.pending_gates(prod)}


@router.post("/deliverables/{did}/run", dependencies=[Depends(require_admin)])
async def continue_drafting(did: str, background: BackgroundTasks, req: DeliverableActor | None = None) -> dict[str, Any]:
    _path, fm, _body = read_note(did)
    prod = production.get_production(str(fm.get("production_id") or ""))
    if not prod:
        raise HTTPException(status_code=409, detail="This note is not a generated deliverable.")
    if prod.get("state") not in DRAFTING_STATES:
        raise HTTPException(status_code=409, detail="Drafting is already complete.")
    if did in _RUNNING:
        return {"ok": True, "running": True}
    update_note(did, {"status": "Drafting", "st": "st-warn", "readiness": "Drafting", "last_error": None})
    background.add_task(run_pipeline, did, (req.actor if req else None) or "operator")
    return {"ok": True, "running": True}


@router.post("/deliverables/{did}/archive", dependencies=[Depends(require_admin)])
async def archive_deliverable(did: str, req: DeliverableActor | None = None) -> dict[str, Any]:
    actor = (req.actor if req else None) or "operator"
    path, fm, _body = read_note(did)
    if did in _RUNNING:
        raise HTTPException(status_code=409, detail="Drafting is still running.")
    folder = _require_folder()
    archive = folder / ARCHIVE_DIR
    archive.mkdir(parents=True, exist_ok=True)
    target = archive / path.name
    k = 2
    while target.exists():
        target = archive / f"{path.stem}-{k}.md"
        k += 1
    with _note_lock(did):
        path.replace(target)
    prod = production.get_production(str(fm.get("production_id") or "")) if fm.get("production_id") else None
    if prod and prod.get("state") not in ("publish", "measure", "cancelled"):
        production.transition(prod["production_id"], "cancelled", actor, "archived from Deliverables")
    audit_log("deliverable.archive", {"deliverable_id": did, "to": _vault_rel(target), "actor": actor})
    return {"ok": True, "archived_to": _vault_rel(target)}
