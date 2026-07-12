"""
FastAPI entry point for the Agentic RAG Orchestrator.

Endpoints:
  POST /query          — run the full LangGraph pipeline
  POST /webhook        — n8n webhook trigger
  GET  /health         — liveness check
  GET  /routing-test   — dry-run the router without LLM
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, AsyncGenerator

from dotenv import load_dotenv
import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

load_dotenv()

from common.security import require_api_key, require_admin, cors_kwargs, bind_host
from common.rbac import require_role, role_for_request

from .graph import graph
from .router import route_query
from .state import AgentState
from .wijerco_router import classify_intent, select_subagent, DEPT_META
from .wijerco_agent import call_wijerco_agent
from .cost_tracker import tracker as cost_tracker
from .token_optimizer import pick_model, classify_task, rough_token_count
from .llm_registry import available_models, MODELS
from .fallback_chain import call_with_fallback, stream_with_fallback
from .session_store import (
    create_session, get_session, list_sessions, delete_session,
    add_message, get_messages, get_history_for_llm, session_cost,
)
from . import uploads as uploads_mod
from .prompt_assistant import improve_prompt
from .wijerco_roster import get_roster, lookup_subagent
from .dashboard import router as dashboard_router, create_deliverable_from_production
from media import registry as media_registry
from media import ingest as media_ingest
from media import tool_registry as media_tools
from media.generate import generate_dict as generate_media
from rag.media_search import media_search as media_search_fn
from . import production as production_store
from . import production_media as production_media_store
from . import governance as governance_store
from . import operating as operating_store
from . import deployment as deployment_store
from . import obsidian_projects as obsidian_projects_store

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Agentic RAG Orchestrator",
    version="1.0.0",
    description="Central command for the Agentic Omni-Channel Ecosystem",
    dependencies=[Depends(require_api_key)],
)

app.add_middleware(CORSMiddleware, **cors_kwargs())

# Command Centre dashboard pages (deliverables, content, engagements, KB,
# memory overview, sector intel, routing inspector, scheduled runs)
app.include_router(dashboard_router)

# Inbox front door (all channels) + operating daemon controls
from .inbox import router as inbox_router
app.include_router(inbox_router)

# ---------------------------------------------------------------------------
# Command Centre UI (installable PWA)
# Served same-origin as the API so the installed app and its fetch() calls both
# live on http://localhost:8000 — no CORS, valid secure context for the service
# worker, and a stable start_url for the installed shortcut.
#   Page     -> http://localhost:8000/app/command_centre.html
#   Manifest -> http://localhost:8000/app/manifest.webmanifest
#   Worker   -> http://localhost:8000/app/sw.js   (scope /app/)
# ---------------------------------------------------------------------------
import mimetypes as _mimetypes
from pathlib import Path as _Path
from fastapi.staticfiles import StaticFiles as _StaticFiles
from fastapi.responses import RedirectResponse as _RedirectResponse

_mimetypes.add_type("application/manifest+json", ".webmanifest")
_UI_DIR = _Path(__file__).resolve().parent.parent / "ui"
if _UI_DIR.is_dir():
    app.mount("/app", _StaticFiles(directory=str(_UI_DIR), html=True), name="command-centre")

    @app.get("/", include_in_schema=False)
    def _serve_root():
        return _RedirectResponse(url="/app/command_centre.html")

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=8192)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    force_model_key: str | None = None
    max_tier: int = 3


class QueryResponse(BaseModel):
    session_id:    str
    request_id:    str = ""
    final_confidence: float = 0.0
    query:         str
    answer:        str
    model_key:     str
    model_label:   str
    provider:      str
    task_type:     str
    cost_usd:      float
    latency_ms:    int
    input_tokens:  int
    output_tokens: int
    agents_used:   list[str]
    context_count: int
    sources:       list[dict] = []
    citations:     list[dict] = []
    assembly_stats: dict      = {}
    errors:        list[str]


class RestoreRequest(BaseModel):
    path: str = Field(..., min_length=1)
    dry_run: bool = True


class ReleaseSnapshotRequest(BaseModel):
    note: str = ""


class ReleaseRollbackRequest(BaseModel):
    path: str = Field(..., min_length=1)
    dry_run: bool = True


class WebhookPayload(BaseModel):
    event: str
    data: dict[str, Any] = {}


class EvalRunRequest(BaseModel):
    suite: str = "routing"                 # routing | answer_quality | all
    live: bool = False                     # live answer evals call models
    departments: list[str] | None = None
    limit: int | None = None
    max_tier: int = 1


class EvalCaseStateRequest(BaseModel):
    status: str | None = None              # new | triaged | fixed | verified
    note: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "agentic-rag-orchestrator"}


def _orchestrator_dependencies() -> list[dict]:
    from common.health import qdrant_check, ollama_check, agent_check
    return [
        qdrant_check(),
        ollama_check(),
        agent_check("local_data", int(os.getenv("LOCAL_DATA_AGENT_PORT", "8001"))),
        agent_check("search",     int(os.getenv("SEARCH_AGENT_PORT", "8002"))),
        agent_check("cloud",      int(os.getenv("CLOUD_AGENT_PORT", "8003"))),
    ]


@app.get("/health/deep")
async def health_deep():
    """Probe Qdrant, Ollama, and the three sub-agents in parallel."""
    from common.health import deep_health
    return await deep_health(_orchestrator_dependencies(), service="orchestrator")


@app.on_event("startup")
async def _startup_checks():
    from common.startup import require_dependencies
    await require_dependencies(_orchestrator_dependencies(), service="orchestrator")


@app.get("/health/qdrant")
async def health_qdrant():
    """Proxy Qdrant's health check so the browser UI can read it (Qdrant sends no CORS headers)."""
    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{qdrant_url}/healthz", timeout=3.0)
        if resp.status_code == 200:
            return {"status": "ok", "service": "qdrant"}
    except Exception:
        pass
    raise HTTPException(status_code=503, detail="qdrant unavailable")


@app.get("/ops/me")
async def ops_me(request: Request):
    return {"role": role_for_request(request)}


@app.get("/ops/status", dependencies=[Depends(require_role("viewer"))])
async def ops_status():
    return deployment_store.status()


@app.post("/ops/migrate", dependencies=[Depends(require_role("admin"))])
async def ops_migrate():
    return deployment_store.migrate()


@app.post("/ops/backup", dependencies=[Depends(require_role("admin"))])
async def ops_backup():
    return deployment_store.backup_database()


