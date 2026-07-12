# Multimodal Expansion: Architecture Specification

Version 1.0. June 2026. Status: for review before any code is written.

## The decision this document asks you to make

This spec turns the agentic RAG system into a multimedia production agency. It adds seven layers: an asset registry, multimodal ingestion, multimodal retrieval, a Content Studio agent team, a production pipeline, a rendering stack, and governance gates. Every layer reuses an existing pattern in the repo rather than inventing a parallel one. Read it, mark up what you disagree with, and tell me which build phase to start. No code ships until you sign off on the shape.

Three constraints are already fixed from your answers:

- Storage for the asset registry is SQLite, consistent with `data/harness.db` and `data/sessions.db`.
- The media backend is hybrid. Self-hosted pipelines (FFmpeg, Remotion, Whisper, a local visual embedder) are the backbone. The connected MCP services (Descript, Higgsfield, Canva, ElevenLabs) sit behind a single adapter interface, off by default, and every paid call passes a governance gate.
- This session produces the spec only. Phase 1 code follows on your word.

## How this fits what already exists

The system already has the spine a media agency needs. The plan plugs into it at known seams rather than rebuilding.

| New layer | Existing pattern it reuses | Seam |
|---|---|---|
| Media Asset Registry | SQLite stores in `data/`, `harness/store.py` | New `media/registry.py` library, REST on orchestrator |
| Multimodal ingestion | `media/whisper_pipeline.py` (8007), `media/video_pipeline.py` (8008), `orchestrator/uploads.py` | New ingestion gateway service, extends both pipelines |
| Multimodal retrieval | `rag/retriever.py`, `rag/schema.py` Chunk, Qdrant collections | New collections, extended Chunk fields |
| Content Studio agents | `orchestrator/wijerco_roster.py`, `wijerco_agent.py`, subagent files | New `content_studio` department, seven subagent files |
| Production pipeline | `orchestrator/dashboard.py` content board, `/content/pipeline` | New `productions` table, state machine, board columns |
| Rendering stack | `my-video/` Remotion project, `media/video_pipeline.py` | Remotion template families, render service |
| Governance | `common/security.py` (`require_admin`, `audit_log`, `backup_file`) | New gate table, gate checks on transitions |

Nothing here introduces a second auth model, a second notification path, or a second agent-invocation convention. The orchestrator stays the front door. Agents stay invokable through `/hybrid` with `force_route` and `subagent`. The Command Centre stays the operator view.

---

## Layer 1: Media Asset Registry

### Purpose

One table that knows about every media file the system has touched, where it came from, what rights attach to it, and which indexes point at it. Without this, retrieval and reuse are guesswork. The registry is the source of truth that the ingestion, retrieval, pipeline, and governance layers all read from.

### Storage

SQLite at `data/media.db`, opened through a small library at `media/registry.py`. Same engine and access style as `harness/store.py`. WAL mode on, so the ingestion services and the orchestrator can read concurrently while one writer appends.

### Schema

Table `assets`:

| column | type | notes |
|---|---|---|
| `asset_id` | TEXT PK | UUID4, generated at ingestion |
| `type` | TEXT | `audio` `video` `image` `slide_deck` `web_page` `document` |
| `path` | TEXT | absolute path under a confined media root |
| `source` | TEXT | `upload` `recording` `mcp:descript` `mcp:higgsfield` `mcp:canva` `web` `derived` |
| `created_at` | TEXT | ISO 8601 UTC |
| `duration` | REAL NULL | seconds, audio and video only |
| `dimensions` | TEXT NULL | `WxH`, image, video, slide thumbnails |
| `transcript_id` | TEXT NULL | FK to `transcripts.transcript_id` |
| `embedding_ids` | TEXT NULL | JSON array of Qdrant point ids, by collection |
| `rights` | TEXT | `owned` `licensed` `client_confidential` `third_party` `unknown` |
| `status` | TEXT | `ingesting` `ready` `quarantined` `failed` `archived` |
| `project` | TEXT NULL | engagement or content project slug |
| `tags` | TEXT NULL | JSON array |

Two companion tables keep the registry normalised:

