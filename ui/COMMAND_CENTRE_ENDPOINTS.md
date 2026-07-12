# Command Centre — new page endpoints

Eight new pages each call one GET endpoint on mount. Until the endpoint exists the page renders built-in seed data, so you can add these one at a time and the UI keeps working.

**Now implemented** in `orchestrator/dashboard.py`, mounted on the existing app. All eight sources are authored in the Obsidian vault, one note per item.

Vault layout, under `OBSIDIAN_VAULT_PATH/13_Command Centre/`:

| Folder | Endpoint | Grouping |
|--------|----------|----------|
| `Deliverables/` | `/deliverables` | flat list |
| `Content Pipeline/` | `/content/pipeline` | legacy fallback only |
| `Engagements/` | `/engagements` | flat list |
| `Sector Intel/` | `/intel/feed` | flat list |
| `Scheduled Runs/` | `/schedule/list` | flat list |
| — (retired: KB reads Qdrant, not notes) | `/kb/overview` | live corpus stats |
| `Memory/` | `/memory/overview` | by frontmatter `g` |
| `Routing/` | `/trace/recent` | flat list |

Each note's YAML frontmatter holds the fields below; the note body is free text. To manage content, add, edit, or delete notes in Obsidian. The field names in the tables that follow are exactly the frontmatter keys. If the vault or a folder is missing, the endpoint returns built-in seed data so no page errors.

Example note (`Deliverables/Online retention sector benchmark and drivers.md`):

```yaml
---
title: "Online retention: sector benchmark and drivers"
cap: "Sector Intelligence & Evidence"
type: "PDF briefing"
status: "Reviewed"
st: "st-good"
meta: "18 pages · 12 Jun"
---
```

Notes: Content now prefers `09_Writing Pipline/` in the vault, one Markdown file per piece. Set `WRITING_PIPELINE_PATH` to override this path. `13_Command Centre/Content Pipeline` remains a legacy fallback. `Memory` notes use `g` (group) plus a single `item` string. `Knowledge Base/` notes are no longer read: `/kb/overview` now measures the live Qdrant collections directly (see section 4).

## Conventions

