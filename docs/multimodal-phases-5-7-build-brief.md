# Multimodal Expansion: Build Brief for Phases 5 to 7

For an agent (Codex) continuing the build with no prior conversation context. Read this top to bottom before writing code. The full design is in `docs/multimodal-expansion-architecture.md`; this brief is the executable plan for the three remaining phases.

## What is already built (Phases 1 to 4, all tested)

Phase 1, Media Asset Registry. `media/registry.py` is a SQLite store at `data/media.db` (WAL on). Tables: `assets`, `transcripts`, `asset_links`. Public functions: `add_asset`, `get_asset(asset_id, with_relations=True)`, `list_assets(...)`, `update_asset(asset_id, **fields)`, `set_status`, `set_embeddings`, `delete_asset`, `add_transcript`, `get_transcript`, `add_link`, `get_links`, `stats`. The `assets` row has a `meta` TEXT column holding JSON (worker metadata: scene boundaries, slide structure, web url). Enums: `ASSET_TYPES`, `RIGHTS` (owned, licensed, client_confidential, third_party, unknown), `STATUSES` (ingesting, ready, quarantined, failed, archived), `RELATIONS`. Asset routes live in `orchestrator/main.py`: `GET /assets`, `GET /assets/{id}`, `POST /assets/ingest`, `PATCH /assets/{id}`, `DELETE /assets/{id}` (writes require `require_admin`).

Phase 2, retrieval. `rag/schema.py` `Chunk` carries `asset_id`, `media_type`, `t_start`, `t_end`, `speaker`, `thumbnail_path`, and `source_ref()` emits them. `rag/visual_embedder.py` is self-hosted CLIP (open_clip, lazy, graceful, `VISUAL_DIM` default 512), with `embed_image`, `embed_text`, `available()`. `rag/media_index.py` ensures three Qdrant collections (`media_text`, `media_transcripts`, `media_visual`) and exposes `index_asset(asset_id)` plus `ensure_media_collections()`. `rag/media_search.py` exposes `media_search(query, modalities, filters, top_k)` and backs `POST /media/search`. `rag/retriever.py` gained `append_bm25_corpus`.

Phase 3, ingestion. `media/ingest/` package: `__init__.py` dispatcher (`detect_type`, `ingest`, `_autoindex`), workers `audio.py`, `images.py`, `video.py`, `slides.py`, `web.py`, `docs.py`. The dispatcher routes all six asset types and indexes worker-created children (video keyframes). Each worker degrades gracefully without its heavy dependency.

Phase 4, Content Studio agents. `orchestrator/wijerco_roster.py` has a `content_studio` department with seven agents (slugs: `brief-builder`, `research-producer`, `scriptwriter`, `storyboarder`, `visual-director`, `editor`, `qa-brand-reviewer`; personas Bria, Sera, Scout, Bree, Vidal, Cade, Wren). Wired into `orchestrator/wijerco_router.py` (`WijerCoDept`, `RouteTarget`, `_DEPT_SIGNALS`, `DEPT_META`) and `orchestrator/wijerco_agent.py` (`_DEPT_FILE`, `_KB_FILES_BY_DEPT`, `_DEPT_TASK_TYPE`). Role files live in the WijerCo folder at `AGENTS/departments/content-studio.md` and `AGENTS/subagents/{slug}.md`, each with an explicit Input/Output contract. Agents run through the existing `POST /hybrid` and `POST /wijerco` with `force_route: content_studio` and `subagent: <slug>`. The WijerCo folder path is `WIJERCO_PATH` (default `C:\Users\ajwij\Claude Cowork\WijerCo`).

## Repository conventions to follow exactly

Services are FastAPI apps; the orchestrator is `orchestrator/main.py` on port 8000 with `dependencies=[Depends(require_api_key)]`. Use `common.security`: `require_api_key`, `require_admin` (writes/paid/destructive), `cors_kwargs()`, `bind_host()`, `confine_to_roots(candidate, roots)`, `audit_log(event, detail)`, `backup_file(target)`. SQLite stores follow the `harness/store.py` and `media/registry.py` pattern: a `@contextmanager _db()` with `sqlite3.Row`, `CREATE TABLE IF NOT EXISTS` on open, JSON columns for dicts/lists. Qdrant access is raw httpx against `QDRANT_URL` (see `rag/media_index.py`). Agent calls go through `call_wijerco_agent(department, query, rag_context, conversation_history, subagent=...)` in `orchestrator/wijerco_agent.py`. n8n workflows are JSON in `n8n-workflows/` and call orchestrator endpoints over `http://host.docker.internal:8000`; model on `n8n-workflows/6-content-pipeline.json`. The Command Centre UI is `ui/command_centre.html`; its board endpoints are documented in `ui/COMMAND_CENTRE_ENDPOINTS.md`, base URL `http://localhost:8000`, every board endpoint is GET and returns `{"items": [...]}` or a column-keyed object, and any non-200 makes the page fall back to seed data.