@app.post("/ops/restore", dependencies=[Depends(require_role("admin"))])
async def ops_restore(req: RestoreRequest):
    try:
        return deployment_store.restore_database(req.path, dry_run=req.dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/ops/backups", dependencies=[Depends(require_role("operator"))])
async def ops_backups(limit: int = 20):
    return {"items": deployment_store.list_backups(limit=limit)}


@app.get("/ops/releases", dependencies=[Depends(require_role("viewer"))])
async def ops_releases():
    return deployment_store.releases()


@app.post("/ops/releases/snapshot", dependencies=[Depends(require_role("admin"))])
async def ops_release_snapshot(req: ReleaseSnapshotRequest):
    return deployment_store.snapshot_release(note=req.note)


@app.post("/ops/releases/rollback", dependencies=[Depends(require_role("admin"))])
async def ops_release_rollback(req: ReleaseRollbackRequest):
    try:
        return deployment_store.rollback_release(req.path, dry_run=req.dry_run)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/ops/monitoring", dependencies=[Depends(require_role("operator"))])
async def ops_monitoring():
    return deployment_store.monitoring_summary()


@app.get("/ops/rehearsal", dependencies=[Depends(require_role("operator"))])
async def ops_rehearsal():
    return deployment_store.operational_rehearsal()


@app.post("/routing-test")
async def routing_test(req: QueryRequest):
    """Dry-run the router to see which model would be selected."""
    initial_state: AgentState = {
        "messages": [],
        "query": req.query,
        "routing": None,
        "context_chunks": [],
        "output_payload": {},
        "agents_used": [],
        "errors": [],
        "finished": False,
    }
    state_after = route_query(initial_state)
    return state_after["routing"].model_dump()


@app.post("/query", response_model=QueryResponse)
async def run_query(req: QueryRequest):
    """
    Main endpoint. Runs the full LangGraph pipeline:
      route → RAG retrieval → LLM → synthesise → return JSON
    """
    initial_state: AgentState = {
        "messages": [],
        "query": req.query,
        "routing": None,
        "context_chunks": [],
        "output_payload": {},
        "agents_used": [],
        "errors": [],
        "finished": False,
    }

    try:
        final_state = await graph.ainvoke(initial_state)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    p = final_state.get("output_payload", {})

    return QueryResponse(
        session_id    = req.session_id,
        request_id    = p.get("request_id",     ""),
        final_confidence = p.get("final_confidence", 0.0),
        query         = p.get("query",         req.query),
        answer        = p.get("answer",         ""),
        model_key     = p.get("model_key",      "unknown"),
        model_label   = p.get("model_label",    "unknown"),
        provider      = p.get("provider",       "unknown"),
        task_type     = p.get("task_type",      "unknown"),
        cost_usd      = p.get("cost_usd",       0.0),
        latency_ms    = p.get("latency_ms",     0),
        input_tokens  = p.get("input_tokens",   0),
        output_tokens = p.get("output_tokens",  0),
        agents_used   = p.get("agents_used",    []),
        context_count = p.get("context_count",  0),
        sources       = p.get("sources",        []),
        citations     = p.get("citations",      []),
        assembly_stats= p.get("assembly_stats", {}),
        errors        = p.get("errors",         []),
    )


@app.get("/cost")
async def get_cost():
    """Return session cost summary — used by the command centre dashboard."""
    return cost_tracker.session_summary()


@app.get("/traces")
async def get_traces(limit: int = 50):
    """Recent per-request traces (route, agents, latency, fallbacks, confidence)."""
    from .trace import read_traces
    return {"traces": read_traces(limit)}


@app.get("/routing-decisions")
async def get_routing_decisions(limit: int = 200):
    """Recent routing decisions, for tuning the classifier thresholds."""
    from .decision_log import read_decisions
    return {"decisions": read_decisions(limit)}


@app.get("/quality/overview")
async def quality_overview(trace_limit: int = 200, eval_limit: int = 10):
    """Phase 1 quality cockpit: traces, eval history, and next actions."""
    from .quality import overview
    return overview(trace_limit=trace_limit, eval_limit=eval_limit)


@app.post("/evals/run", dependencies=[Depends(require_admin)])
async def run_evals(req: EvalRunRequest):
    """Run a persisted eval suite. Offline routing evals are free; live answer
    evals are opt-in because they call models and may incur cost."""
    from .eval_runner import run_eval
    return await run_eval(
        suite=req.suite,
        live=req.live,
        departments=req.departments,
        limit=req.limit,
        max_tier=req.max_tier,
    )


@app.get("/evals/runs")
async def eval_runs(limit: int = 20):
    from .eval_store import list_runs
    return {"runs": list_runs(limit)}


@app.get("/evals/runs/{run_id}")
async def eval_run_detail(run_id: str):
    from .eval_store import get_run
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="eval run not found")
    return run


@app.get("/evals/cases")
async def eval_case_states(status: str | None = None, limit: int = 200):
    from .eval_store import list_case_states
    return {"cases": list_case_states(status=status, limit=limit)}


@app.get("/evals/work-queue")
async def eval_work_queue(status: str | None = None, limit: int = 200, include_verified: bool = False):
    from .eval_store import list_case_work_items
    return {"items": list_case_work_items(status=status, limit=limit, include_verified=include_verified)}


