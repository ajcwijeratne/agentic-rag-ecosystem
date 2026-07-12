# System Effectiveness Plan

Status: draft for approval. Nothing in here is built yet. Approve or amend, then I implement in the phase order at the end.

The recommendation up front: do the retrieval core first (items 1, 2, 4). They share one data structure, the provenance-carrying chunk, and they deliver the largest answer-quality gain. Routing, testing, observability, and reliability build on top of that core and can follow in order.

A single design decision threads through items 1, 2, 4, and 6: define one chunk schema and one per-request trace schema, then make every layer read and write them. Most of the work below is plumbing those two schemas through code that already exists but is bypassed or thin.

---

## Item 1: Unify retrieval

### Current state

`orchestrator/graph.py` `rag_node` (lines 59-85) calls three sub-agents over HTTP `POST /retrieve` and merges their chunks. The local agent's `retrieve` (`agents/local_data_agent.py` lines 110-128) runs dense-only Qdrant search. The hybrid pipeline in `rag/retriever.py` (BM25, dense, RRF, cross-encoder rerank) runs only as its own FastAPI service on port 8006 and is never called by the graph. So normal queries get dense-only local search, no BM25, no RRF, no rerank.

### Target

Make `rag/retriever.search()` the default local retrieval path. The local data agent calls it directly in-process rather than running a second dense-only search.

### Changes

`agents/local_data_agent.py`
- Replace the body of `retrieve(query, top_k)` so it calls `rag.retriever.search(query, top_k, use_reranker=True)` instead of the local `_embed` + `_qdrant_search`. Keep the existing dense path as a fallback inside a try/except so the agent still answers if `rag_bm25` or the reranker is missing.
- Keep `_embed` and `_qdrant_search` for that fallback. No endpoint signature changes, so `rag_node` needs no change in this item.

`rag/retriever.py`
- `search()` already returns the merged, reranked list. Add a `collection` parameter (default `QDRANT_COLLECTION`) so the same function can serve the vault, `wijerco_knowledge`, and `uploaded_docs` collections. Thread it into `_dense_search`.
- Today the BM25 corpus is a single global rebuilt per index run (lines 35-46). Multiple collections overwrite each other. Change `_bm25_corpus`, `_bm25_payloads`, `_bm25_model` into per-collection dicts keyed by collection name. `update_bm25_corpus(texts, payloads, collection)` writes the right bucket; `_bm25_search(query, top_k, collection)` reads it. This is required for hybrid search to work across the three collections rather than only the last one indexed.

`rag/indexer.py` and `orchestrator/uploads.py`
- Pass the collection name into `update_bm25_corpus` (indexer lines 150-155, 260-264; uploads lines 148-153). Indexer currently passes WijerCo and vault corpora into the same global, so they clobber each other. This is a real bug the per-collection change fixes.

### Risk

The cross-encoder loads `sentence-transformers` and a model on first call. In-process load inside the agent adds startup latency and memory. Mitigate by lazy-loading on first query (already how `reranker._load_encoder` works) and by an env flag `RETRIEVER_USE_RERANKER` so it can be disabled on memory-constrained runs.

---

## Item 2: Source quality controls and chunk provenance

### Current state

Chunks carry only `text`, `file`, `score`, plus `source_agent` added in `rag_node` and `_rerank_score` / `_rrf_score` added inside the retriever. Qdrant payloads written by the indexer are `{file, text}` for the vault (lines 128-129) and `{file, text, source}` for WijerCo and uploads. No `collection`, `section`, `modified_at`, `chunk_id`. The final payload in `synthesize_node` reports `context_count` but no per-source detail and no citations.

### Target

Every chunk carries full provenance from index time to final answer. Every answer carries source IDs, freshness, score, retrieval mode, and citations.

### Changes

New file `rag/schema.py`
- One dataclass, `Chunk`, with: `text`, `collection`, `file`, `section`, `modified_at` (ISO 8601), `chunk_id`, `score`, `rerank_score`, `source` (vault / wijerco / upload / web / cloud), `source_agent`, `retrieval_mode` (dense / bm25 / rrf / rerank). A `to_dict()` and `from_qdrant_payload()` helper. Defaults make every field optional so old payloads still load.

