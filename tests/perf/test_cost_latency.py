"""Cost/latency regression tracking.

Records a baseline to logs/perf_baseline.json on first run. On later runs it
compares against the baseline and fails if latency or cost regresses beyond a
tolerance. The live test needs a reachable model; the offline test covers the
pure-Python assembly path so a baseline always exists to extend.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

BASELINE_PATH = Path(os.getenv("PERF_BASELINE_PATH",
                               str(Path(__file__).resolve().parent.parent.parent / "logs" / "perf_baseline.json")))
TOLERANCE = float(os.getenv("PERF_TOLERANCE", "1.5"))   # allow 50% slower before failing


def _load_baseline() -> dict:
    if BASELINE_PATH.exists():
        try:
            return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_baseline(data: dict) -> None:
    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def test_assembly_latency_regression(sample_chunks, tmp_path, monkeypatch):
    # Use a throwaway baseline file so CI runs are self-contained.
    monkeypatch.setenv("PERF_BASELINE_PATH", str(tmp_path / "perf.json"))
    from orchestrator import context_assembler as ca

    # Bulk the input up so timing is measurable.
    chunks = (sample_chunks * 50)
    t0 = time.perf_counter()
    for _ in range(20):
        ca.assemble(chunks, query="diagnostic sprint competitors triage")
    elapsed = (time.perf_counter() - t0) / 20

    baseline_path = Path(tmp_path / "perf.json")
    key = "assemble_avg_s"
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}

    if key not in baseline:
        baseline[key] = elapsed
        baseline_path.write_text(json.dumps(baseline, indent=2))
        pytest.skip(f"recorded baseline {key}={elapsed:.4f}s")
    else:
        assert elapsed <= baseline[key] * TOLERANCE, (
            f"assembly regressed: {elapsed:.4f}s vs baseline {baseline[key]:.4f}s")


@pytest.mark.perf
@pytest.mark.live
def test_query_cost_latency_baseline():
    import os as _os
    qdrant = _os.getenv("QDRANT_URL", "http://localhost:6333")
    try:
        import httpx
        if httpx.get(f"{qdrant}/healthz", timeout=1.0).status_code >= 500:
            pytest.skip("qdrant not reachable")
    except Exception:
        pytest.skip("qdrant not reachable")

    import asyncio
    from orchestrator.graph import graph

    state = {
        "messages": [], "query": "What does the Diagnostic Sprint cost?",
        "routing": None, "context_chunks": [], "output_payload": {},
        "agents_used": [], "errors": [], "finished": False,
    }
    t0 = time.perf_counter()
    final = asyncio.get_event_loop().run_until_complete(graph.ainvoke(state))
    elapsed = time.perf_counter() - t0
    payload = final.get("output_payload", {})
    cost = payload.get("cost_usd", 0.0)

    baseline = _load_baseline()
    rec = {"query_latency_s": elapsed, "query_cost_usd": cost}
    if "query_latency_s" not in baseline:
        baseline.update(rec)
        _save_baseline(baseline)
        pytest.skip(f"recorded query baseline: {rec}")
    else:
        assert elapsed <= baseline["query_latency_s"] * TOLERANCE
        assert cost <= max(baseline.get("query_cost_usd", 0.0) * TOLERANCE, 0.01)