@app.patch("/evals/cases/{suite}/{case_id}", dependencies=[Depends(require_admin)])
async def update_eval_case_state(suite: str, case_id: str, req: EvalCaseStateRequest):
    from .eval_store import update_case_state
    try:
        return update_case_state(suite, case_id, status=req.status, note=req.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/evals/cases/{suite}/{case_id}/verify", dependencies=[Depends(require_admin)])
async def verify_eval_case(suite: str, case_id: str, live: bool = False, max_tier: int = 1):
    from .eval_runner import verify_case
    try:
        return await verify_case(suite, case_id, live=live, max_tier=max_tier)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/usage/monthly")
async def usage_monthly(months: int = 12):
    """Monthly usage rollup from the persistent ledger — powers the Admin tab."""
    return cost_tracker.monthly_summary(months=months)


# ---------------------------------------------------------------------------
# File upload
# ---------------------------------------------------------------------------

@app.post("/upload", dependencies=[Depends(require_admin)])
async def upload_file(
    file:       UploadFile = File(...),
    mode:       str = Form("chat"),          # "chat" | "kb"
    session_id: str = Form(""),
):
    """
    Upload a file. mode='chat' attaches it to the current conversation only;
    mode='kb' indexes it permanently into the uploaded_docs collection.
    """
    data = await file.read()
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File exceeds 25 MB limit")

    text = uploads_mod.extract_text(file.filename, data)
    if not text.strip():
        raise HTTPException(status_code=422, detail="Could not extract any text from this file")

    if mode == "kb":
        result = await uploads_mod.index_to_kb(file.filename, text)
        return {"mode": "kb", "file": file.filename, **result}

    # chat mode
    uploads_mod.add_chat_context(session_id or "default", file.filename, text)
    return {
        "mode":       "chat",
        "file":       file.filename,
        "chars":      len(text),
        "session_id": session_id,
        "attached":   uploads_mod.list_chat_context(session_id or "default"),
    }


@app.delete("/upload/{session_id}", dependencies=[Depends(require_admin)])
async def clear_uploads(session_id: str):
    n = uploads_mod.clear_chat_context(session_id)
    return {"status": "cleared", "removed": n}


# ---------------------------------------------------------------------------
# Media Asset Registry  (multimodal expansion, Phase 1)
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    path:     str = Field(..., min_length=1, description="local path under a media root, or a URL")
    project:  str | None = None
    rights:   str = "unknown"
    source:   str | None = None
    language: str | None = None


class AssetPatch(BaseModel):
    rights:     str | None = None
    status:     str | None = None
    project:    str | None = None
    tags:       list[str] | None = None
    dimensions: str | None = None
    duration:   float | None = None


class AssetCollectionCreate(BaseModel):
    name:    str = Field(..., min_length=1)
    project: str | None = None
    purpose: str | None = None
    status:  str = "draft"
    meta:    dict[str, Any] = Field(default_factory=dict)


class AssetCollectionPatch(BaseModel):
    name:    str | None = None
    project: str | None = None
    purpose: str | None = None
    status:  str | None = None
    meta:    dict[str, Any] | None = None


class AssetCollectionAdd(BaseModel):
    asset_id: str
    role:     str = "reference"


@app.get("/assets")
async def list_assets(
    type: str | None = None,
    project: str | None = None,
    status: str | None = None,
    rights: str | None = None,
    tag: str | None = None,
    q: str | None = None,
    limit: int = 200,
):
    """Filter the asset registry. `q` is free text over tags and transcript text."""
    items = media_registry.list_assets(
        type_=type, project=project, status=status, rights=rights,
        tag=tag, q=q, limit=limit,
    )
    return {"items": items, "stats": media_registry.stats()}


@app.get("/assets/{asset_id}")
async def get_asset(asset_id: str):
    asset = media_registry.get_asset(asset_id)
    if not asset:
        raise HTTPException(status_code=404, detail="asset not found")
    return asset


@app.get("/assets/{asset_id}/moments")
async def list_asset_moments(asset_id: str, kind: str | None = None, q: str | None = None, limit: int = 500):
    if not media_registry.get_asset(asset_id, with_relations=False):
        raise HTTPException(status_code=404, detail="asset not found")
    return {"items": media_registry.list_moments(asset_id, kind=kind, q=q, limit=limit)}


@app.post("/assets/ingest", dependencies=[Depends(require_admin)])
async def ingest_asset(req: IngestRequest, background: BackgroundTasks):
    """
    Register an asset and run its ingestion worker in the background. Returns
    the created row immediately with status 'ingesting'; poll GET /assets/{id}
    for the worker result.
    """
    try:
        asset_type = media_ingest.detect_type(req.path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    background.add_task(
        media_ingest.ingest, req.path,
        project=req.project, rights=req.rights,
        source=req.source, language=req.language,
    )
    return {"status": "ingesting", "type": asset_type, "path": req.path}


@app.patch("/assets/{asset_id}", dependencies=[Depends(require_admin)])
async def patch_asset(asset_id: str, patch: AssetPatch):
    if not media_registry.get_asset(asset_id):
        raise HTTPException(status_code=404, detail="asset not found")
    fields = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        media_registry.update_asset(asset_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return media_registry.get_asset(asset_id)


@app.delete("/assets/{asset_id}", dependencies=[Depends(require_admin)])
async def delete_asset(asset_id: str, hard: bool = False):
    if not media_registry.get_asset(asset_id):
        raise HTTPException(status_code=404, detail="asset not found")
    media_registry.delete_asset(asset_id, hard=hard)
    return {"status": "deleted", "hard": hard, "asset_id": asset_id}


@app.get("/asset-collections")
async def list_asset_collections(project: str | None = None, status: str | None = None, limit: int = 200):
    return {"items": media_registry.list_collections(project=project, status=status, limit=limit)}


@app.post("/asset-collections", dependencies=[Depends(require_admin)])
async def create_asset_collection(req: AssetCollectionCreate):
    try:
        cid = media_registry.create_collection(
            req.name,
            project=req.project,
            purpose=req.purpose,
            status=req.status,
            meta=req.meta,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return media_registry.get_collection(cid)


@app.get("/asset-collections/{collection_id}")
async def get_asset_collection(collection_id: str):
    collection = media_registry.get_collection(collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="collection not found")
    return collection


@app.patch("/asset-collections/{collection_id}", dependencies=[Depends(require_admin)])
async def patch_asset_collection(collection_id: str, patch: AssetCollectionPatch):
    fields = {k: v for k, v in patch.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        ok = media_registry.update_collection(collection_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="collection not found")
    return media_registry.get_collection(collection_id)


@app.delete("/asset-collections/{collection_id}", dependencies=[Depends(require_admin)])
async def archive_asset_collection(collection_id: str):
    if not media_registry.archive_collection(collection_id):
        raise HTTPException(status_code=404, detail="collection not found")
    return media_registry.get_collection(collection_id)


@app.post("/asset-collections/{collection_id}/assets", dependencies=[Depends(require_admin)])
async def add_asset_to_collection(collection_id: str, req: AssetCollectionAdd):
    if not media_registry.add_to_collection(collection_id, req.asset_id, role=req.role):
        raise HTTPException(status_code=404, detail="collection or asset not found")
    return media_registry.get_collection(collection_id)


@app.delete("/asset-collections/{collection_id}/assets/{asset_id}", dependencies=[Depends(require_admin)])
async def remove_asset_from_collection(collection_id: str, asset_id: str):
    if not media_registry.remove_from_collection(collection_id, asset_id):
        raise HTTPException(status_code=404, detail="asset is not in this collection")
    return media_registry.get_collection(collection_id)


# ---------------------------------------------------------------------------
# Multimodal search  (multimodal expansion, Phase 2)
# ---------------------------------------------------------------------------

class MediaSearchRequest(BaseModel):
    query:      str = Field(..., min_length=1)
    modalities: list[str] | None = None       # subset of text|transcript|visual
    filters:    dict[str, Any] | None = None  # project|rights|status|type
    top_k:      int = 5


class MediaToolPatch(BaseModel):
    enabled: bool
    notes:   str | None = None


class MediaGenerateRequest(BaseModel):
    capability:    str
    brief:         dict[str, Any] = Field(default_factory=dict)
    production_id: str | None = None
    tool:          str | None = None
    source_assets: list[str] = Field(default_factory=list)
    rights:        str = "unknown"
    meta:          dict[str, Any] = Field(default_factory=dict)


class ProductionGenerateRequest(BaseModel):
    capability:    str
    brief:         dict[str, Any] = Field(default_factory=dict)
    tool:          str | None = None
    source_assets: list[str] | None = None
    rights:        str = "owned"
    meta:          dict[str, Any] = Field(default_factory=dict)


class ProductionGeneratePlanRequest(BaseModel):
    capabilities: list[str] | None = None
    include_video: bool = False
    max_jobs:      int = 20
    dry_run:       bool = False
    actor:         str = "operator"


@app.post("/media/search")
async def media_search_endpoint(req: MediaSearchRequest):
    """Search media assets across text, transcript, and visual indexes. Each hit
    is joined to its registry record; filters scope by project, rights, status,
    and type."""
    return await media_search_fn(
        req.query,
        modalities=req.modalities,
        filters=req.filters,
        top_k=req.top_k,
    )


@app.get("/media/tools")
async def list_media_tools(capability: str | None = None, enabled_only: bool = False):
    """List local multimedia tools and their configured availability."""
    return {"items": media_tools.list_tools(capability=capability, enabled_only=enabled_only)}


@app.patch("/media/tools/{tool_name}", dependencies=[Depends(require_admin)])
async def patch_media_tool(tool_name: str, patch: MediaToolPatch):
    ok = media_tools.set_tool_enabled(tool_name, patch.enabled, notes=patch.notes)
    if not ok:
        raise HTTPException(status_code=404, detail="media tool not found")
    return media_tools.get_tool(tool_name)


@app.post("/media/generate", dependencies=[Depends(require_admin)])
async def generate_media_endpoint(req: MediaGenerateRequest):
    """Run one local multimedia generation job."""
    try:
        result = generate_media(req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return result


# ---------------------------------------------------------------------------
# Production pipeline and governance  (integrated roadmap, Phases 3 and 4)
# ---------------------------------------------------------------------------

class ProductionCreateRequest(BaseModel):
    title:   str = Field(..., min_length=1)
    project: str | None = None
    format:  str
    owner:   str | None = None


class ProductionTransitionRequest(BaseModel):
    to_state: str
    note:     str = ""
    actor:    str = "operator"


class ProductionActionRequest(BaseModel):
    action: str
    actor:  str = "operator"


class ProductionHandoffRequest(BaseModel):
    actor: str = "operator"
    note:  str = ""


class GovernanceApproveRequest(BaseModel):
    gate:      str
    target_id: str
    note:      str = ""
    actor:     str = "operator"
    status:    str = "approved"


class OperatingPlanCreateRequest(BaseModel):
    title:   str = Field(..., min_length=1)
    project: str | None = None
    goal:    str | None = None
    owner:   str | None = None
    tasks:   list[dict[str, Any]] = Field(default_factory=list)
    meta:    dict[str, Any] = Field(default_factory=dict)


class OperatingPlanGenerateRequest(BaseModel):
    goal:     str = Field(..., min_length=1)
    title:    str | None = None
    project:  str | None = None
    owner:    str | None = None
    workflow: str | None = None
    context:  dict[str, Any] = Field(default_factory=dict)
    create:   bool = True


class OperatingPlanPatchRequest(BaseModel):
    title:   str | None = None
    project: str | None = None
    goal:    str | None = None
    status:  str | None = None
    owner:   str | None = None
    meta:    dict[str, Any] | None = None


class OperatingTaskCreateRequest(BaseModel):
    plan_id:   str | None = None
    title:     str = Field(..., min_length=1)
    type:      str = "manual"
    status:    str = "todo"
    assignee:  str | None = None
    priority:  int = 3
    due:       str | None = None
    target_id: str | None = None
    note:      str | None = None
    meta:      dict[str, Any] = Field(default_factory=dict)


class OperatingTaskPatchRequest(BaseModel):
    title:     str | None = None
    type:      str | None = None
    status:    str | None = None
    assignee:  str | None = None
    priority:  int | None = None
    due:       str | None = None
    target_id: str | None = None
    note:      str | None = None
    meta:      dict[str, Any] | None = None


class ProjectMemoryRequest(BaseModel):
    project: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    source:  str = "operator"
    meta:    dict[str, Any] = Field(default_factory=dict)


class ObsidianPlanSyncRequest(BaseModel):
    overwrite: bool = True


class ObsidianImportRequest(BaseModel):
    project: str = Field(..., min_length=1)
    limit:   int = 20


@app.get("/production")
async def list_production(state: str | None = None, project: str | None = None, limit: int = 200):
    return {"items": production_store.list_productions(state=state, project=project, limit=limit)}


@app.get("/production/board")
async def production_board():
    return production_store.board()


@app.get("/production/intelligence")
async def production_intelligence():
    return production_store.intelligence()


@app.post("/production", dependencies=[Depends(require_admin)])
async def create_production(req: ProductionCreateRequest):
    try:
        pid = production_store.create_production(req.title, req.project, req.format, req.owner)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return production_store.get_production(pid)


@app.get("/production/{production_id}")
async def get_production(production_id: str):
    prod = production_store.get_production(production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="production not found")
    return prod


@app.post("/production/{production_id}/generate", dependencies=[Depends(require_admin)])
async def generate_for_production(production_id: str, req: ProductionGenerateRequest):
    try:
        return production_media_store.generate_one_for_production(
            production_id,
            capability=req.capability,
            brief=req.brief,
            tool=req.tool,
            source_assets=req.source_assets,
            rights=req.rights,
            meta=req.meta,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="production not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/production/{production_id}/generate-plan", dependencies=[Depends(require_admin)])
async def generate_plan_for_production(production_id: str, req: ProductionGeneratePlanRequest):
    try:
        return production_media_store.generate_plan_for_production(
            production_id,
            capabilities=req.capabilities,
            include_video=req.include_video,
            max_jobs=req.max_jobs,
            dry_run=req.dry_run,
            actor=req.actor,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="production not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/production/{production_id}/advance", dependencies=[Depends(require_admin)])
async def advance_production(production_id: str, actor: str = "operator"):
    try:
        return await production_store.advance(production_id, actor=actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="production not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/production/{production_id}/action", dependencies=[Depends(require_admin)])
async def run_production_action(production_id: str, req: ProductionActionRequest):
    try:
        return await production_store.run_action(production_id, req.action, actor=req.actor)
    except KeyError:
        raise HTTPException(status_code=404, detail="production not found")
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/production/{production_id}/transition", dependencies=[Depends(require_admin)])
async def transition_production(production_id: str, req: ProductionTransitionRequest):
    prod = production_store.get_production(production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="production not found")
    pending = governance_store.pending_gates(prod, req.to_state)
    if pending:
        return {"blocked": True, "gate": pending[0]["gate"], "pending": pending, "production": prod}
    try:
        return production_store.transition(production_id, req.to_state, req.actor, req.note)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/production/{production_id}/handoff", dependencies=[Depends(require_admin)])
async def handoff_production(production_id: str, req: ProductionHandoffRequest):
    prod = production_store.get_production(production_id)
    if not prod:
        raise HTTPException(status_code=404, detail="production not found")
    if prod.get("state") not in ("publish", "measure"):
        raise HTTPException(status_code=422, detail="production must be published before deliverable handoff")
    try:
        deliverable = create_deliverable_from_production(prod, actor=req.actor, note=req.note)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    memory = None
    if prod.get("project"):
        memory_id = operating_store.add_project_memory(
            prod["project"],
            f"Production deliverable created: {prod.get('title')} ({prod.get('format')}). "
            f"Production ID: {production_id}.",
            source="production_handoff",
            meta={"production_id": production_id, "deliverable_path": deliverable.get("path")},
        )
        memory = {"memory_id": memory_id}
    production_store.record_event(
        production_id,
        prod.get("state"),
        prod.get("state"),
        req.actor,
        f"deliverable handoff: {deliverable.get('path')}",
    )
    return {
        "ok": True,
        "production": production_store.get_production(production_id),
        "deliverable": deliverable,
        "memory": memory,
    }


@app.get("/governance/pending")
async def governance_pending():
    return governance_store.pending()


@app.get("/governance/approvals")
async def governance_approvals(target_id: str | None = None, limit: int = 200):
    return {"items": governance_store.list_approvals(target_id=target_id, limit=limit)}


@app.post("/governance/approve", dependencies=[Depends(require_admin)])
async def governance_approve(req: GovernanceApproveRequest):
    try:
        return governance_store.approve(req.gate, req.target_id, req.actor, req.note, req.status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


# ---------------------------------------------------------------------------
# Autonomous operating layer  (integrated roadmap, Phase 4)
# ---------------------------------------------------------------------------

@app.get("/operating/overview")
async def operating_overview():
    return operating_store.overview()


@app.get("/operating/daily-brief")
async def operating_daily_brief():
    return operating_store.daily_brief()


@app.get("/operating/plans")
async def operating_plans(status: str | None = None, project: str | None = None, limit: int = 100):
    return {"items": operating_store.list_plans(status=status, project=project, limit=limit)}


@app.post("/operating/plans", dependencies=[Depends(require_admin)])
async def operating_create_plan(req: OperatingPlanCreateRequest):
    try:
        plan_id = operating_store.create_plan(
            req.title,
            project=req.project,
            goal=req.goal,
            owner=req.owner,
            tasks=req.tasks,
            meta=req.meta,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return operating_store.get_plan(plan_id)


@app.post("/operating/plans/generate", dependencies=[Depends(require_admin)])
async def operating_generate_plan(req: OperatingPlanGenerateRequest):
    try:
        return operating_store.generate_plan_from_goal(
            req.goal,
            title=req.title,
            project=req.project,
            owner=req.owner,
            workflow=req.workflow,
            context=req.context,
            create=req.create,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/operating/next-action")
async def operating_next_action(plan_id: str | None = None, project: str | None = None):
    return operating_store.recommend_next_action(plan_id=plan_id, project=project)


@app.get("/operating/projects/obsidian-status")
async def operating_obsidian_status():
    try:
        return obsidian_projects_store.status()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/operating/plans/{plan_id}/sync-obsidian", dependencies=[Depends(require_admin)])
async def operating_sync_plan_obsidian(plan_id: str, req: ObsidianPlanSyncRequest):
    try:
        return obsidian_projects_store.sync_plan(plan_id, overwrite=req.overwrite)
    except KeyError:
        raise HTTPException(status_code=404, detail="plan not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/operating/projects/import-obsidian", dependencies=[Depends(require_admin)])
async def operating_import_obsidian(req: ObsidianImportRequest):
    try:
        return obsidian_projects_store.import_project_notes(req.project, limit=req.limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.get("/operating/plans/{plan_id}")
async def operating_get_plan(plan_id: str):
    plan = operating_store.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@app.patch("/operating/plans/{plan_id}", dependencies=[Depends(require_admin)])
async def operating_patch_plan(plan_id: str, req: OperatingPlanPatchRequest):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        ok = operating_store.update_plan(plan_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="plan not found")
    return operating_store.get_plan(plan_id)


@app.get("/operating/tasks")
async def operating_tasks(plan_id: str | None = None, status: str | None = None, project: str | None = None, limit: int = 200):
    return {"items": operating_store.list_tasks(plan_id=plan_id, status=status, project=project, limit=limit)}


@app.post("/operating/tasks", dependencies=[Depends(require_admin)])
async def operating_create_task(req: OperatingTaskCreateRequest):
    try:
        task_id = operating_store.add_task(
            req.plan_id,
            req.title,
            type=req.type,
            status=req.status,
            assignee=req.assignee,
            priority=req.priority,
            due=req.due,
            target_id=req.target_id,
            note=req.note,
            meta=req.meta,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="plan not found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"task_id": task_id, "items": operating_store.list_tasks(plan_id=req.plan_id, limit=500)}


@app.patch("/operating/tasks/{task_id}", dependencies=[Depends(require_admin)])
async def operating_patch_task(task_id: str, req: OperatingTaskPatchRequest):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    try:
        ok = operating_store.update_task(task_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="task not found")
    return {"items": operating_store.list_tasks(limit=500)}


@app.post("/operating/sync-approvals", dependencies=[Depends(require_admin)])
async def operating_sync_approvals():
    return {"items": operating_store.sync_approval_tasks()}


@app.post("/operating/sync-productions", dependencies=[Depends(require_admin)])
async def operating_sync_productions():
    return {"items": operating_store.sync_production_tasks()}


@app.get("/operating/project-memory")
async def operating_project_memory(project: str | None = None, limit: int = 100):
    return {"items": operating_store.list_project_memory(project=project, limit=limit)}


@app.post("/operating/project-memory", dependencies=[Depends(require_admin)])
async def operating_add_project_memory(req: ProjectMemoryRequest):
    memory_id = operating_store.add_project_memory(
        req.project,
        req.content,
        source=req.source,
        meta=req.meta,
    )
    return {"memory_id": memory_id, "items": operating_store.list_project_memory(project=req.project)}


# ---------------------------------------------------------------------------
# Prompt assistant
# ---------------------------------------------------------------------------

class ImprovePromptRequest(BaseModel):
    prompt:   str = Field(..., min_length=1, max_length=8192)
    max_tier: int = 1


@app.post("/improve-prompt")
async def improve_prompt_endpoint(req: ImprovePromptRequest):
    """Rewrite a draft prompt to be clearer and more specific (cheap model)."""
    return await improve_prompt(req.prompt, max_tier=req.max_tier)


@app.delete("/cost", dependencies=[Depends(require_admin)])
async def reset_cost():
    """Clear the session cost ledger."""
    cost_tracker.clear()
    return {"status": "cleared"}


@app.get("/models")
async def get_models():
    """Return all models in the registry with availability flags."""
    result = {}
    for key, spec in MODELS.items():
        result[key] = {
            "label":             spec.label,
            "provider":          spec.provider,
            "tier":              spec.tier,
            "available":         spec.is_available(),
            "cost_input_per_m":  spec.cost_input_per_m,
            "cost_output_per_m": spec.cost_output_per_m,
            "context_window":    spec.context_window,
            "capabilities":      list(spec.capabilities),
        }
    return result


class OptimizeRequest(BaseModel):
    query:    str
    max_tier: int = 3


@app.post("/optimize")
async def optimize_model(req: OptimizeRequest):
    """
    Dry-run the token optimizer: show which model would be chosen
    and the cost comparison for all candidates.
    """
    result = pick_model(req.query, max_tier=req.max_tier)
    return {
        "chosen_model":       result.model_key,
        "chosen_label":       result.model.label,
        "task_type":          result.task_type,
        "estimated_cost_usd": result.estimated_cost_usd,
        "input_tokens":       result.input_tokens,
        "expected_output":    result.expected_output,
        "reason":             result.reason,
        "all_candidates":     [
            {"model": k, "cost_usd": round(c, 6)} for k, c in result.candidates_scored
        ],
    }


@app.get("/wijerco/roster")
async def wijerco_roster():
    """Full WijerCo org chart — departments and their named employee agents."""
    return get_roster()


@app.get("/agents")
async def list_agents():
    """Return metadata for all agents — used by the command centre UI."""
    rag_agents = {
        "local_data":  {"label": "Local Data",   "emoji": "📁", "color": "#6366f1", "port": 8001, "description": "Obsidian vault + Qdrant"},
        "search":      {"label": "Web Search",   "emoji": "🌐", "color": "#3b82f6", "port": 8002, "description": "SearXNG / Tavily"},
        "cloud":       {"label": "Cloud Engine", "emoji": "☁️",  "color": "#0ea5e9", "port": 8003, "description": "GCS / S3"},
        "indexer":     {"label": "Indexer",      "emoji": "🗂️",  "color": "#64748b", "port": 8005, "description": "Vault embedding pipeline"},
        "retriever":   {"label": "Retriever",    "emoji": "🔍", "color": "#64748b", "port": 8006, "description": "Qdrant semantic search"},
        "whisper":     {"label": "Whisper",      "emoji": "🎙️",  "color": "#8b5cf6", "port": 8007, "description": "Audio transcription"},
        "video":       {"label": "Video",        "emoji": "🎬", "color": "#ec4899", "port": 8008, "description": "FFmpeg / MoviePy"},
        "notifier":    {"label": "Notifier",     "emoji": "🔔", "color": "#f59e0b", "port": 8004, "description": "Apprise notifications"},
    }
    return {
        "wijerco": DEPT_META,
        "rag": rag_agents,
        "infrastructure": {
            "qdrant":    {"label": "Qdrant",    "port": 6333, "url": "http://localhost:6333/dashboard"},
            "ollama":    {"label": "Ollama",    "port": 11434},
            "n8n":       {"label": "n8n",       "port": 5678,  "url": "http://localhost:5678"},
            "searxng":   {"label": "SearXNG",   "port": 8080,  "url": "http://localhost:8080"},
        },
    }


class WijerCoRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=8192)
    department: str | None = Field(None, description="Force a specific department (optional)")
    subagent: str | None = Field(None, description="Target a specific WijerCo employee agent")
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_history: list[dict] = Field(default_factory=list)


class HybridRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=8192)
    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_history: list[dict] = Field(default_factory=list)
    force_route: str | None = None   # "rag" | "wijerco" | department key | None = auto
    department: str | None = None    # used with force_route="wijerco" or subagent validation
    subagent: str | None = None      # target a specific WijerCo employee agent


def _resolve_subagent(slug: str | None, department: str | None = None) -> dict | None:
    if not slug:
        return None
    sub = lookup_subagent(slug)
    if not sub:
        raise HTTPException(status_code=422, detail=f"unknown subagent: {slug}")
    if department and department != sub["department"]:
        raise HTTPException(
            status_code=422,
            detail=f"subagent {slug} belongs to {sub['department']}, not {department}",
        )
    return sub


@app.post("/classify")
async def classify_query(req: QueryRequest):
    """Dry-run: classify the routing intent without executing."""
    classification = classify_intent(req.query)
    subagent, subagent_confidence, subagent_reason = select_subagent(
        req.query, classification.department
    )
    return {
        **classification.model_dump(),
        "subagent": subagent,
        "subagent_confidence": subagent_confidence,
        "subagent_reason": subagent_reason,
    }


@app.post("/wijerco")
async def run_wijerco(req: WijerCoRequest):
    """
    Run a query directly through the WijerCo agent layer.
    Auto-classifies department if not specified.
    """
    sub = _resolve_subagent(req.subagent, req.department)
    if sub:
        department = sub["department"]
    elif req.department:
        department = req.department
    else:
        classification = classify_intent(req.query)
        department = classification.department or "orchestrator"

    selected_subagent = req.subagent
    if not selected_subagent and department != "orchestrator":
        selected_subagent, _, _ = select_subagent(req.query, department)

    result = await call_wijerco_agent(
        department=department,
        query=req.query,
        conversation_history=req.conversation_history,
        subagent=selected_subagent,
    )
    return {
        "session_id":   req.session_id,
        "department":   result["department"],
        "subagent":     selected_subagent,
        "answer":       result["answer"],
        "model":        result["model"],
        "tokens_used":  result["tokens_used"],
        "error":        result["error"],
    }


@app.post("/hybrid")
async def run_hybrid(req: HybridRequest):
    """
    Hybrid routing endpoint.
    1. Classify intent
    2. If RAG-only: run the LangGraph pipeline
    3. If WijerCo-only: run the WijerCo agent
    4. If hybrid: run RAG first, pass context to WijerCo agent
    """
    sub = _resolve_subagent(req.subagent, req.department)
    if sub and not req.force_route:
        route = sub["department"]
    else:
        route = req.force_route or classify_intent(req.query).target

    # Use stored history if no history passed in the request
    history = req.conversation_history or get_history_for_llm(req.session_id)

    rag_result   = None
    wijerco_result = None

    # Step 1: RAG retrieval (for hybrid or rag targets)
    if route in ("rag", "hybrid"):
        initial_state: AgentState = {
            "messages": [],
            "query":    req.query,
            "routing":  None,
            "context_chunks": [],
            "output_payload": {},
            "agents_used": [],
            "errors": [],
            "finished": False,
        }
        try:
            final_state = graph.invoke(initial_state)
            rag_result = final_state.get("output_payload", {})
        except Exception as exc:
            rag_result = {"error": str(exc), "answer": "", "context_chunks": []}

    selected_subagent = req.subagent

    # Step 2: WijerCo synthesis
    if route in ("hybrid",) or (route not in ("rag",)):
        if sub:
            department = sub["department"]
        elif req.department:
            department = req.department
        elif route in DEPT_META and route not in ("rag", "hybrid"):
            department = route
        elif route == "wijerco":
            department = classify_intent(req.query).department or "research_intelligence"
        else:
            classification = classify_intent(req.query)
            department = classification.department or "research_intelligence"
        if not selected_subagent:
            selected_subagent, _, _ = select_subagent(req.query, department)
        rag_chunks = []
        if rag_result:
            # Pass retrieved chunks into WijerCo context
            rag_chunks = rag_result.get("context_chunks", [])

        wijerco_result = await call_wijerco_agent(
            department=department,
            query=req.query,
            rag_context=rag_chunks,
            conversation_history=history,
            subagent=selected_subagent,
        )

    # Compose final answer
    if route == "rag" and rag_result:
        answer       = rag_result.get("answer", "")
        final_route  = "rag"
        department   = None
        model_used   = rag_result.get("model_key", "")
        cost_usd     = rag_result.get("cost_usd", 0.0)
    elif wijerco_result:
        answer       = wijerco_result["answer"]
        final_route  = wijerco_result["department"]
        department   = wijerco_result["department"]
        model_used   = wijerco_result.get("model", "")
        cost_usd     = wijerco_result.get("cost_usd", 0.0)
    else:
        answer       = "[No result]"
        final_route  = route
        department   = None
        model_used   = ""
        cost_usd     = 0.0

    # Persist conversation turns
    add_message(req.session_id, "user",      req.query, cost_usd=0.0)
    add_message(req.session_id, "assistant", answer, model_key=model_used, cost_usd=cost_usd)

    return {
        "session_id":     req.session_id,
        "query":          req.query,
        "route":          final_route,
        "department":     department,
        "subagent":       selected_subagent,
        "answer":         answer,
        "rag_summary":    rag_result.get("answer", "") if rag_result else None,
        "agents_used":    rag_result.get("agents_used", []) if rag_result else [],
        "context_count":  rag_result.get("context_count", 0) if rag_result else 0,
        "model":          model_used,
        "cost_usd":       cost_usd,
        "errors":         (rag_result.get("errors", []) if rag_result else [])
                          + ([wijerco_result["error"]] if wijerco_result and wijerco_result.get("error") else []),
    }


# ---------------------------------------------------------------------------
# Session endpoints
# ---------------------------------------------------------------------------

@app.post("/sessions")
async def new_session(title: str = "New conversation"):
    """Create a new conversation session."""
    s = create_session(title)
    return {"id": s.id, "title": s.title, "created_at": s.created_at}


@app.get("/sessions")
async def get_sessions(limit: int = 50):
    """List all sessions, most recent first."""
    return [
        {"id": s.id, "title": s.title, "created_at": s.created_at,
         "updated_at": s.updated_at, "message_count": s.message_count}
        for s in list_sessions(limit)
    ]


@app.get("/sessions/{session_id}")
async def get_session_detail(session_id: str):
    s = get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    msgs = get_messages(session_id)
    total_cost = session_cost(session_id)
    return {
        "id":            s.id,
        "title":         s.title,
        "created_at":    s.created_at,
        "updated_at":    s.updated_at,
        "message_count": s.message_count,
        "total_cost_usd": total_cost,
        "messages": [
            {"role": m.role, "content": m.content,
             "model_key": m.model_key, "cost_usd": m.cost_usd, "ts": m.ts}
            for m in msgs
        ],
    }


@app.delete("/sessions/{session_id}", dependencies=[Depends(require_admin)])
async def remove_session(session_id: str):
    ok = delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"status": "deleted", "session_id": session_id}


# ---------------------------------------------------------------------------
# Memory endpoints
# ---------------------------------------------------------------------------

@app.get("/memory")
async def search_memory(q: str, top_k: int = 5):
    """Recall memories semantically matching a query."""
    from memory.memory_store import store as mem_store
    memories = await mem_store.recall(q, top_k=top_k)
    return [
        {"id": m.id, "entity": m.entity, "content": m.content,
         "source": m.source, "timestamp": m.timestamp, "score": m.score}
        for m in memories
    ]


class MemoryAddRequest(BaseModel):
    entity:  str
    content: str
    source:  str = "manual"


@app.post("/memory", dependencies=[Depends(require_admin)])
async def add_memory(req: MemoryAddRequest):
    from memory.memory_store import store as mem_store
    entry_id = await mem_store.add(req.entity, req.content, req.source)
    return {"status": "stored", "id": entry_id}


@app.delete("/memory/{entry_id}", dependencies=[Depends(require_admin)])
async def delete_memory(entry_id: str):
    from memory.memory_store import store as mem_store
    await mem_store.delete(entry_id)
    return {"status": "deleted"}


@app.delete("/memory", dependencies=[Depends(require_admin)])
async def clear_memory():
    from memory.memory_store import store as mem_store
    await mem_store.clear_all()
    return {"status": "cleared"}


@app.post("/memory/episodic/{session_id}", dependencies=[Depends(require_admin)])
async def summarise_episode(session_id: str, department: str = "general"):
    """Summarise a session into episodic memory."""
    from memory.episodic import summarise_session
    ep = await summarise_session(session_id, department)
    if not ep:
        return {"status": "skipped", "reason": "session too short or unavailable"}
    return {"status": "stored", "summary": ep.summary, "id": ep.id}


# ---------------------------------------------------------------------------
# Self-Harness — self-improvement loop
# ---------------------------------------------------------------------------

class HarnessRunRequest(BaseModel):
    departments: list[str] | None = None


@app.post("/harness/run", dependencies=[Depends(require_admin)])
async def harness_run(req: HarnessRunRequest):
    """Run one self-harness optimization loop. Queues proposals for approval."""
    from harness.loop import run_loop
    return await run_loop(req.departments)


@app.get("/harness/proposals")
async def harness_proposals(status: str = "pending"):
    """List harness proposals (pending by default)."""
    from harness.store import list_proposals
    return list_proposals(None if status == "all" else status)


@app.post("/harness/proposals/{pid}/accept", dependencies=[Depends(require_admin)])
async def harness_accept(pid: str):
    """Approve a proposal: append the learned rule to the WijerCo file (with backup)."""
    from harness.store import accept_proposal
    result = accept_proposal(pid)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result["message"])
    return result


@app.post("/harness/proposals/{pid}/reject", dependencies=[Depends(require_admin)])
async def harness_reject(pid: str):
    from harness.store import reject_proposal
    ok = reject_proposal(pid)
    if not ok:
        raise HTTPException(status_code=404, detail="proposal not found or already decided")
    return {"status": "rejected", "id": pid}


@app.get("/harness/history")
async def harness_history(limit: int = 20):
    from harness.store import list_iterations
    return list_iterations(limit)


# ---------------------------------------------------------------------------
# n8n automation (MCP)
# ---------------------------------------------------------------------------

@app.get("/n8n/health")
async def n8n_health():
    from .n8n_client import health
    return await health()


@app.get("/n8n/tools")
async def n8n_tools():
    """List the workflows exposed by the n8n MCP Server Trigger."""
    from .n8n_client import list_tools
    try:
        tools = await list_tools()
        return {"status": "ok", "count": len(tools), "tools": tools}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"n8n MCP error: {exc}")


class N8nCallRequest(BaseModel):
    name:      str
    arguments: dict = Field(default_factory=dict)


@app.post("/n8n/call", dependencies=[Depends(require_admin)])
async def n8n_call(req: N8nCallRequest):
    """Trigger an n8n workflow-tool by name."""
    from .n8n_client import call_tool
    try:
        return await call_tool(req.name, req.arguments)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"n8n MCP error: {exc}")


# ---------------------------------------------------------------------------
# Streaming endpoints (SSE)
# ---------------------------------------------------------------------------

class StreamRequest(BaseModel):
    query:                str = Field(..., min_length=1, max_length=8192)
    session_id:           str = Field(default_factory=lambda: str(uuid.uuid4()))
    system_prompt:        str = ""
    conversation_history: list[dict] = Field(default_factory=list)
    force_model_key:      str | None = None
    max_tier:             int = 3
    subagent:             str | None = None     # target a specific WijerCo employee
    department:           str | None = None     # its department
    tools:                bool = False          # let the agent call n8n workflows


async def _sse_generator(gen: AsyncGenerator[dict, None]) -> AsyncGenerator[str, None]:
    """Wrap a dict-yielding generator into SSE text/event-stream format."""
    async for event in gen:
        yield f"data: {json.dumps(event)}\n\n"
    yield "data: [DONE]\n\n"


@app.post("/query/stream")
async def stream_query(req: StreamRequest):
    """
    SSE streaming endpoint for RAG queries.
    Yields tokens as they arrive; final event contains full metadata.
    """
    system = req.system_prompt
    chat_files = uploads_mod.get_chat_context(req.session_id)
    if chat_files:
        system = (chat_files + "\n\n---\n\n" + system) if system else chat_files

    async def _gen() -> AsyncGenerator[dict, None]:
        async for event in stream_with_fallback(
            user_message    = req.query,
            system_prompt   = system,
            history         = req.conversation_history,
            force_model_key = req.force_model_key,
            max_tier        = req.max_tier,
        ):
            yield event

    return StreamingResponse(
        _sse_generator(_gen()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/hybrid/stream")
async def stream_hybrid(req: StreamRequest):
    """
    SSE streaming hybrid endpoint.
    Classifies intent, runs RAG context retrieval (non-streaming),
    then streams the WijerCo or fallback LLM response token-by-token.
    """
    # If a specific employee agent is targeted, route straight to it.
    sub = _resolve_subagent(req.subagent, req.department)
    if sub:
        route      = "wijerco"
        department = sub["department"]
    else:
        # Classify synchronously first
        classification = classify_intent(req.query)
        route          = classification.target
        department     = classification.department

    # RAG context retrieval (non-streaming, fast)
    rag_context: list = []
    if route in ("rag", "hybrid"):
        try:
            initial_state: AgentState = {
                "messages":       [],
                "query":          req.query,
                "routing":        None,
                "context_chunks": [],
                "output_payload": {},
                "agents_used":    [],
                "errors":         [],
                "finished":       False,
            }
            final_state = await graph.ainvoke(initial_state)
            rag_context = final_state.get("output_payload", {}).get("context_chunks", [])
        except Exception:
            pass

    # Build system prompt for WijerCo agent (with specific employee role if set)
    from .wijerco_agent import _build_system_prompt
    system = _build_system_prompt(
        department or "research_intelligence",
        rag_context,
        subagent=req.subagent,
    )

    # Inject any files the user uploaded to this conversation (chat mode)
    chat_files = uploads_mod.get_chat_context(req.session_id)
    if chat_files:
        system = chat_files + "\n\n---\n\n" + system

    history = req.conversation_history or get_history_for_llm(req.session_id)

    async def _gen() -> AsyncGenerator[dict, None]:
        # First emit a metadata event so the UI knows route/department
        yield {
            "type":       "meta",
            "route":      route,
            "department": department,
            "subagent":   req.subagent,
            "session_id": req.session_id,
            "uploaded":   uploads_mod.list_chat_context(req.session_id),
            "done":       False,
        }
        full = ""
        final = {}

        if req.tools:
            from .agent_executor import run_agentic_turn
            source = run_agentic_turn(req.query, system, history, req.max_tier,
                                      force_model_key=req.force_model_key)
        else:
            source = stream_with_fallback(
                user_message    = req.query,
                system_prompt   = system,
                history         = history,
                force_model_key = req.force_model_key,
                max_tier        = req.max_tier,
            )

        async for event in source:
            # Agentic events already carry a "type"; normal stream events don't.
            if "type" not in event:
                event["type"] = "token" if not event.get("done") else "end"
            if event.get("token") and not event.get("done"):
                full += event["token"]
            if event.get("done"):
                final = event
            yield event

        # Persist the turn so it appears in the session list and feeds memory
        try:
            add_message(req.session_id, "user", req.query)
            add_message(req.session_id, "assistant", full,
                        model_key=final.get("model_key"), cost_usd=final.get("cost_usd", 0.0))
            # Opportunistic episodic summarisation every few turns
            from memory.episodic import summarise_session
            msgs = get_messages(req.session_id, limit=50)
            if len([m for m in msgs if m.role == "user"]) % 3 == 0:
                asyncio.ensure_future(summarise_session(req.session_id, department or "general"))
        except Exception:
            pass

    return StreamingResponse(
        _sse_generator(_gen()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/webhook")
async def n8n_webhook(payload: WebhookPayload, background_tasks: BackgroundTasks):
    """
    Receives events from n8n (e.g. scheduled triggers, file-change notifications).
    Runs the query pipeline in the background.
    """
    if payload.event == "query":
        query_text = payload.data.get("query", "")
        if query_text:
            background_tasks.add_task(_background_query, query_text)
            return {"status": "accepted", "event": payload.event}

    return {"status": "ignored", "event": payload.event}


async def _background_query(query: str) -> None:
    """Fire-and-forget query execution triggered by n8n webhook."""
    initial_state: AgentState = {
        "messages": [],
        "query": query,
        "routing": None,
        "context_chunks": [],
        "output_payload": {},
        "agents_used": [],
        "errors": [],
        "finished": False,
    }
    try:
        final_state = graph.invoke(initial_state)
        payload = final_state.get("output_payload", {})

        # Forward result to Apprise notifier
        notifier_url = os.getenv("NOTIFIER_URL", "http://localhost:8004")
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{notifier_url}/notify",
                json={
                    "title": "Agentic RAG — Query Complete",
                    "body": payload.get("answer", "No answer generated"),
                    "tags": ["rag", "query"],
                },
                timeout=10.0,
            )
    except Exception as exc:
        print(f"[background_query] Error: {exc}")


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    # reload=False: a single process is cleanly killable. reload=True spawns a
    # child worker that survives stop_all and keeps serving stale code on 8000.
    uvicorn.run("orchestrator.main:app", host=bind_host(), port=8000, reload=False)
# end of orchestrator.main