`rag/indexer.py`
- At chunk creation (lines 126-141 for vault, 227-246 for WijerCo) write the full payload: add `section` (nearest preceding Markdown heading), `modified_at` (`md_file.stat().st_mtime` as ISO), `chunk_id` (the deterministic `uuid5` already computed for the point id), and `collection`. Chunking moves from `chunk_text` returning strings to returning `(text, section)` pairs so the section heading travels with the chunk.

`rag/retriever.py`
- `_dense_search` (lines 122-130) and `_bm25_search` (lines 148-159) populate `Chunk` fields from the payload and tag `retrieval_mode`. `_rrf_merge` and `rerank` set `retrieval_mode` to `rrf` / `rerank` and keep `score` and `rerank_score` distinct.

`orchestrator/graph.py`
- `synthesize_node` (lines 164-190) adds a `sources` list to the payload: one entry per chunk that survived into the prompt, with `chunk_id`, `file`, `section`, `collection`, `modified_at`, `score`, `rerank_score`, `retrieval_mode`, `source_agent`. Adds a `citations` list mapping the bracketed markers in the answer (`[1]`, `[2]`) to those source entries. Citation markers come from the prompt template in item 4.

`orchestrator/main.py`
- `QueryResponse` (lines 96-110) gains `sources: list[dict]` and `citations: list[dict]`. `run_query` passes them through.

### Risk

Re-indexing is required for existing collections to gain the new fields. The `from_qdrant_payload` defaults mean un-reindexed chunks still work, just with empty `section` / `modified_at`. Call out a one-time re-index in the runbook.

---

## Item 3: Improve routing

### Current state

Three separate routers. `orchestrator/router.py` decides local vs cloud on token count and a keyword list (lines 33-38). `token_optimizer.classify_task` picks one of eight task types on keyword match (lines 27-64). `wijerco_router.classify_intent` picks a department. None report a confidence score, none handle ambiguity, none log their decisions, none are tuned against data.

### Target

A classifier layer that returns a label plus a confidence score, defers to a safe default when confidence is low or two labels are close, and logs every decision so the choices can be tuned against the eval set rather than by editing keyword lists.

### Changes

New file `orchestrator/classifier.py`
- `classify(query) -> ClassificationResult` with `task_type`, `confidence` (0..1), `runner_up`, `margin` (top minus runner-up), `method` (heuristic / embedding), and `decided_by`. Start with a transparent scored model: each task type accumulates weighted keyword hits, scores are softmaxed into a confidence. This keeps it dependency-free and explainable, and it gives a real confidence number rather than first-match-wins.
- Thresholds via env: `ROUTER_MIN_CONFIDENCE` (default 0.45) and `ROUTER_MIN_MARGIN` (default 0.15). Below either, fall back to the safe default (`advisory` for task type, `local` for backend) and set `decided_by="low_confidence_default"`.
- Optional second stage behind a flag `ROUTER_USE_EMBEDDING`: cosine similarity of the query embedding against per-label centroid vectors built from the eval tasks. Off by default so there is no new hard dependency; on when tuned centroids exist.

`orchestrator/router.py`
- `route_query` calls `classifier.classify` and keeps producing a `RoutingDecision`, now with the confidence and runner-up folded into `reason`. Behaviour stays backward compatible; the keyword list becomes a fallback, not the primary path.

`orchestrator/state.py`
- `RoutingDecision` (lines 18-31) gains `confidence: float`, `runner_up: str | None`, `decided_by: str`.

New file `orchestrator/decision_log.py`
- `log_decision(kind, query, result)` appends one JSON line to `logs/routing_decisions.jsonl`: timestamp, query preview, chosen label, confidence, margin, runner-up, method. `router.py`, `classifier.py`, and `wijerco_router.classify_intent` all log through it.

Tuning, not a code change but a deliverable
- A script `scripts/tune_router.py` replays `harness/eval_suite.py` TASKS through the classifier, prints a confusion matrix of predicted task type against the task's department-implied type, and reports accuracy and low-confidence rate. This is how thresholds and keyword weights get set from data. It runs without live services.