- `transcripts`: `transcript_id` PK, `asset_id` FK, `language`, `segments` (JSON of `{start, end, text, speaker}`), `text`, `created_at`.
- `asset_links`: `asset_id`, `linked_asset_id`, `relation` (`derived_from`, `keyframe_of`, `audio_of`, `thumbnail_of`). This is how a keyframe knows its parent video and a clip knows its source recording.

### Lifecycle

An asset is created with `status = ingesting`, moves to `ready` once its pipeline finishes and indexes are written, or to `failed` on error and `quarantined` if governance flags it (unknown rights, client-confidential without approval). Nothing leaves the system in a `quarantined` state without an explicit admin release.

### REST surface (on the orchestrator, port 8000)

Reads are open to the same callers as the rest of the read API. Writes and deletes require `require_admin`, matching `/upload` and `/memory`.

- `GET /assets` with filters: `type`, `project`, `status`, `rights`, `tag`, `q` (free text over tags and transcript text).
- `GET /assets/{asset_id}` full record plus linked assets.
- `POST /assets/ingest` admin, hands a path or URL to the ingestion gateway.
- `PATCH /assets/{asset_id}` admin, edit tags, rights, project, status.
- `DELETE /assets/{asset_id}` admin, soft delete to `archived`, hard delete behind a second confirmation flag.

---

## Layer 2: Multimodal Ingestion

### Shape

One ingestion gateway, type-specific workers behind it. The gateway is a FastAPI service on port 8009. It detects the asset type, writes the `ingesting` registry row, dispatches to the right worker, collects results, writes indexes, and flips the row to `ready`. This mirrors how `orchestrator/uploads.py` already splits on file extension, just promoted to its own service because the media work is heavy and runs out of band.

Every worker writes files only under confined roots, using `confine_to_roots` from `common/security.py`, the same guard `whisper_pipeline.py` already applies. Media root defaults to `MEDIA_INPUT_ROOT`; derived files land under a `MEDIA_DERIVED_ROOT`.

### Workers

Audio. Extends the existing `whisper_pipeline.py` on 8007. Output gains a structured transcript with per-segment timestamps and a speaker field, written to the `transcripts` table rather than only to a Markdown file. The Markdown export stays for the vault.

Video. Extends `video_pipeline.py` on 8008. Four steps: extract audio to feed the audio worker, transcribe, sample keyframes (FFmpeg scene-change filter plus a fixed-interval fallback), and detect scene boundaries. Each keyframe becomes its own `image`-type asset linked back with `relation = keyframe_of`. Scene boundaries are stored on the video asset as `extra` timestamps.

Images. New worker. Three outputs: OCR text (Tesseract, self-hosted), a caption, and a visual embedding. Caption and embedding are where the hybrid choice bites. Self-hosted path: a local caption model and an open CLIP embedder. MCP path: Higgsfield or a vision model adapter. Default is self-hosted; the adapter swap is config, covered in Layer 6.

Slide decks. New worker. Pulls slide text, speaker notes, and a rendered thumbnail per slide. PPTX through `python-pptx`, PDF decks through the existing `pdf` skill path. Each slide is indexed as a text chunk carrying its slide number; thumbnails register as linked image assets.

Web pages. New worker. Cleaned main text (Readability-style extraction) plus a full-page screenshot. Screenshot capture uses the Claude-in-Chrome tools when a browser is available, with a headless fallback. Text is chunked and indexed; the screenshot registers as a linked image asset.

### Flow

```
POST /ingest (path|url, project?, rights?)
   -> registry row: status=ingesting
   -> type detect
   -> worker(s) run, may fan out to child assets (keyframes, thumbnails, screenshots)
   -> transcripts written, chunks + embeddings upserted to Qdrant
   -> embedding_ids + transcript_id written back to registry
   -> status=ready  (or quarantined if rights gate trips)
   -> notifier (8004) optional ping
```

---

## Layer 3: Multimodal Retrieval

### Principle

Separate indexes, one join key. Text, transcript-timestamp chunks, and visual embeddings live in different Qdrant collections because they have different vectors and different query intents. The `asset_id` on every payload is the join back to the registry, so a hit in any index can pull its full asset record and its siblings.

### Collections

