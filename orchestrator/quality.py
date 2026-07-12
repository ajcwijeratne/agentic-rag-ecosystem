"""
Quality overview aggregation.

Combines recent request traces with the persistent eval ledger. This is the
Phase 1 cockpit surface: are answers cited, are routes accurate, are requests
cheap/fast, and where are failures clustering?
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .eval_store import latest_run, list_runs
from .trace import read_traces


def overview(trace_limit: int = 200, eval_limit: int = 10) -> dict[str, Any]:
    traces = read_traces(trace_limit)
    trace_summary = _summarise_traces(traces)
    runs = list_runs(eval_limit)
    latest = latest_run()
    return {
        "traces": trace_summary,
        "latest_eval": latest,
        "eval_runs": runs,
        "recommendations": _recommendations(trace_summary, latest),
    }


def _summarise_traces(traces: list[dict]) -> dict[str, Any]:
    total = len(traces)
    if not total:
        return {
            "total": 0,
            "avg_latency_ms": 0.0,
            "avg_cost_usd": 0.0,
            "avg_confidence": 0.0,
            "citation_rate": 0.0,
            "error_rate": 0.0,
            "providers": [],
            "agents": [],
            "top_errors": [],
        }

    latencies = [float(t.get("total_latency_ms") or 0) for t in traces]
    costs = [float(t.get("cost_usd") or 0) for t in traces]
    confidences = [float(t.get("final_confidence") or 0) for t in traces]
    cited = sum(1 for t in traces if int(t.get("citation_count") or 0) > 0)
    errored = sum(1 for t in traces if t.get("errors"))
    providers = Counter(t.get("provider") or "unknown" for t in traces)
    agents = Counter()
    errors = Counter()
    for t in traces:
        for a in t.get("agents_used") or []:
            agents[a] += 1
        for e in t.get("errors") or []:
            errors[str(e)[:160]] += 1

    return {
        "total": total,
        "avg_latency_ms": round(sum(latencies) / total, 2),
        "avg_cost_usd": round(sum(costs) / total, 6),
        "avg_confidence": round(sum(confidences) / total, 4),
        "citation_rate": round(cited / total, 4),
        "error_rate": round(errored / total, 4),
        "providers": [{"name": k, "count": v} for k, v in providers.most_common()],
        "agents": [{"name": k, "count": v} for k, v in agents.most_common()],
        "top_errors": [{"error": k, "count": v} for k, v in errors.most_common(10)],
    }


def _recommendations(trace_summary: dict[str, Any], latest: dict[str, Any] | None) -> list[str]:
    recs: list[str] = []
    if trace_summary.get("total", 0) == 0:
        recs.append("Run a few representative queries to populate request traces.")
    if trace_summary.get("citation_rate", 1.0) < 0.7 and trace_summary.get("total", 0) >= 5:
        recs.append("Citation rate is low; review retrieval quality and context assembly.")
    if trace_summary.get("error_rate", 0.0) > 0.05:
        recs.append("Errors are recurring; inspect top_errors and failing agent spans.")
    if trace_summary.get("avg_confidence", 1.0) < 0.65 and trace_summary.get("total", 0) >= 5:
        recs.append("Average confidence is low; tune routing thresholds and source freshness.")
    if not latest:
        recs.append("Run the offline routing eval to establish a quality baseline.")
    else:
        summary = latest.get("summary") or {}
        if summary.get("pass_rate", 1.0) < 0.85:
            recs.append("Latest eval pass rate is below target; inspect failed cases before expanding automation.")
    return recs