### Risk

Department-implied task type is a proxy label, not ground truth. The tuning script measures self-consistency and threshold behaviour, not absolute correctness, until a small hand-labelled set exists. Note that limit; add a labelled set later.

---

## Item 4: Context assembly

### Current state

`graph.py` `llm_node` (lines 97-110) concatenates up to 10 chunks in arrival order. No deduplication, no source diversity, no recency weighting, no compression, no citation template. Duplicate or near-duplicate chunks from overlapping windows waste the context budget and bias the answer.

### Target

A deliberate assembly step: dedupe, diversify across sources, weight by recency, compress, and render with a citation-aware template.

### Changes

New file `orchestrator/context_assembler.py`
- `assemble(chunks, query, max_chunks, token_budget) -> AssembledContext` returning the selected chunks (with their citation index) and the rendered context string. Stages, in order:
  - Dedupe: drop chunks whose normalised text overlaps an already-kept chunk above a Jaccard threshold (handles the indexer's 50-word overlap windows). Env `CONTEXT_DEDUPE_THRESHOLD` default 0.85.
  - Diversity: cap chunks per `file` and per `collection` so one note cannot fill the whole budget. Env `CONTEXT_MAX_PER_FILE` default 3.
  - Recency: blend `rerank_score` with a freshness factor from `modified_at` using `final = rerank_score * (1 + w * recency)`, `w` from `CONTEXT_RECENCY_WEIGHT` default 0.15. Chunks without `modified_at` get recency 0, so nothing breaks.
  - Compression: when over `token_budget`, trim each chunk to its most query-relevant sentences (sentence split, score each sentence by query term overlap, keep top sentences) rather than hard-truncating. Keeps the citation intact.
  - Render: a citation-aware template that numbers each kept chunk `[n]` and lists `file` and `section`, so the model can cite `[n]` and `synthesize_node` can map markers back to sources for item 2.

`orchestrator/graph.py`
- `rag_node` calls `assemble` after gathering chunks and stores the assembled set and the rendered string in state. `llm_node` uses the rendered string and the citation-aware system prompt instructing the model to cite sources as `[n]`. The hard `chunks[:10]` slice is removed.

`orchestrator/state.py`
- `AgentState` gains `assembled_context: dict` (the selected chunks plus rendered text) so `synthesize_node` reads exactly what went to the model.

### Risk

Compression can drop a sentence the model needed. Make compression apply only when over budget, and log pre and post token counts in the trace (item 6) so its effect is visible. Keep the full chunk text in `sources` even when the prompt copy is compressed.

---

## Item 5: Testing and evaluation

### Current state

No `tests/` directory, no pytest config. `harness/eval_suite.py` has tasks and judges but no golden answers and no retrieval recall tests. A `.venv` exists; pytest is not in requirements.

### Target

A runnable pytest suite plus RAG and retrieval evals, with the live-service tests skipping cleanly when Qdrant, Ollama, or the agents are down.

### Changes

`requirements-dev.txt` (new)
- `pytest`, `pytest-asyncio`, `pytest-cov`, `respx` (httpx mocking), `freezegun` (time control for recency and cooldown tests).

`tests/` (new), structure mirrors the packages:
- `tests/conftest.py`: fixtures for a fake Qdrant (respx), sample chunks with full provenance, a temp `logs/` dir, and a `live` marker that skips when a probed service is unavailable.
- `tests/unit/test_router.py`: classifier confidence and margin, low-confidence default, decision logging writes a line. Pure Python.
- `tests/unit/test_chunking.py`: `chunk_text` window and overlap counts, section heading capture, empty and whitespace input. Pure Python.
- `tests/unit/test_uploads.py`: `extract_text` for txt and md, chunk counts, chat-context add / get / clear / list. PDF and DOCX behind import-guards. Pure Python.
- `tests/unit/test_memory.py`: memory add / recall / delete against a mocked Qdrant via respx; episodic summarisation skips without a model.
- `tests/unit/test_context_assembler.py`: dedupe drops near-duplicates, per-file cap holds, recency reorders deterministically under freezegun, compression respects the budget and preserves citations.
- `tests/unit/test_provenance.py`: `Chunk.from_qdrant_payload` fills defaults; `synthesize_node` emits `sources` and `citations`.
- `tests/unit/test_security.py`: see item on security tests below.
- `tests/eval/test_rag_golden.py`: golden-answer evals. A new `harness/golden.py` holds 8 to 12 question / expected-fact / source-file records. The test scores answers with `eval_suite.deterministic_score` plus a fact-substring check; marked `live`, skipped without models.
- `tests/eval/test_retrieval_recall.py`: a labelled set in `harness/recall_set.py` (query to the file IDs that should appear). Asserts recall@k against a seeded fake corpus so it runs offline, and against live Qdrant when present.
- `tests/perf/test_cost_latency.py`: see item 6.

Security tests, called out because the brief names them
- `tests/unit/test_security.py`: `confine_to_roots` rejects `../` traversal and absolute escapes, accepts in-root paths (covers `common/security.py` lines 119-138). `require_admin` returns 403 for a non-loopback caller without the admin key and 503 when no key is configured. A request with a spoofed non-loopback client to each `Depends(require_admin)` endpoint (`/upload`, `DELETE /cost`, `DELETE /memory`, `/harness/run`, `/n8n/call`) gets 401 or 403, not execution. These use FastAPI's `TestClient` with a patched client host, no live services.

`pyproject.toml` or `pytest.ini` (new)
- Register the `live` and `perf` markers, set `asyncio_mode = auto`, point coverage at the source packages.

### Risk

Golden answers drift as models change. Score on expected facts and style, not exact strings, and keep the golden set small and curated so it stays maintainable.

---

## Item 6: Observability

### Current state

`cost_tracker` logs per-call cost to `logs/cost_log.jsonl` (lines 75-80). There is no per-request trace tying together route, agents called, latency by agent, retrieval count, fallback events, token use, and final confidence. Errors accumulate in `state["errors"]` but are not structured.

### Target

One structured trace per request, written as a JSON line, that stitches the whole pipeline together.

### Changes

New file `orchestrator/trace.py`
- A `RequestTrace` object created at request entry with a `request_id`. Methods: `start_span(name)` / `end_span(name)` for per-agent and per-node latency, `set(field, value)`, `add_event(kind, detail)`. `finish()` writes one line to `logs/traces.jsonl` with: `request_id`, timestamp, query preview, route and department, `task_type` and routing `confidence`, agents called and per-agent latency and chunk count, total retrieval count, chunks after assembly, model chosen, model fallback events (from a hook in `fallback_chain`), token use, cost, total latency, errors, and final confidence.

`orchestrator/graph.py`
- Create the trace in the entry node and thread it through state (`AgentState` gains `trace`). `route_node`, `rag_node` (per-agent spans around `_fetch_agent`), `llm_node`, and `synthesize_node` each record into it. `synthesize_node` calls `trace.finish()`.

`orchestrator/fallback_chain.py`
- `_mark_failed` (line 65) and each retry in `call_with_fallback` (lines 168-177) emit a fallback event through an optional `trace` argument so model fallbacks land in the trace, not just the logger.

`orchestrator/main.py`
- Generate `request_id` in `run_query`, `run_hybrid`, and the stream endpoints; put it in the response and the `X-Request-ID` header so a UI line can be traced end to end.
- New `GET /traces?limit=` reads the tail of `logs/traces.jsonl` for the dashboard.

### Risk

Tracing every span adds small overhead and a growing log file. Keep spans coarse (per agent, per node, not per function) and add a simple size-based rotation to the trace writer.

---

## Item 7: Operational reliability

### Current state

Each service exposes `GET /health` returning a static `{"status":"ok"}`. There are no dependency checks at startup, no version pinning (requirements use `>=`), no shared retry/backoff policy (only the ad hoc back-off in `fallback_chain`), and provider failure cooldowns live in an in-memory dict (`fallback_chain._FAILED_PROVIDERS`, line 38) that resets on restart.

### Target

Health checks that report real dependency state, startup checks that fail fast with a clear message, pinned versions, one retry policy, and provider cooldowns that survive a restart.

### Changes

New file `common/health.py`
- `check_dependency(name, url, kind)` for Qdrant, Ollama, and each upstream agent. `deep_health()` aggregates them into `{status: ok|degraded|down, checks: {...}}`. Every service's `/health` calls `deep_health()` for its own dependencies instead of returning a static dict. The orchestrator gains `GET /health/deep` that probes Qdrant, Ollama, and the three agents in parallel.

New file `common/startup.py`
- `require_dependencies([...])` run in each service's FastAPI startup event. Missing hard dependency (for example Qdrant for the retriever) logs a clear line and, behind `STRICT_STARTUP`, exits non-zero rather than serving broken. Soft dependencies log a warning and continue.

New file `common/retry.py`
- One `@with_retry` decorator and an `async_retry` wrapper using exponential backoff with jitter, built on the `tenacity` dependency already in requirements. Replace the hand-rolled back-off in `fallback_chain` and wrap the httpx calls in `graph._fetch_agent`, the indexer's upserts, and the agents' Qdrant calls.

`orchestrator/fallback_chain.py`
- Move `_FAILED_PROVIDERS` to a small persistent store, `logs/provider_cooldowns.json`, written on `_mark_failed` and read on `_is_available`. Cooldowns then survive a restart. Keep the in-memory dict as a write-through cache.

`requirements.txt`
- Pin every line to a tested version (`==` or compatible-release `~=`) rather than `>=`. Generate the pinned set from the working `.venv` so the pins match what runs today. Keep the existing comments.

### Risk

Pinning can surface an install that the current `.venv` satisfied loosely. Generate pins from `pip freeze` of the live `.venv` so the pinned set is known-good, and pin in a single commit that is easy to revert.

---

## Cross-cutting deliverables

- `docs/runbook.md`: the one-time re-index step (item 2), the env flags introduced (`RETRIEVER_USE_RERANKER`, `ROUTER_MIN_CONFIDENCE`, `ROUTER_MIN_MARGIN`, `ROUTER_USE_EMBEDDING`, `CONTEXT_*`, `STRICT_STARTUP`), and how to read `logs/traces.jsonl` and `logs/routing_decisions.jsonl`.
- `.env.example`: add the new flags with defaults and short comments.

---

## Phase order and dependencies

Phase A, retrieval core. Item 2 schema first (`rag/schema.py`), then item 1 (unify on the hybrid retriever, per-collection BM25), then item 4 (context assembler and citation template). These three share the `Chunk` schema and must land together to be coherent. Re-index after.

Phase B, routing. Item 3 classifier, decision log, and the tuning script. Independent of Phase A code, but the tuning script reuses the eval tasks.

Phase C, observability. Item 6 trace. Depends on A and B so the trace can record real route confidence, per-agent retrieval counts, and assembly counts.

Phase D, reliability. Item 7 health, startup, retry, persistent cooldowns, pinned versions. Independent, can run in parallel with C.

Phase E, testing. Item 5 last so tests cover the final shapes from A to D, with security and recall tests written alongside the code they cover rather than retrofitted.

## What I can verify here, and what I cannot

In the sandbox I can run every pure-Python unit test: router and classifier, chunking, context assembler, uploads, provenance schema, security (path traversal and the admin-gate checks via TestClient), and the offline retrieval-recall test against a seeded fake corpus. I will run these and report pass or fail.

I cannot run anything that needs your live services: Qdrant, Ollama, the three agents, or real model calls. Those tests carry the `live` marker and skip cleanly here. You run them in your environment. The cost and latency regression test records a baseline on first run, so it is meaningful only once you run it against live models.

## Open questions before I build

1. Pin versions from the current `.venv` with `~=` (allows patch updates) or hard `==` (fully reproducible). I recommend `~=`.
2. The embedding second stage for routing (item 3) stays off by default and dependency-free unless you want it on from the start.
3. Golden set size for item 5: I suggest 8 to 12 questions drawn from your real vault and WijerCo KB so the expected facts are true. I can draft them from the indexed content, or you supply the questions.