| collection | vector | holds |
|---|---|---|
| `media_text` | 768, nomic-embed-text | OCR text, slide text and notes, cleaned web text, document text |
| `media_transcripts` | 768, nomic-embed-text | transcript chunks, each carrying `asset_id`, `start`, `end`, `speaker` |
| `media_visual` | CLIP dim (512 or 768) | image and keyframe embeddings |

The existing `obsidian_vault`, `wijerco_knowledge`, and `uploaded_docs` collections are untouched. Text media reuses the existing embedder and the dense, BM25, RRF, and rerank path already in `rag/retriever.py`. Only the visual collection needs a new embedder and a separate search call, since you cannot rerank an image against a text query with the current cross-encoder.

### Chunk schema extension

`rag/schema.py` Chunk gains optional fields, all defaulted so old payloads still load: `asset_id`, `media_type`, `t_start`, `t_end`, `speaker`, `thumbnail_path`. The provenance record in `source_ref()` gains the timestamp span so a citation can point at a moment, not just a file.

### Query intents it answers

- "Find the clip where I discuss X." Dense plus BM25 over `media_transcripts`, return `asset_id` plus `t_start` and `t_end`, so the answer links to a timestamped moment and the render layer can cut it.
- "Locate diagrams about Y." Text query embedded into the visual space over `media_visual`, filtered to `type in (image, slide)`, joined to the registry for the parent asset.
- "Reuse approved visual assets for Z." Registry filter first (`rights = owned`, `status = ready`, `project = Z`), then visual similarity within that set. Governance, not just relevance, scopes reuse.

### Orchestrator surface

- `POST /media/search` body `{query, modalities, filters, top_k}`. Returns ranked hits across the requested collections with registry records attached.
- The existing `/hybrid` rag node gains an optional media leg, so an agent answering a question can pull a clip or a diagram alongside text chunks.

---

## Layer 4: Content Studio Agent Layer

### Where it lives

A new department, `content_studio`, added to `orchestrator/wijerco_roster.py` next to the existing six. Seven agents, each backed by a subagent file at `AGENTS/subagents/{slug}.md` in the WijerCo folder, loaded as the role layer exactly as `wijerco_agent.py` already builds prompts (about-me, anti-AI-style, my-company, then the department and role layer, then retrieved context). They are invoked through the existing `/hybrid` route with `force_route: content_studio` and a `subagent` slug. No new invocation path.

### The seven roles

| persona | slug | role | output contract |
|---|---|---|---|
| Brief | `brief-builder` | Brief Builder | Goal in, structured brief out: audience, format, angle, key message, call to action, constraints |
| Sera | `research-producer` | Research Producer | Evidence pack: claims with named sources, numbers, dates, pulled via the RAG and media-search legs |
| Scout | `scriptwriter` | Scriptwriter | Script keyed to the format, with section or scene markers and runtime estimate |
| Bree | `storyboarder` | Storyboarder | Script mapped to scenes, each scene naming the asset to use or generate |
| Vidal | `visual-director` | Visual Director | Generation briefs for each missing visual: subject, style, aspect, source references |
| Cutter | `editor` | Editor | Captions, cut list against transcript timestamps, format variants |
| Quill-Q | `qa-brand-reviewer` | QA / Brand Reviewer | Pass or fail against voice, claim-evidence, accessibility, and rights, with specific fixes |

Personas avoid clashing with the existing roster (Quincy already reviews; the studio reviewer is distinct and brand-and-rights-scoped).

### Agent contracts

Each agent reads and writes a single shared production record (Layer 5), so the chain is inspectable and resumable. Brief Builder writes the brief; Research Producer appends evidence; Scriptwriter reads both and writes the script; and so on. The QA / Brand Reviewer is the only agent that can move a production from `review` toward `publish`, and only after the governance gates clear. This makes the QA agent the structural checkpoint, not an optional pass.

---

## Layer 5: Production Pipeline

### States

```
idea -> brief -> research -> outline -> draft -> asset_plan -> render -> review -> publish -> measure
```

Forward by default. Backward allowed (review can send a draft back) and logged. Each transition records who moved it, when, and why, in an `events` table, reusing the decision-log habit already in `orchestrator/decision_log.py`.

### Storage

