# System Effectiveness Runbook

What changed, what you must run once, and how to read the new logs. Pairs with docs/system-effectiveness-plan.md.

## One-time re-index (required)

The provenance fields (`section`, `modified_at`, `chunk_id`, `collection`) are written at index time. Existing Qdrant chunks predate them and will retrieve with those fields empty until you re-index. Old chunks still work; they just lack section and recency data, which weakens context assembly.

Re-index the vault and the WijerCo knowledge base:

```
python -m rag.indexer --vault "C:/Users/ajwij/OneDrive/Documents/Obsidian Vault"
python -m rag.indexer --wijerco
```

Uploaded documents gain provenance automatically on the next upload. To backfill old uploads, re-upload them.

## Install

Runtime stays the same. For the test suite:

```
pip install -r requirements.txt -r requirements-dev.txt
```

Versions in requirements.txt are now pinned with `~=` (compatible release). To capture exactly what runs on your machine: `pip freeze > requirements.lock.txt`.

## Running tests

```
pytest                 # full suite; live tests skip when services are down
pytest -m "not live"   # offline only (routing, chunking, assembler, security, provenance, offline recall)
pytest -m live         # needs Qdrant + Ollama + a model; runs golden + live recall
pytest tests/perf      # records a baseline on first run, then guards against regression
```

The `live` marker skips cleanly when Qdrant, Ollama, or the agents are unreachable, so the offline suite is always runnable.

## Tuning the router

The classifier logs every decision. Tune thresholds and keyword weights against data rather than by editing lists:

```
python -m scripts.tune_router
```

It replays the eval tasks and prints a confusion matrix, accuracy against the department-implied label, and the low-confidence rate. The department-implied label is a proxy, not ground truth, so treat the accuracy as a consistency signal. For real accuracy, build a hand-labelled set and point the script at it. Set `ROUTER_MIN_CONFIDENCE` and `ROUTER_MIN_MARGIN` from what you see.

## New endpoints

- `GET /health/deep` on the orchestrator, retriever, indexer, and local-data agent. Probes Qdrant, Ollama, and the sub-agents in parallel and returns `ok | degraded | down` with per-dependency detail.
- `GET /traces?limit=50` returns recent per-request traces.
- `GET /routing-decisions?limit=200` returns recent routing decisions.
- Multimedia production endpoints are covered in `docs/multimedia-production-runbook.md`.

`/query` responses now also carry `request_id`, `final_confidence`, `sources`, `citations`, and `assembly_stats`.

## Reading the logs

All under `logs/`.

`traces.jsonl` is one line per request: route and backend, task type and routing confidence, per-agent latency and chunk count (`spans.agent.*`), `retrieval_count`, `chunks_after_assembly`, model chosen, `model_fallback` events, token use, cost, `total_latency_ms`, errors, and `final_confidence`. Rotates at `TRACE_LOG_MAX_BYTES` (default 10 MB) to `traces.jsonl.1`.

`routing_decisions.jsonl` is one line per routing decision: chosen task type and backend, confidence, margin, runner-up, and how it was decided (`heuristic` or `low_confidence_default`).

`provider_cooldowns.json` holds provider failure cooldowns so a rate-limited or billing-failed provider stays skipped across a restart until its cooldown expires.

`cost_log.jsonl` is unchanged.

## New environment flags

See `.env.example` for the full list with defaults. The ones you are most likely to touch:

- `RETRIEVER_USE_RERANKER=1` — set 0 to skip loading the cross-encoder on low-memory machines.
- `ROUTER_MIN_CONFIDENCE`, `ROUTER_MIN_MARGIN` — routing safety thresholds.
- `ROUTER_USE_EMBEDDING=0` — leave off until you have tuned centroids.
- `CONTEXT_*` — dedupe threshold, per-file cap, recency weight and half-life, chunk and token budgets.
- `STRICT_STARTUP=0` — set 1 to make a service exit if a hard dependency is down at startup.

## Rollback

Each phase is independent. The riskiest single change is version pinning; it is one commit and easy to revert if an install breaks. The retrieval, routing, observability, and reliability changes are additive and degrade gracefully (hybrid falls back to dense, classifier falls back to the default task, missing provenance fields default to empty).
