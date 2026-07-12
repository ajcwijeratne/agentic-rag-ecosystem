# Phase 1 Quality Operations

Phase 1 adds the measurement layer for the agentic ecosystem: request traces,
persisted eval runs, and a quality overview.

The Command Centre now includes a **Quality** view under Workspace, with the
same metrics and a button for the offline routing baseline.

The Quality view is also a working surface: click a recent eval run to inspect
case-level results, filter failed/passed cases, create an improvement task from
any recommendation or failed case, and move cases through `new`, `triaged`,
`fixed`, and `verified` with a short note.

The **Quality work queue** groups open eval issues by status across all recent
runs, so fresh failures can be triaged without opening each run manually.

## Endpoints

- `GET /traces` returns recent request traces from `logs/traces.jsonl`.
- `GET /routing-decisions` returns recent router/classifier decisions.
- `POST /evals/run` starts a persisted eval run.
- `GET /evals/runs` lists recent eval runs.
- `GET /evals/runs/{run_id}` returns a run with case-level results.
- `GET /evals/cases` lists saved eval case states.
- `GET /evals/work-queue` lists latest open eval issues joined to case state.
- `PATCH /evals/cases/{suite}/{case_id}` updates status and note.
- `POST /evals/cases/{suite}/{case_id}/verify` re-runs one case and promotes it when it passes.
- `GET /quality/overview` combines trace health, latest evals, and recommendations.

Admin auth is required for `POST /evals/run` because live evals can call models
and incur cost. Offline routing evals are safe and free.

## Baseline Run

Run the free routing baseline:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/evals/run `
  -ContentType "application/json" `
  -Body '{"suite":"routing","live":false}'
```

Run a small live answer-quality sample:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/evals/run `
  -ContentType "application/json" `
  -Body '{"suite":"answer_quality","live":true,"limit":3,"max_tier":1}'
```

Inspect the cockpit summary:

```powershell
Invoke-RestMethod http://localhost:8000/quality/overview
```

## What To Watch

- `citation_rate`: should rise as retrieval and context assembly improve.
- `error_rate`: should stay near zero.
- `avg_confidence`: low values indicate routing or retrieval uncertainty.
- `latest_eval.summary.pass_rate`: use this as the phase gate before expanding automation.
- `top_issues`: recurring style, routing, or generation weaknesses to fix next.