CRITICAL repo hazard. This repo is OneDrive-synced. The file-editing tools intermittently persist a TRUNCATED copy to disk (the tail is lost) while the editor view looks intact. After any file edit, verify on disk with `python3 -m py_compile <file>`. If it fails, rewrite the whole file (a full-file write persists correctly). Prefer whole-file writes and in-place `python` string-replacement scripts over partial edits for existing files. Always run a compile sweep before declaring done.

Style for any human-facing copy or docs (Aaron's rules): lead with the point, active voice, no em dashes (use commas, periods, semicolons), and avoid the banned words in `ABOUT ME/anti-ai-writing-style.md` (elevate, leverage, transform, seamless, robust, ecosystem, etc.). Code comments and internal docs should be plain and specific.

---

## Phase 5: Production Pipeline

Goal. A persistent production record and a state machine that walks a piece through `idea -> brief -> research -> outline -> draft -> asset_plan -> render -> review -> publish -> measure`, driven by the Content Studio agents, surfaced on the Command Centre board, and runnable from n8n.

### 5.1 Storage — `orchestrator/production.py`

New SQLite tables in `data/media.db` (reuse the registry db; open with the same `_db()` style, do not import registry's private `_db`, make your own contextmanager pointed at `MEDIA_DB_PATH`).

`productions`:
- `production_id` TEXT PK (uuid4)
- `title` TEXT
- `project` TEXT (engagement or content project slug; nullable)
- `format` TEXT (one of the six template families: `linkedin_short`, `explainer_carousel`, `talking_head_clip`, `policy_briefing`, `course_teaser`, `proposal_walkthrough`)
- `state` TEXT (one of the ten states; default `idea`)
- `brief` TEXT json, `research` TEXT json, `script` TEXT json, `asset_plan` TEXT json, `edit_plan` TEXT json, `review` TEXT json (each agent writes its slice)
- `linked_assets` TEXT json (array of asset_id)
- `gates` TEXT json (gate name -> {status, by, at}; Phase 7 populates)
- `owner` TEXT, `created_at` TEXT, `updated_at` TEXT

`production_events` (the transition log, model on `orchestrator/decision_log.py`):
- `id` TEXT PK, `production_id` TEXT, `at` TEXT, `from_state` TEXT, `to_state` TEXT, `actor` TEXT, `note` TEXT

Functions: `create_production(title, project, format, owner) -> id`; `get_production(id)`; `list_productions(state=None, project=None, limit=200)`; `update_production(id, **fields)` (json-encode dict fields); `record_event(id, from_state, to_state, actor, note)`; `advance(id, actor)` (see 5.3); `transition(id, to_state, actor, note)` (manual move, logs, validates the target is a real state).

Define `STATES = ("idea","brief","research","outline","draft","asset_plan","render","review","publish","measure")` and `STATE_ORDER` for index lookups. Forward by default; allow backward (review can return to draft) and log it.

### 5.2 State -> agent map

Each forward transition runs the right Content Studio subagent via `call_wijerco_agent`. Map:
- `idea -> brief`: subagent `brief-builder`, writes `brief`
- `brief -> research`: `research-producer`, writes `research`
- `research -> outline` and `outline -> draft`: `scriptwriter`, writes `script` (outline first, then full draft; store both under `script`)
- `draft -> asset_plan`: `storyboarder`, writes `asset_plan`; then `visual-director` augments `asset_plan` with generation briefs for scenes flagged `needs_generation`
- `asset_plan -> render`: `editor`, writes `edit_plan`
- `render -> review`: the render layer runs (Phase 6); until then, mark render prepared
- `review -> publish`: `qa-brand-reviewer`, writes `review`; this transition is gate-blocked (Phase 7)
- `publish -> measure`: no agent; records publication

Build the agent prompt by passing the production record (the prior slices) into the query/context so each agent sees what came before. Reuse `call_wijerco_agent(department="content_studio", query=<assembled>, subagent=<slug>)`. Parse the agent's returned text into the slice; agents are instructed to return their contract fields, so prefer a tolerant JSON-or-structured parse with a fallback that stores the raw text under a `_raw` key.

### 5.3 `advance(id, actor)`

Look up current state, find the next state in `STATE_ORDER`, check whether any gate blocks the transition (Phase 7 `governance.check`; until Phase 7 lands, treat gates as open), run the mapped agent, write its slice, then `transition` to the next state and `record_event`. Return the updated production plus the agent output. If a gate blocks, do not advance; return `{blocked: true, gate: <name>}` and ping the notifier (port 8004) so the operator can approve.

### 5.4 Orchestrator routes (in `orchestrator/main.py`)

- `GET /production` and `GET /production/{id}`
- `POST /production` (create from a goal; body `{title, project?, format, owner?}`; lands in `idea`) — `require_admin`
- `POST /production/{id}/advance` — runs the next agent; `require_admin`
- `POST /production/{id}/transition` (manual override; body `{to_state, note}`) — `require_admin`
- `GET /production/board` — returns the Command Centre board envelope (see 5.6)

Import production module the same way registry/media_search are imported near the top of `main.py`.

### 5.5 n8n workflow — `n8n-workflows/13-content-studio-pipeline.json`

Model on `6-content-pipeline.json`. A trigger, then sequential `POST /production/{id}/advance` calls, pausing when a response has `blocked: true` and pinging the notifier so Aaron can approve in the Command Centre. Keep timeouts at 180000 like the existing content workflow.

### 5.6 Command Centre board

Add `GET /production/board` returning a single object keyed by board columns (match the existing `/content/pipeline` envelope shape in `ui/COMMAND_CENTRE_ENDPOINTS.md`). Map states to columns: `idea`+`brief` -> Ideas; `research`+`outline`+`draft` -> Drafting; `asset_plan`+`render` -> In Production; `review` -> Review; `publish`+`measure` -> Published. Each card: `{title, cap: format, status: state, st: status-class, meta}`. Then add a board view in `ui/command_centre.html` modelled on the existing content pipeline board (it already renders column-keyed data; the change is data wiring, not a rewrite). Remember the OneDrive hazard when editing the HTML; verify the file is intact after.

### 5.7 Tests

Unit-test the state machine without agents by monkeypatching the agent call: create -> advance through every state -> assert events logged and slices written. Test backward `transition`. Test `list_productions` filters. Test the board envelope shape. Compile-sweep all touched files.

---

## Phase 6: Rendering Stack and the Hybrid Adapter

Goal. Remotion template families for repeatable video, a render service that turns a production into a `derived` asset, and one adapter interface with a self-hosted implementation and an MCP implementation, selected by config, with paid calls gated.

### 6.1 Adapter interface — `media/adapters/`

`base.py` defines abstract interfaces: `Transcriber`, `ImageGenerator`, `VideoGenerator`, `AudioGenerator`, `VisualEmbedder` (this last one already effectively exists as `rag/visual_embedder`; wrap it). Each has a small method surface, for example `ImageGenerator.generate(brief: dict) -> {path, asset_id}`.

`selfhosted.py`: implementations backed by the existing stack — faster-whisper (transcribe), ffmpeg + Remotion (video), open_clip (visual embed). `AudioGenerator` has no self-hosted path; return a clear `NotAvailable`.

`mcp.py`: implementations that call the connected MCP servers when configured — Descript (transcribe/edit), Higgsfield (image/video/audio/3D), Canva (image/design), ElevenLabs (audio/voice). These are remote MCP tools; the code should call them through whatever MCP bridge the orchestrator uses, or expose a thin HTTP shim. Keep each adapter behind the interface so callers never import an MCP directly.

`gateway.py`: the single chokepoint. `select(capability) -> adapter` reads env (`ADAPTER_IMAGE`, `ADAPTER_VIDEO`, `ADAPTER_AUDIO`, `ADAPTER_TRANSCRIBE`; values `self` or `mcp:<name>`; default every one to `self`). Every `mcp:` call passes the `paid_job` governance gate (Phase 7) before spending. A `self` call skips the paid gate but still respects rights and publication gates.

### 6.2 Remotion template families — in `my-video/`

`my-video/` is an existing Remotion project (package.json, src, remotion.config.ts). Add six parametrised compositions driven by a JSON props file the pipeline writes: `linkedin_short`, `explainer_carousel`, `talking_head_clip` (cut against `media_transcripts` timestamps), `policy_briefing`, `course_teaser`, `proposal_walkthrough`. Each composition reads props (script lines, asset paths, captions, brand colours). Keep brand styling consistent; pull Aaron's voice rules into on-screen text.

### 6.3 Render service — `media/render.py` (or extend `media/video_pipeline.py` on 8008)

`render(production_id, template, props) -> asset_id`. Wraps `npx remotion render <template> --props=<file> <out>`. Writes the output under `MEDIA_DERIVED_ROOT`, registers it via `registry.add_asset(type="video", source="derived", ...)`, links it to the production (`linked_assets`) and to its source assets (`add_link(..., "derived_from")`). Best-effort and gated: a `paid_job` gate applies if any MCP generation fed the render.

### 6.4 Wire into Phase 5

`asset_plan -> render` calls the render service for the production's format with props assembled from `script` + `asset_plan` + `edit_plan`. The produced asset id goes on the production.

### 6.5 Tests

Render one `linkedin_short` and one `talking_head_clip` self-hosted (requires ffmpeg + node + Remotion installed; gate behind a skip if absent). Assert the output registers as a `derived` asset linked to the production. Test the gateway selects `self` by default and routes `mcp:` only when configured. Compile-sweep.

---

## Phase 7: Governance

Goal. Five named gates that block a state transition or an adapter spend until satisfied, enforced at exactly two chokepoints, with approvals logged.

### 7.1 `orchestrator/governance.py`

Gates: `public_claim` (blocks `review -> publish`), `generated_image` (blocks use of any generated image in a render), `client_sensitive` (blocks publish/share of `client_confidential` assets), `paid_job` (blocks any `mcp:` adapter call with a cost), `external_publish` (blocks the `publish` transition itself).

`check(gate, context) -> {ok: bool, reason}`. A gate is open when an approval exists for it on the relevant production or job, else closed. Store approvals in a `gate_approvals` table in `data/media.db`: `id, gate, target_id (production_id or job id), status (approved|rejected), actor, at, note`. `approve(gate, target_id, actor, note)` writes an approval via `require_admin` and `audit_log`. `pending_gates(production)` returns which gates are unmet for the production's next move.

### 7.2 Enforcement at two points only

State transitions: `production.advance` and `/production/{id}/transition` call `governance.check` for the gates the target state requires (`review -> publish` needs `public_claim`, `client_sensitive` if any asset is client_confidential, and `external_publish`). Adapter spend: `media/adapters/gateway.py` calls `governance.check("paid_job", job_context)` before any `mcp:` call. Both call the single `governance.check`. No gate logic anywhere else.

### 7.3 Routes and UI

- `GET /governance/pending` (open approvals across productions) and `POST /governance/approve` (body `{gate, target_id, note}`) — `require_admin`.
- Surface pending gates on the Command Centre so the operator sees what is waiting and what was approved. Approving writes through `audit_log`.

### 7.4 Tests

A production cannot leave `review` without `public_claim` approved; approving unblocks it. A `mcp:` adapter call is refused until `paid_job` is approved. A `client_confidential` asset cannot publish without `client_sensitive`. Approvals are logged. Compile-sweep.

---

## Definition of done for each phase

Every new or changed Python file compiles (`python3 -m py_compile`). New SQLite tables create cleanly and round-trip. New routes are registered on the app. The state machine walks a production end to end in a test with agents stubbed. The Command Centre board renders the productions (or falls back to seed data without error). Governance blocks and unblocks as specified. After editing any file, verify it is intact on disk (OneDrive truncation hazard). Update `requirements.txt` if new deps are added (Remotion is node-side, not Python).

## Suggested order

Phase 5 first (it is the backbone the agents plug into), then Phase 7 (governance is small and the QA agent already references it), then Phase 6 (rendering is the largest external-dependency surface). Phases 5 + 7 give a working, governed content operation without rendering; add Phase 6 when the Remotion and adapter work is ready.
