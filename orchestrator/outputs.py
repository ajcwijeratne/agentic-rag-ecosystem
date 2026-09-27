"""
Outputs: documents Apex writes on request, saved as markdown in the vault
=========================================================================

"Draft a content brief from the latest sector intel", said or typed, becomes a
note in `<vault>/13_Command Centre/Outputs/`, written live into the Command
Centre's document pane as it is generated. Ordinary questions are untouched:
only a request to draft, write, build or prepare a named kind of document
(brief, plan, proposal, report, agenda...) opens a document.

The note is plain markdown with a small frontmatter record, so Obsidian opens
it as is, and Syncthing carries it to wijwork. Client documents with an
approval gate stay in Deliverables; an output is a working document.

A draft runs as a background job, not inside the request or the spoken turn
that asked for it. So saying "thanks" while it writes, closing the pane, or a
dropped socket never throws the work away: the job finishes and saves, and any
listener that is still there sees it happen. Only an explicit stop ends it
early, and then what was written so far is saved and marked partial.

  POST /outputs/detect          {"text"} -> is this a document request?
  POST /outputs/draft           {"request", "session_id"?, "source"?} -> start a draft
  GET  /outputs                 recent outputs, newest first
  GET  /outputs/{id}            one output: body, record, vault path, Obsidian link
  GET  /outputs/{id}/stream     server-sent events while it drafts
  PUT  /outputs/{id}            save edits from the pane
  POST /outputs/{id}/stop       stop a running draft, keeping what exists
  GET  /outputs/{id}/download   the .md file
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Awaitable, Callable
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from common.security import require_admin

from . import dashboard as dash

logger = logging.getLogger(__name__)

router = APIRouter(tags=["outputs"])

FOLDER = "Outputs"
OUTPUTS_MODEL_KEY: str = os.getenv("OUTPUTS_MODEL_KEY", "").strip()
OUTPUTS_RETRIEVAL_TIMEOUT_S: float = float(os.getenv("OUTPUTS_RETRIEVAL_TIMEOUT_S", "20"))
OUTPUTS_CONTEXT_CHUNKS: int = int(os.getenv("OUTPUTS_CONTEXT_CHUNKS", "8"))
OUTPUTS_MAX_RUNNING: int = int(os.getenv("OUTPUTS_MAX_RUNNING", "2"))
SAVE_EVERY_S = 2.0          # a crash mid-draft loses at most this much
DELTA_EVERY_S = 0.12        # batch tokens so a long draft is not thousands of frames

# ---------------------------------------------------------------------------
# Is this a document request?
# ---------------------------------------------------------------------------

# Kinds, the words that name them, and a word range to aim for.
KINDS: dict[str, dict[str, Any]] = {
    "brief":     {"words": (600, 1000), "nouns": [r"(?:content |creative |project |client |research |design )?brief(?!ly)", r"briefing(?: note| paper)?"]},
    "proposal":  {"words": (900, 1500), "nouns": [r"proposal", r"pitch", r"business case", r"tender response"]},
    "report":    {"words": (900, 1500), "nouns": [r"report", r"(?:discussion |decision |position |white )paper", r"review of"]},
    "plan":      {"words": (700, 1200), "nouns": [r"(?:\w+ )?plan", r"strategy", r"roadmap", r"framework", r"playbook", r"timeline"]},
    "summary":   {"words": (300, 600),  "nouns": [r"summary", r"synopsis", r"one[- ]pager", r"overview doc(?:ument)?"]},
    "memo":      {"words": (300, 600),  "nouns": [r"memo", r"update for", r"status update"]},
    "agenda":    {"words": (150, 350),  "nouns": [r"agenda", r"run sheet", r"minutes"]},
    "checklist": {"words": (150, 400),  "nouns": [r"checklist", r"shortlist", r"reading list"]},
    "email":     {"words": (120, 300),  "nouns": [r"email", r"e-mail", r"letter", r"newsletter"]},
    "guide":     {"words": (700, 1200), "nouns": [r"guide", r"policy", r"procedure", r"sop", r"handbook", r"faq",
                                                  r"rubric", r"job description", r"position description", r"lesson plan"]},
    "article":   {"words": (700, 1200), "nouns": [r"article", r"blog post", r"op-?ed", r"case study", r"essay",
                                                  r"linkedin post", r"script", r"speech", r"talk"]},
    "outline":   {"words": (300, 700),  "nouns": [r"outline", r"structure for"]},
    "document":  {"words": (500, 900),  "nouns": [r"draft document", r"document", r"doc", r"write-?up"]},
}
DEFAULT_WORDS = (500, 900)

_LEAD = (
    r"^\s*(?:(?:hey|ok|okay|right|so)[,\s]+)?(?:apex[,:]?\s+)?"
    r"(?:(?:please|can you|could you|would you|will you|i need you to|i want you to|i'd like you to|"
    r"i would like you to|let's|lets|go ahead and|help me|now)\s+)*"
)
_VERB = (
    r"(?P<verb>draft|write(?:\s+up)?|build|prepare|create|make|produce|compose|generate|develop|"
    r"put together|pull together|draw up|sketch(?:\s+out)?|outline|turn\s+(?:this|that|it)\s+into)"
)
_KIND_RE = [(kind, re.compile(r"\b(?:" + "|".join(spec["nouns"]) + r")\b", re.I)) for kind, spec in KINDS.items()]
_REQUEST_RE = re.compile(_LEAD + _VERB + r"\b(?P<rest>.*)$", re.I | re.S)
_NOT_DOCS = re.compile(r"\b(?:note to self|reminder|task|ticket|calendar|meeting invite|workflow|automation|"
                       r"folder|playlist|appointment|booking)\b", re.I)
_NON_DOC_HEADS = {"note", "notes", "task", "tasks", "reminder", "call", "booking", "appointment", "copy",
                  "backup", "change", "list"}
_ARTICLE = re.compile(r"^(?:(?:me|us|him|her|them)\s+)?(?:up\s+)?(?:a|an|the|some|my|our|quick|short|new)\s+", re.I)


@dataclass
class DocRequest:
    kind: str
    title: str
    request: str
    words: tuple[int, int]

    def public(self) -> dict:
        return {"document": True, "kind": self.kind, "title": self.title,
                "words": list(self.words), "request": self.request}


def detect(text: str) -> DocRequest | None:
    """
    A document request, or None for anything else.

    Deliberately narrow: it needs a making verb at the front ("draft", "write
    up", "put together") and a named kind of document within the next few
    words. "Summarise this week's signals" or "what should the brief say?"
    stay ordinary answers. A false positive opens a pane nobody wanted; a
    false negative only means the answer arrives as chat.
    """
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s or len(s) > 600:
        return None
    m = _REQUEST_RE.match(s)
    if not m:
        return None
    rest = m.group("rest").strip()
    head = rest[:90]
    if _NOT_DOCS.search(head.split(" about ")[0]):
        return None
    # "make a note to...", "create a task for...": the thing made is not a document.
    first = title_from(rest).split(" ", 1)[0].lower().strip(",.")
    if first in _NON_DOC_HEADS:
        return None
    found: tuple[int, str] | None = None
    for kind, rx in _KIND_RE:
        km = rx.search(head)
        if km and (found is None or km.start() < found[0]):
            found = (km.start(), kind)
    if found is None:
        return None
    kind = found[1]
    return DocRequest(kind=kind, title=title_from(rest), request=s, words=KINDS[kind]["words"])


def title_from(rest: str) -> str:
    """"a content brief from the latest sector intel." -> "Content brief from the latest sector intel"."""
    t = rest.strip().strip(" .!?\"'")
    for _ in range(2):
        t = _ARTICLE.sub("", t)
    t = re.sub(r"\s+(?:please|thanks|thank you)$", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" ,;:-")
    if len(t) > 72:
        cut = t[:72].rsplit(" ", 1)[0]
        t = cut.rstrip(" ,;:-")
    return (t[:1].upper() + t[1:]) if t else "Untitled output"


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

_BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')
_ID_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [^\\/:*?\"<>|#^\[\]\x00-\x1f]{1,100}$")


def folder(create: bool = False) -> Path | None:
    base = dash._base()
    if not base:
        return None
    f = base / FOLDER
    if create:
        f.mkdir(parents=True, exist_ok=True)
    return f


def _require_folder(create: bool = False) -> Path:
    f = folder(create)
    if f is None:
        raise HTTPException(status_code=503, detail="OBSIDIAN_VAULT_PATH is not configured")
    return f


def check_id(oid: str) -> str:
    if not oid or not _ID_RE.match(oid) or ".." in oid:
        raise HTTPException(status_code=400, detail="invalid output id")
    return oid


def path_for(oid: str) -> Path:
    f = _require_folder()
    p = f / f"{check_id(oid)}.md"
    if p.resolve().parent != f.resolve():
        raise HTTPException(status_code=400, detail="invalid output id")
    return p


def new_id(title: str, when: datetime | None = None) -> str:
    """A file stem that does not exist yet: "2026-09-27 Content brief" or "... (2)"."""
    when = when or datetime.now()
    clean = _BAD_CHARS.sub(" ", title)
    clean = re.sub(r"\s+", " ", clean).strip(" .") or "Untitled output"
    stem = f"{when:%Y-%m-%d} {clean[:80].strip(' .')}"
    f = _require_folder(create=True)
    oid, n = stem, 2
    while (f / f"{oid}.md").exists() or oid in _JOBS:
        oid = f"{stem} ({n})"
        n += 1
    return oid


def vault_rel(p: Path) -> str:
    vault = dash._vault_root()
    try:
        return p.resolve().relative_to(vault.resolve()).as_posix() if vault else p.name
    except ValueError:
        return p.name


def obsidian_uri(p: Path) -> str:
    vault = dash._vault_root()
    if not vault:
        return ""
    rel = vault_rel(p)
    rel = rel[:-3] if rel.endswith(".md") else rel
    return f"obsidian://open?vault={quote(vault.name)}&file={quote(rel)}"


def word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.-]*", text or ""))


def write(oid: str, fm: dict[str, Any], body: str) -> Path:
    """Write the note atomically: Obsidian and Syncthing never see half a file."""
    p = path_for(oid)
    p.parent.mkdir(parents=True, exist_ok=True)
    fm = {k: v for k, v in fm.items() if v not in (None, "")}
    fm["updated"] = datetime.now().isoformat(timespec="seconds")
    fm["words"] = word_count(body)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(dash._frontmatter_block(fm) + "\n" + body.strip("\n") + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def read(oid: str) -> tuple[Path, dict[str, Any], str]:
    p = path_for(oid)
    if not p.exists():
        raise HTTPException(status_code=404, detail="output not found")
    text = p.read_text(encoding="utf-8")
    return p, dash._parse_frontmatter(text), dash._strip_frontmatter(text)


def record(oid: str, p: Path, fm: dict[str, Any], body: str | None = None) -> dict[str, Any]:
    out = {
        "id": oid,
        "title": fm.get("title") or oid[11:],
        "kind": fm.get("kind") or "",
        "status": fm.get("status") or "draft",
        "created": fm.get("created") or "",
        "updated": fm.get("updated") or "",
        "words": fm.get("words") or 0,
        "source": fm.get("source") or "",
        "request": fm.get("request") or "",
        "model": fm.get("model") or "",
        "cost_usd": fm.get("cost_usd") or 0.0,
        "path": vault_rel(p),
        "file": p.name,
        "obsidian_uri": obsidian_uri(p),
    }
    if body is not None:
        out["content"] = body
    return out


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------

DOC_RULES = (
    "You are writing a document for Aaron, not replying in a chat.\n"
    "- Output only the document, in GitHub-flavoured markdown. No preamble, no sign-off, "
    "no offer of further help, no remarks about yourself.\n"
    "- Start with one '# ' title line. Use '## ' section headings, short paragraphs, and lists "
    "or a table where they help the reader.\n"
    "- Aim for {lo} to {hi} words.\n"
    "- Write in Australian English. Short sentences, active voice, plain words.\n"
    "- Do not use em dashes. Use commas, full stops or semicolons.\n"
    "- Use only facts from the request, the conversation and the retrieved context. Never invent "
    "figures, names, dates, quotes or results. Where a needed fact is missing, write "
    "[To confirm: what is missing].\n"
    "- Never claim anything was sent, published, scheduled or agreed.\n"
    "- Do not use these words: elevate, disrupt, revolutionise, foster, reimagine, transform, "
    "leverage, unlock, empower, innovate, holistic, seamless, dynamic, agile, ecosystem, "
    "game-changer, cutting-edge."
)

KIND_LABEL = {"email": "email", "guide": "guide", "article": "piece", "outline": "outline"}


def spoken_start(req: DocRequest) -> str:
    what = KIND_LABEL.get(req.kind, req.kind)
    return f"On it. I'm drafting the {what} now; it's opening beside me."


def spoken_done(kind: str, words: int) -> str:
    what = KIND_LABEL.get(kind, kind)
    about = f"about {round(words, -1) if words >= 100 else words} words"
    return f"The {what} is ready, {about}. It's saved in Outputs."


async def _retrieve(query: str) -> list:
    """Context for the draft. Seam for tests; failures mean no context, not no draft."""
    try:
        from .graph import rag_node

        state: dict[str, Any] = {
            "messages": [], "query": query, "routing": None, "context_chunks": [],
            "output_payload": {}, "agents_used": [], "errors": [], "finished": False,
            "agent_timeout_s": OUTPUTS_RETRIEVAL_TIMEOUT_S,
        }
        got = await rag_node(state)  # type: ignore[arg-type]
        return (got.get("context_chunks", []) or [])[:OUTPUTS_CONTEXT_CHUNKS]
    except Exception as exc:  # noqa: BLE001
        logger.info("outputs: retrieval skipped: %s", exc)
        return []


def _system_prompt(req: DocRequest, department: str, context: list) -> str:
    rules = DOC_RULES.format(lo=req.words[0], hi=req.words[1])
    try:
        from .wijerco_agent import _build_system_prompt

        return _build_system_prompt(department, context) + "\n\n---\n\n" + rules
    except Exception:  # noqa: BLE001
        return rules


def _history(session_id: str) -> list[dict]:
    if not session_id:
        return []
    try:
        from .session_store import get_history_for_llm

        return get_history_for_llm(session_id, max_turns=8)
    except Exception:  # noqa: BLE001
        return []


def _stream(user_message: str, system: str, history: list[dict]):
    """Model tokens. Seam for tests."""
    from .fallback_chain import stream_with_fallback

    return stream_with_fallback(
        user_message=user_message, system_prompt=system, history=history,
        force_model_key=OUTPUTS_MODEL_KEY or None,
    )


def _department(text: str) -> str:
    try:
        from .wijerco_router import classify_intent

        return classify_intent(text).department or "research_intelligence"
    except Exception:  # noqa: BLE001
        return "research_intelligence"


def _log_turn(session_id: str, request: str, title: str, body: str, model: str, cost: float) -> None:
    """Put the draft in the conversation, so "make it shorter" has something to refer to."""
    if not session_id:
        return
    try:
        from .session_store import add_message

        add_message(session_id, "user", request, cost_usd=0.0)
        add_message(session_id, "assistant", f"(Drafted \"{title}\" in Outputs.)\n\n{body[:6000]}",
                    model_key=model, cost_usd=cost)
    except Exception:  # noqa: BLE001
        pass


@dataclass
class DraftJob:
    id: str
    req: DocRequest
    source: str
    session_id: str
    created: str
    content: str = ""
    status: str = "drafting"         # drafting | draft | partial | error
    error: str = ""
    model: str = ""
    cost_usd: float = 0.0
    department: str = ""
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float = 0.0
    task: asyncio.Task | None = None
    listeners: list[asyncio.Queue] = field(default_factory=list)
    stop_requested: bool = False

    @property
    def running(self) -> bool:
        return self.status == "drafting"

    def frontmatter(self) -> dict[str, Any]:
        return {
            "title": self.req.title, "type": "output", "kind": self.req.kind,
            "status": self.status, "created": self.created, "source": self.source,
            "request": self.req.request, "department": self.department,
            "model": self.model, "cost_usd": round(self.cost_usd, 5) if self.cost_usd else "",
        }

    def save(self) -> Path:
        return write(self.id, self.frontmatter(), self.content)

    def start_event(self) -> dict[str, Any]:
        p = path_for(self.id)
        return {"type": "doc_start", **record(self.id, p, self.frontmatter()),
                "status": self.status, "spoken": spoken_start(self.req)}

    def emit(self, event: dict[str, Any]) -> None:
        for q in list(self.listeners):
            try:
                q.put_nowait(event)
            except Exception:  # noqa: BLE001
                pass

    def subscribe(self) -> asyncio.Queue:
        """A queue that replays the draft so far, then follows it live."""
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(self.start_event())
        if self.content:
            q.put_nowait({"type": "doc_delta", "id": self.id, "text": self.content, "replay": True})
        if not self.running:
            q.put_nowait(self.end_event())
        else:
            self.listeners.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.listeners:
            self.listeners.remove(q)

    def end_event(self) -> dict[str, Any]:
        words = word_count(self.content)
        base = {"id": self.id, "words": words, "status": self.status, "model": self.model,
                "cost_usd": round(self.cost_usd, 5), "title": self.req.title, "kind": self.req.kind}
        if self.status == "error":
            return {"type": "doc_error", **base, "message": self.error or "The draft failed.",
                    "spoken": "I couldn't finish that draft. What I had is saved in Outputs." if self.content
                    else "I couldn't draft that one. The model may be offline."}
        if self.status == "partial":
            return {"type": "doc_stopped", **base, "spoken": "Stopped. What I'd written is saved in Outputs."}
        return {"type": "doc_done", **base, "spoken": spoken_done(self.req.kind, words)}

    async def run(self) -> None:
        last_save = last_delta = time.monotonic()
        pending = ""
        try:
            self.department = _department(self.req.request)
            context = await _retrieve(self.req.request)
            system = _system_prompt(self.req, self.department, context)
            async for event in _stream(self.req.request, system, _history(self.session_id)):
                token = event.get("token") or ""
                if token and not event.get("done"):
                    self.content += token
                    pending += token
                if event.get("done"):
                    self.model = event.get("model_key", "") or self.model
                    self.cost_usd = float(event.get("cost_usd", 0.0) or 0.0)
                t = time.monotonic()
                if pending and t - last_delta >= DELTA_EVERY_S:
                    self.emit({"type": "doc_delta", "id": self.id, "text": pending})
                    pending, last_delta = "", t
                if t - last_save >= SAVE_EVERY_S:
                    self.save()
                    last_save = t
            if pending:
                self.emit({"type": "doc_delta", "id": self.id, "text": pending})
                pending = ""
            self.content = tidy(self.content)
            if not self.content.strip():
                raise RuntimeError("The model returned nothing.")
            self.status = "draft"
        except asyncio.CancelledError:
            if pending:
                self.emit({"type": "doc_delta", "id": self.id, "text": pending})
            self.status = "partial"
        except Exception as exc:  # noqa: BLE001
            logger.warning("outputs: draft %s failed: %s", self.id, exc)
            self.status, self.error = "error", str(exc)[:300]
        finally:
            self.finished_at = time.monotonic()
            try:
                self.save()
            except Exception as exc:  # noqa: BLE001
                logger.error("outputs: could not save %s: %s", self.id, exc)
            if self.status == "draft":
                _log_turn(self.session_id, self.req.request, self.req.title, self.content, self.model, self.cost_usd)
            self.emit({"type": "doc_saved", "id": self.id, "text": self.content})
            self.emit(self.end_event())
            self.listeners.clear()


def tidy(text: str) -> str:
    """Strip what models add around a document despite being told not to."""
    t = (text or "").strip()
    t = re.sub(r"^```(?:markdown|md)?\s*\n(.*?)\n```\s*$", r"\1", t, flags=re.S)
    lines = t.splitlines()
    # A chatty first line before the title: "Here's the brief:".
    if len(lines) > 2 and not lines[0].lstrip().startswith("#") and lines[1].strip() == "" \
            and any(l.lstrip().startswith("# ") for l in lines[2:6]) and len(lines[0]) < 120:
        lines = lines[2:]
    return "\n".join(lines).replace(" — ", ", ").replace("—", ", ").strip() + "\n"


_JOBS: dict[str, DraftJob] = {}
_JOB_TTL_S = 900


def _prune() -> None:
    now = time.monotonic()
    for oid, job in list(_JOBS.items()):
        if not job.running and job.finished_at and now - job.finished_at > _JOB_TTL_S:
            _JOBS.pop(oid, None)


def running_count() -> int:
    return sum(1 for j in _JOBS.values() if j.running)


def start(req: DocRequest, source: str = "chat", session_id: str = "") -> DraftJob:
    """Create the note and start drafting into it in the background."""
    _prune()
    if running_count() >= OUTPUTS_MAX_RUNNING:
        raise HTTPException(status_code=429, detail="Already drafting; wait for one to finish or stop it.")
    oid = new_id(req.title)
    job = DraftJob(id=oid, req=req, source=source, session_id=session_id,
                   created=datetime.now().isoformat(timespec="seconds"))
    _JOBS[oid] = job
    job.save()                                   # the file exists before the first token
    job.task = asyncio.create_task(job.run())
    return job


def stop(oid: str) -> bool:
    job = _JOBS.get(oid)
    if not job or not job.running or not job.task:
        return False
    job.stop_requested = True
    job.task.cancel()
    return True


async def follow(job: DraftJob, send: Callable[[dict], Awaitable[None]]) -> None:
    """Relay one job's events to a socket until it ends or the socket fails."""
    q = job.subscribe()
    try:
        while True:
            event = await q.get()
            if event.get("type") == "doc_saved":
                continue
            try:
                await send(event)
            except Exception:  # noqa: BLE001 — the job carries on without us
                return
            if event.get("type") in ("doc_done", "doc_stopped", "doc_error"):
                return
    finally:
        job.unsubscribe(q)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

class DetectBody(BaseModel):
    text: str = Field(..., max_length=4000)


class DraftBody(BaseModel):
    request: str = Field(..., min_length=3, max_length=4000)
    session_id: str = ""
    source: str = "chat"
    title: str = ""
    kind: str = ""


class SaveBody(BaseModel):
    content: str = Field(..., max_length=400_000)
    title: str = ""


@router.post("/outputs/detect")
def outputs_detect(body: DetectBody) -> dict:
    req = detect(body.text)
    return req.public() if req else {"document": False}


@router.post("/outputs/draft", dependencies=[Depends(require_admin)])
async def outputs_draft(body: DraftBody) -> dict:
    req = detect(body.request)
    if req is None:
        # Asked from the pane directly ("New document"): honour it anyway.
        kind = body.kind if body.kind in KINDS else "outline"
        req = DocRequest(kind=kind, title=title_from(body.request), request=body.request.strip(),
                         words=KINDS.get(kind, {}).get("words", DEFAULT_WORDS))
    if body.title.strip():
        req.title = title_from(body.title)
    if body.kind in KINDS:
        req.kind, req.words = body.kind, KINDS[body.kind]["words"]
    job = start(req, source=(body.source or "chat")[:20], session_id=body.session_id)
    return job.start_event()


@router.get("/outputs")
def outputs_list(limit: int = 30) -> dict:
    f = folder()
    if f is None:
        return {"items": [], "error": "OBSIDIAN_VAULT_PATH is not configured"}
    if not f.exists():
        return {"items": [], "folder": vault_rel(f)}
    files = sorted((p for p in f.glob("*.md") if _ID_RE.match(p.stem)),
                   key=lambda p: p.stat().st_mtime, reverse=True)[: max(1, min(limit, 200))]
    items = []
    for p in files:
        try:
            fm = dash._parse_frontmatter(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        rec = record(p.stem, p, fm)
        job = _JOBS.get(p.stem)
        if job and job.running:
            rec["status"] = "drafting"
        items.append(rec)
    return {"items": items, "folder": vault_rel(f)}


@router.get("/outputs/{oid}")
def outputs_get(oid: str) -> dict:
    job = _JOBS.get(check_id(oid))
    if job and job.running:
        return {**record(oid, path_for(oid), job.frontmatter(), job.content), "status": "drafting"}
    p, fm, body = read(oid)
    return record(oid, p, fm, body)


@router.get("/outputs/{oid}/stream")
async def outputs_stream(oid: str):
    job = _JOBS.get(check_id(oid))

    async def events() -> AsyncGenerator[str, None]:
        if job is None:
            p, fm, body = read(oid)
            yield "data: " + json.dumps({"type": "doc_start", **record(oid, p, fm)}) + "\n\n"
            yield "data: " + json.dumps({"type": "doc_delta", "id": oid, "text": body, "replay": True}) + "\n\n"
            yield "data: " + json.dumps({"type": "doc_done", "id": oid, "status": fm.get("status") or "draft",
                                         "words": word_count(body)}) + "\n\n"
            return
        q = job.subscribe()
        try:
            while True:
                event = await q.get()
                if event.get("type") == "doc_saved":
                    continue
                yield "data: " + json.dumps(event) + "\n\n"
                if event.get("type") in ("doc_done", "doc_stopped", "doc_error"):
                    return
        finally:
            job.unsubscribe(q)

    if job is None:
        read(oid)                                    # 404 before the stream starts
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.put("/outputs/{oid}", dependencies=[Depends(require_admin)])
def outputs_save(oid: str, body: SaveBody) -> dict:
    job = _JOBS.get(check_id(oid))
    if job and job.running:
        raise HTTPException(status_code=409, detail="Still drafting; stop it or wait before editing.")
    p, fm, _old = read(oid)
    if body.title.strip():
        fm["title"] = title_from(body.title)
    if fm.get("status") in ("draft", "partial", "error", None, ""):
        fm["status"] = "edited"
    p = write(oid, fm, body.content)
    _, fm2, body2 = read(oid)
    return record(oid, p, fm2, None) | {"saved": True}


@router.post("/outputs/{oid}/stop", dependencies=[Depends(require_admin)])
def outputs_stop(oid: str) -> dict:
    return {"stopped": stop(check_id(oid))}


@router.get("/outputs/{oid}/download")
def outputs_download(oid: str) -> Response:
    p, _fm, _body = read(oid)
    data = p.read_bytes()
    name = p.name.encode("ascii", "ignore").decode() or "output.md"
    return Response(content=data, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=\"{name}\"; filename*=UTF-8''{quote(p.name)}"})