Table `productions` in `data/media.db`: `production_id`, `title`, `project`, `format`, `state`, `brief` (JSON), `script` (JSON), `asset_plan` (JSON), `linked_assets` (JSON of `asset_id`), `gates` (JSON of gate status), `owner`, `created_at`, `updated_at`. Companion `production_events` for the transition log.

### Orchestrator surface

- `GET /production` and `GET /production/{id}`.
- `POST /production` create from a goal, lands in `idea`.
- `POST /production/{id}/advance` runs the right Content Studio agent for the next state, writes its output, and moves the state if no gate blocks.
- `POST /production/{id}/transition` manual move, admin, for overrides.

### n8n and Command Centre

n8n gets workflow 13, `content-studio-pipeline.json`, modelled on the existing `6-content-pipeline.json`: a trigger, then sequential `POST /production/{id}/advance` calls, pausing at any state whose gate is unmet and pinging the notifier so you can approve in the Command Centre.

The Command Centre content board already renders columns from `/content/pipeline`. The production states map onto a board: `idea` and `brief` group as Ideas, `research` through `draft` as Drafting, `asset_plan` and `render` as In Production, `review` as Review, `publish` and `measure` as Published. A new `GET /production/board` returns the same envelope shape the board already consumes, so the UI change is data, not rewrite.

---

## Layer 6: Rendering Stack and the Hybrid Adapter

### Remotion template families

Remotion stays for repeatable, templated video. The existing `my-video/` project becomes the home for six template families, each a parametrised composition driven by a JSON props file the pipeline writes:

1. LinkedIn short video.
2. Explainer carousel.
3. Talking-head transcript clip, cut against `media_transcripts` timestamps.
4. Policy briefing video.
5. Course or module teaser.
6. Client proposal walkthrough.

A render service wraps `npx remotion render`, takes a template id plus props, writes the output as a `derived` asset, and links it to its source production.

### The adapter interface

This is the core of the hybrid choice. A thin set of interfaces in `media/adapters/`, each with a self-hosted implementation and an MCP implementation, selected by config:

```
Transcriber      -> self: faster-whisper      | mcp: descript
ImageGenerator   -> self: local model         | mcp: higgsfield, canva
VideoGenerator   -> self: remotion + ffmpeg   | mcp: higgsfield, descript
AudioGenerator   -> self: (none, stub)        | mcp: elevenlabs
VisualEmbedder   -> self: open clip           | mcp: vision adapter
```

Selection is per capability through env, for example `ADAPTER_IMAGE=self` or `ADAPTER_IMAGE=mcp:higgsfield`. Default every capability to `self`. The pipeline calls the interface, never the MCP directly, so swapping a backend is a config change and the agents stay unaware of which engine ran.

Every MCP adapter call routes through one chokepoint, `media/adapters/gateway.py`, which checks the relevant governance gate before spending. A self-hosted call skips the paid gate but still respects the rights and publication gates.

---

## Layer 7: Governance

### Gates

Five gates, each a named check that blocks a state transition or an adapter call until satisfied:

| gate | blocks | cleared by |
|---|---|---|
| `public_claim` | `review -> publish` | QA agent verifies each claim has a named source; admin confirms |
| `generated_image` | use of any generated image in a render | admin reviews the image, sets rights |
| `client_sensitive` | publish or external share of `client_confidential` assets | admin approval, logged |
| `paid_job` | any `mcp:` adapter call with a cost | admin approval, optional budget ceiling |
| `external_publish` | the `publish` transition itself | admin sign-off |

### Enforcement

Gates are enforced in two places only, so there is one rule, not many. State transitions check gates in `POST /production/{id}/advance` and `/transition`. Adapter spend checks the `paid_job` gate in `media/adapters/gateway.py`. Both call a single `governance.check(gate, context)` function. Approvals are recorded through the existing `audit_log` in `common/security.py`, and admin actions require `require_admin`, the same dependency `/upload` and `/memory` already use. Approving a gate writes an entry the Command Centre can show, so the operator sees what is waiting and what was approved.

### What this prevents

No generated image reaches a published asset without a rights decision. No client-confidential file leaves the system without sign-off. No paid render or generation runs without an explicit yes. No external publication happens automatically. The gates are the difference between a media tool and a media operation you would trust with a client's name on it.