- Base URL is the orchestrator the UI already talks to: `http://localhost:8000` (the `API` constant in `command_centre.html`).
- Every endpoint is `GET` and returns JSON with status `200`. Any non-200 or network error makes the page fall back to seed data.
- List endpoints accept either `{"items": [...]}` or a bare array `[...]`. The exception is noted per endpoint.
- `st` and `stl` are presentation fields. `st` is a status colour class, `stl` is the human label. See [Status fields](#status-fields) for a cleaner option if you would rather not return CSS classes.

Status colour classes in use: `st-good` (pine), `st-warn` (gold), `st-mute` (grey), `st-fail` (red).

---

## 1. Deliverables — `GET /deliverables`

Backs the Deliverables page. Finished, reviewed outputs the client can take away. Source: your reviewed-artifact store.

Envelope: `{"items": [...]}` or bare array.

| field | type | example | notes |
|-------|------|---------|-------|
| `title` | string | `"Online retention: sector benchmark and drivers"` | Deliverable name |
| `cap` | string | `"Sector Intelligence & Evidence"` | Capability or department |
| `type` | string | `"PDF briefing"` | Artifact type |
| `status` | string | `"Reviewed"` | Status label |
| `st` | string | `"st-good"` | Status colour class |
| `meta` | string | `"18 pages · 12 Jun"` | Free-text sub-line |

```json
{ "items": [
  { "title": "Online retention: sector benchmark and drivers",
    "cap": "Sector Intelligence & Evidence",
    "type": "PDF briefing", "status": "Reviewed",
    "st": "st-good", "meta": "18 pages · 12 Jun" }
] }
```

---

## 2. Content pipeline — `GET /content/pipeline`

Backs the Content command centre. Source: the Obsidian vault writing pipeline.

Preferred vault layout:

```text
09_Writing Pipline/
  00_Ideas/    # Build Content Brief + Find Evidence
  01_Drafts/   # Draft the piece
  02_Editing/  # Tighten Voice
  03_Ready/    # Run QA Review
  04_Published/# Publish record
```

Each piece of content is a single `.md` file. The Command Centre reads the file frontmatter into a card, appends agent outputs into the same file, and moves that file between folders as actions are executed.

Envelope: a single object keyed by the five column names, in this exact spelling: `Ideas`, `Drafts`, `Editing`, `Ready`, `Published`. Each value is an array of cards.

| field | type | example | notes |
|-------|------|---------|-------|
| `t` | string | `"What AI agents already do in academic operations"` | Post title |
| `p` | string | `"LinkedIn"` | Platform |
| `m` | string (optional) | `"draft 2"` or `"QA ready"` | Draft state or review cue |
| `path` | string (optional) | `"09_Writing Pipline/00_Ideas/example.md"` | Vault-relative Markdown path |
| `pillar` | string (optional) | `"AI in academic operations"` | Strategic content pillar |
| `audience` | string (optional) | `"Academic leaders"` | Target audience |
| `intent` | string (optional) | `"Educate"` | Content intent |
| `format` | string (optional) | `"LinkedIn post"` | Recommended or current format |
| `priority` | number (optional) | `88` | Base priority score, 0 to 100 |
| `confidence` | string (optional) | `"High"` | System confidence |
| `effort` | string (optional) | `"Low"` | Effort estimate |
| `next_action` | string (optional) | `"Tighten the hook."` | Recommended next action |
| `signal` | string (optional) | `"Ready to sharpen"` | Short card-level signal |
| `source` | string (optional) | `"Sector intel"` | Source material or origin |
| `evidence` | string (optional) | `"Strong"` | Evidence status |
| `due` | string (optional) | `"This week"` | Operating rhythm cue |
| `views` | string (optional) | `"3.1k views"` | Published performance signal |
| `engagement` | string (optional) | `"Strong"` | Qualitative performance signal |

```json
{
  "Ideas": [
    {
      "t": "The micro-credential demand gap nobody costs",
      "p": "Article",
      "pillar": "Micro-credentials and workforce demand",
      "audience": "University executives",
      "intent": "Educate",
      "priority": 91,
      "next_action": "Build the cost model and add a concrete workforce-demand example.",
      "signal": "Strategic gap",
      "evidence": "Partial"
    }
  ],
  "Drafts": [
    {
      "t": "Adaptive versus technical",
      "p": "LinkedIn",
      "m": "draft 2",
      "pillar": "AI in academic operations",
      "signal": "Ready to sharpen"
    }
  ],
  "Editing": [ { "t": "What AI agents already do", "p": "LinkedIn", "m": "voice pass" } ],
  "Ready": [ { "t": "Andragogy is not a footnote", "p": "Article", "m": "QA ready" } ],
  "Published": [ { "t": "The published piece", "p": "Article", "m": "Published" } ]
}
```

### Execute a content action — `POST /content/action`

Runs a named WijerCo agent, appends the actual generated output to the relevant Markdown file, verifies the text is present in the file, and moves the file to the mapped folder. A successful response includes `"persisted": true`.

| action | agent intent | target folder |
|--------|--------------|---------------|
| `brief` | Build Content Brief | `09_Writing Pipline/00_Ideas` |
| `evidence` | Find Evidence | `09_Writing Pipline/00_Ideas` |
| `draft` | Draft the piece | `09_Writing Pipline/01_Drafts` |
| `voice` | Tighten Voice | `09_Writing Pipline/02_Editing` |
| `review` | Run QA Review | `09_Writing Pipline/03_Ready` |
| `publish` | Publish | `09_Writing Pipline/04_Published` |

Request:

```json
{
  "action": "draft",
  "query": "Agent prompt built by the UI",
  "department": "marketing_sales",
  "subagent": "content-creator",
  "item": {
    "t": "The micro-credential demand gap nobody costs",
    "path": "09_Writing Pipline/00_Ideas/the-micro-credential-demand-gap-nobody-costs.md"
  }
}
```

### Create a writing idea — `POST /content/ideas`

Creates a new Markdown file in `09_Writing Pipline/00_Ideas`.

```json
{
  "title": "Why academic AI adoption keeps stalling",
  "p": "LinkedIn",
  "pillar": "AI in academic operations",
  "audience": "Academic leaders",
  "notes": "Start with operating model, not tool adoption."
}
```

### Generate writing ideas from sector intel — `POST /content/ideas/from-intel`

Uses the Sector Intel feed to generate new article ideas and creates one Markdown file per idea in `09_Writing Pipline/00_Ideas`.

```json
{
  "count": 3,
  "p": "Article",
  "pillar": "AI in academic operations",
  "audience": "Academic leaders",
  "source_query": "TEQSA",
  "use_agent": true
}
```

### Run an assisted content action — `POST /content/assist`

Reads the selected article Markdown, asks an agent for an actionable improvement plan, appends that plan to the same file, and verifies persistence. Use this for actions such as `improve_hook`, `voice_check`, `resolve_gaps`, and `prepare_qa`.

```json
{
  "action": "improve_hook",
  "label": "Improve hook",
  "detail": "Open with the tension, not the topic.",
  "item": {
    "t": "Article 1-26",
    "path": "09_Writing Pipline/01_Drafts/Article 1-26.md"
  }
}
```

Response:

```json
{
  "ok": true,
  "path": "09_Writing Pipline/00_Ideas/why-academic-ai-adoption-keeps-stalling.md"
}
```

---

## 3. Engagements — `GET /engagements`

Backs the Engagements page. Live client work with progress. Source: your project records.

Envelope: `{"items": [...]}` or bare array.

| field | type | example | notes |
|-------|------|---------|-------|
| `cap` | string | `"Learning & Curriculum Design"` | Capability |
| `title` | string | `"Master of Public Health, rebuilt for online delivery"` | Engagement title |
| `lead` | string | `"Instructional Designer"` | Lead advisor |
| `milestone` | string | `"Curriculum architecture, draft in 6 days"` | Next milestone |
| `st` | string | `"st-good"` | Status colour class |
| `stl` | string | `"In delivery"` | Status label |
| `pct` | integer | `55` | Percent complete, 0 to 100 |

```json
{ "items": [
  { "cap": "Learning & Curriculum Design",
    "title": "Master of Public Health, rebuilt for online delivery",
    "lead": "Instructional Designer",
    "milestone": "Curriculum architecture, draft in 6 days",
    "st": "st-good", "stl": "In delivery", "pct": 55 }
] }
```

---

## 4. Knowledge base — `GET /kb/overview` and friends

Backs the Knowledge base page. As of July 2026 every number is measured, none typed in.

`GET /kb/overview` queries Qdrant directly across three collections (`QDRANT_COLLECTION`, `WIJERCO_COLLECTION`, `UPLOADS_COLLECTION`). `stats.docs` = distinct files in the index, `stats.chunks` = exact point count, `stats.updated` = timestamp of the last index run (written by `rag/indexer.py` to `data/kb_index_runs.jsonl`). Source rows are derived by grouping indexed file paths by top-level folder; freshness is computed from the newest `modified_at` per group (`KB_FRESH_DAYS`=30, `KB_STALE_DAYS`=90, both env-overridable). Responses are cached for `KB_CACHE_TTL` seconds (default 300).

If Qdrant is unreachable the endpoint returns seed data with `"demo": true` and the UI shows a demo-data banner.

```json
{
  "stats": { "docs": 128, "chunks": 3400, "updated": "12 Jul 2026, 09:14" },
  "collections": [
    { "collection": "wijerco_knowledge", "label": "WijerCo", "docs": 41, "chunks": 880,
      "last_indexed": "2026-07-12T09:14:02+00:00", "last_indexed_label": "12 Jul 2026, 09:14" }
  ],
  "sources": [
    { "name": "KNOWLEDGE BASE", "collection": "wijerco_knowledge", "collection_label": "WijerCo",
      "docs": 5, "chunks": 210, "newest": "2026-07-01T04:11:00+00:00",
      "fresh": "Current", "st": "st-good", "age_days": 11 }
  ],
  "demo": false
}
```

Companion endpoints:

| endpoint | method | notes |
|----------|--------|-------|
| `/kb/source/{name}?collection=` | GET | Drill-in: files behind one source row, with chunk counts and modified dates |
| `/kb/reindex` | POST (admin) | `{"target": "vault"\|"wijerco"}` — proxies to the indexer service at `INDEXER_URL` |
| `/kb/quality` | GET | Last stored recall run (`data/kb_quality.json`) |
| `/kb/quality/run` | POST (admin) | Runs `harness/recall_set.py` cases against `wijerco_knowledge`, returns and stores recall@5 |
| `/kb/misses` | GET | Recent queries with no good retrieval hit, logged by `rag/retriever.py` to `data/kb_misses.jsonl` |

## 5. Memory — `GET /memory/overview`

Backs the Memory page. The facts and preferences carried between conversations. Uses its own path because the existing `GET /memory` is a semantic search that requires a `q` param. Source: notes in `Memory/`, each with a `g` (group) and a single `item` string; the endpoint groups them by `g`.

Envelope: `{"groups": [...]}` or bare array.

| field | type | example | notes |
|-------|------|---------|-------|
| `g` | string | `"Preferences"` | Category name |
| `items` | string[] | `["Lead with the point", "No em dashes"]` | Remembered facts |

```json
{ "groups": [
  { "g": "Identity", "items": [
      "Aaron Wijeratne, Academic Director at OES",
      "Positioning for PVC or Academic Dean on a 12-month horizon" ] },
  { "g": "Preferences", "items": [
      "Lead with the point; no preamble",
      "Claims backed by names, numbers, and dates" ] }
] }
```

---

## 6. Sector intel — `GET /intel/feed`

Backs the Sector intel page. Source: your research and intelligence agent's monitoring.

Envelope: `{"items": [...]}` or bare array.

| field | type | example | notes |
|-------|------|---------|-------|
| `src` | string | `"TEQSA"` | Source label |
| `date` | string | `"11 Jun"` | Date, free text |
| `head` | string | `"Consultation opens on teaching-qualification expectations"` | Headline |
| `so` | string | `"The forcing function you have been writing about."` | The so-what line |

```json
{ "items": [
  { "src": "TEQSA", "date": "11 Jun",
    "head": "Consultation opens on teaching-qualification expectations for academic staff",
    "so": "The forcing function you have been writing about." }
] }
```

---

## 7. Routing inspector — `GET /trace/recent`

Backs the Routing page. Why each query went where it did. Source: your routing or observability log.

Envelope: `{"items": [...]}` or bare array.

| field | type | example | notes |
|-------|------|---------|-------|
| `q` | string | `"Draft a proposal for a micro-credential in data literacy"` | The query |
| `dept` | string | `"Learning & Curriculum Design"` | Routed agent or department |
| `model` | string | `"claude-opus-4.8"` | Model used |
| `conf` | integer | `91` | Match confidence percent |
| `sources` | integer | `6` | Retrieval sources used |
| `ms` | integer | `4200` | Latency in milliseconds |
| `cost` | number | `0.021` | Cost in USD |

```json
{ "items": [
  { "q": "Draft a proposal for a micro-credential in data literacy",
    "dept": "Learning & Curriculum Design", "model": "claude-opus-4.8",
    "conf": 91, "sources": 6, "ms": 4200, "cost": 0.021 }
] }
```

`ms` and `cost` are formatted by the existing `fmtMs` and `fmtCost` helpers, so send raw milliseconds and raw dollars. `conf` and `sources` are optional; the page renders them only when present.

---

## 8. Scheduled runs — `GET /schedule/list`

Backs the Scheduled runs page. Source: your scheduled-tasks registry plus last-run results.

Envelope: `{"items": [...]}` or bare array.

| field | type | example | notes |
|-------|------|---------|-------|
| `name` | string | `"Morning email triage"` | Task name |
| `when` | string | `"Daily · 7:00am"` | Schedule, free text |
| `next` | string | `"Tomorrow 7:00am"` | Next run, free text |
| `st` | string | `"st-good"` | Status colour class |
| `stl` | string | `"OK"` | Last result label |

```json
{ "items": [
  { "name": "Morning email triage", "when": "Daily · 7:00am",
    "next": "Tomorrow 7:00am", "st": "st-good", "stl": "OK" },
  { "name": "KB freshness check", "when": "Daily · 11:00pm",
    "next": "Tonight 11:00pm", "st": "st-fail", "stl": "Failed" }
] }
```

---

## Status fields

The pages read `st` directly as a CSS class, which means the backend currently has to know about UI colours. If you would rather keep the API clean, return a semantic status and map it on the frontend.

Return, for example, `"status": "reviewed" | "draft" | "scheduled" | "failed" | "ok"`, then add one mapping in `command_centre.html`:

```js
const ST = { reviewed:'st-good', ok:'st-good', current:'st-good',
             draft:'st-warn', scheduled:'st-mute', failed:'st-fail' };
// usage: className={"stag "+(ST[d.status]||'st-mute')}
```

That removes `st` from the contract and leaves you returning only `status` plus its label.

---

## FastAPI sketch

One endpoint, to anchor the pattern. The rest follow the same shape.

```python
@app.get("/deliverables")
def deliverables():
    return {"items": [
        {"title": "...", "cap": "...", "type": "PDF briefing",
         "status": "Reviewed", "st": "st-good", "meta": "18 pages · 12 Jun"},
    ]}
```

Wire these to your real stores in any order. Each page picks up live data the moment its endpoint returns 200, and falls back to seed data until then.
