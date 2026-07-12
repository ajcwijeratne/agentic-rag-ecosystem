"""
Phase 1 evaluation runner.

The default run is offline and free: it evaluates routing against the WijerCo
seed tasks. Live mode calls agents/models and scores the generated output, so it
is opt-in and protected by admin auth at the API layer.
"""

from __future__ import annotations

import time
from typing import Any

from harness.eval_suite import TASKS, score_output

from . import eval_store
from .wijerco_router import classify_intent


async def run_eval(
    suite: str = "routing",
    *,
    live: bool = False,
    departments: list[str] | None = None,
    limit: int | None = None,
    max_tier: int = 1,
) -> dict[str, Any]:
    """Run an evaluation suite and persist its results."""
    suite = suite or "routing"
    mode = "live" if live else "offline"
    run_id = eval_store.create_run(suite, mode)
    status = "complete"
    try:
        tasks = _selected_tasks(departments, limit)
        if suite in ("routing", "all"):
            for task in tasks:
                _eval_routing_case(run_id, task)
        if suite in ("answer_quality", "all"):
            if not live:
                eval_store.add_result(
                    run_id,
                    case_id="answer_quality:skipped",
                    suite="answer_quality",
                    target=None,
                    passed=True,
                    score=1.0,
                    issues=[],
                    detail={"skipped": "answer_quality requires live=true"},
                )
            else:
                for task in tasks:
                    await _eval_live_answer_case(run_id, task, max_tier=max_tier)
        summary = eval_store.finish_run(run_id, status)
    except Exception as exc:
        status = "error"
        eval_store.add_result(
            run_id,
            case_id="runner:error",
            suite=suite,
            target=None,
            passed=False,
            score=0.0,
            issues=[str(exc)],
            detail={"error": type(exc).__name__},
        )
        summary = eval_store.finish_run(run_id, status)
    return {"run_id": run_id, "status": status, "summary": summary}


async def verify_case(suite: str, case_id: str, *, live: bool = False, max_tier: int = 1) -> dict[str, Any]:
    """Re-run one eval case and auto-promote to verified if it passes."""
    task = next((t for t in TASKS if t.id == case_id), None)
    if task is None:
        raise ValueError(f"unknown eval case: {case_id}")

    run_id = eval_store.create_run(f"{suite}_verification", "live" if live else "offline")
    if suite == "routing":
        _eval_routing_case(run_id, task)
    elif suite == "answer_quality":
        if not live:
            raise ValueError("answer_quality verification requires live=true")
        await _eval_live_answer_case(run_id, task, max_tier=max_tier)
    else:
        raise ValueError("suite must be 'routing' or 'answer_quality'")

    summary = eval_store.finish_run(run_id)
    passed = bool(summary.get("passed", 0))
    state = None
    if passed:
        state = eval_store.update_case_state(suite, case_id, status="verified")
    return {
        "run_id": run_id,
        "case_id": case_id,
        "suite": suite,
        "passed": passed,
        "summary": summary,
        "case_state": state or eval_store.get_case_state(suite, case_id),
    }


def _selected_tasks(departments: list[str] | None, limit: int | None):
    tasks = TASKS
    if departments:
        wanted = set(departments)
        tasks = [t for t in tasks if t.department in wanted]
    if limit:
        tasks = tasks[: max(0, limit)]
    return tasks


def _eval_routing_case(run_id: str, task) -> None:
    classification = classify_intent(task.prompt)
    predicted = classification.department or classification.target
    passed = (
        predicted == task.department
        or (classification.target == "hybrid" and classification.department == task.department)
    )
    issues = [] if passed else [f"expected {task.department}, got {predicted}"]
    eval_store.add_result(
        run_id,
        case_id=task.id,
        suite="routing",
        target=task.department,
        passed=passed,
        score=1.0 if passed else 0.0,
        issues=issues,
        detail={
            "prompt": task.prompt,
            "split": task.split,
            "predicted_target": classification.target,
            "predicted_department": classification.department,
            "confidence": classification.confidence,
            "reason": classification.reason,
        },
    )


async def _eval_live_answer_case(run_id: str, task, *, max_tier: int) -> None:
    from .wijerco_agent import call_wijerco_agent

    t0 = time.perf_counter()
    result = await call_wijerco_agent(
        department=task.department,
        query=task.prompt,
        max_tier=max_tier,
    )
    latency_ms = int((time.perf_counter() - t0) * 1000)
    score = await score_output(task.prompt, result.get("answer", ""))
    passed = score.score >= 0.75 and not result.get("error")
    issues = list(score.issues)
    if result.get("error"):
        issues.append(result["error"])
    eval_store.add_result(
        run_id,
        case_id=task.id,
        suite="answer_quality",
        target=task.department,
        passed=passed,
        score=score.score,
        latency_ms=latency_ms,
        cost_usd=float(result.get("cost_usd") or 0.0),
        issues=issues,
        detail={
            "prompt": task.prompt,
            "split": task.split,
            "model": result.get("model_label") or result.get("model"),
            "provider": result.get("provider"),
            "tokens": result.get("tokens_used"),
            "judge": score.detail,
        },
    )