---

## New and changed files

New:

```
media/registry.py                  asset + transcript + link store (SQLite)
media/ingest/gateway.py            ingestion service (8009)
media/ingest/images.py             OCR, caption, visual embed
media/ingest/slides.py             text, notes, thumbnails
media/ingest/web.py                clean text + screenshot
media/adapters/base.py             interface definitions
media/adapters/selfhosted.py       whisper, ffmpeg, remotion, clip
media/adapters/mcp.py              descript, higgsfield, canva, elevenlabs
media/adapters/gateway.py          paid-gate chokepoint
orchestrator/production.py         state machine + events
orchestrator/governance.py         gate checks
n8n-workflows/13-content-studio-pipeline.json
AGENTS/subagents/brief-builder.md  (and six siblings, in the WijerCo folder)
```

Changed:

```
media/whisper_pipeline.py          structured timestamped transcripts to registry
media/video_pipeline.py            keyframes + scene boundaries + child assets
rag/schema.py                      Chunk gains asset_id, media_type, t_start, t_end, speaker, thumbnail_path
rag/retriever.py                   visual search call + media collections
orchestrator/wijerco_roster.py     content_studio department, seven agents
orchestrator/main.py               /assets, /media/search, /production routes
ui/command_centre.html             production board view
docker-compose.yml                 ingestion gateway service, optional Tesseract
.env.example                       MEDIA_INPUT_ROOT, MEDIA_DERIVED_ROOT, ADAPTER_* , gate budgets
```

---

## Build sequence

Each phase ends with something that runs and is tested. Later phases depend on earlier ones, so the order is not arbitrary.

Phase 1, foundation. Registry (`media/registry.py`, `data/media.db`), the `/assets` routes, and the audio and image ingestion workers writing real rows. Deliverable: you can ingest a recording and an image and see them in the registry with transcripts and tags. Depends on nothing new.

Phase 2, retrieval. Media collections, the Chunk extension, visual embedder, and `/media/search`. Deliverable: "find the clip where I discuss X" and "locate diagrams about Y" return real hits with timestamps. Depends on Phase 1.

Phase 3, video and slides and web. The heavier ingestion workers and keyframe and scene handling. Deliverable: a full video ingests into searchable transcript moments and keyframes. Depends on Phases 1 and 2.

Phase 4, Content Studio agents. The seven subagent files, the `content_studio` department, and the agent contracts against a stubbed production record. Deliverable: each agent runs through `/hybrid` and produces its part. Depends on Phase 2 for the research and media legs.

Phase 5, production pipeline. The `productions` table, the state machine, the advance and transition routes, and the Command Centre board. Deliverable: a production walks from idea to draft, visible on the board. Depends on Phase 4.

Phase 6, rendering and adapters. Remotion template families, the render service, and the adapter interface with self-hosted implementations, MCP implementations behind config. Deliverable: a production renders a LinkedIn short and a transcript clip self-hosted, with an MCP path available. Depends on Phase 5.

Phase 7, governance. The gate table, the gate checks at the two chokepoints, the approval surface, and n8n workflow 13. Deliverable: nothing publishes or spends without a logged approval. Depends on Phases 5 and 6.

A reasonable cut for a first usable agency is Phases 1, 2, 4, and 5: ingest, search, the agent team, and the pipeline, self-hosted only, governance deferred to manual review. That gives you a working content operation without the rendering and paid-adapter surface.

---

## Open decisions for you

1. Visual embedder. Self-hosted open CLIP adds a model dependency and some setup. The alternative is to defer visual similarity and ship text and transcript search first, adding `media_visual` in a later phase. Which matters more early: searching diagrams by content, or shipping sooner?

2. Web screenshots. Browser-based capture needs the Chrome tools live at ingestion time. Acceptable, or should web ingestion be text-only at first?

3. Audio generation. The only capability with no self-hosted path is voice. If you want narrated video without an ElevenLabs call, the talking-head and briefing templates stay caption-only until that gate is approved. Confirm that is fine.

4. Persona names. Seven new handles are proposed. Tell me if any clash with how you think about the existing roster, and I will rename before Phase 4.
