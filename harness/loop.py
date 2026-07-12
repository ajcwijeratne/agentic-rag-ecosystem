"""
Self-Harness Optimization Loop
==============================
One iteration, per the Self-Harness design:

  1. Weakness mining — run the current harness on held-in tasks, score each
     output with the verifier (deterministic + LLM judge), collect failures and
     cluster their issues into a dominant failure pattern per department.
  2. Harness proposal — the same model, prompted with the failure pattern,
     drafts ONE bounded rule that would prevent it.
  3. Proposal validation — re-run held-in AND held-out tasks with the candidate
     rule injected; keep it only if held-in improves and held-out doesn't
     regress. Winners are queued for Aaron's approval (never auto-applied).

The fixed models and the evaluator stay fixed; only the harness (the rule layer
on the department files) is proposed for change.
"""

from __future__ import annotations

import re
import time
from collections import Counter

from orchestrator.wijerco_agent import _build_system_prompt, _DEPT_FILE
from orchestrator.fallback_chain import call_with_fallback
from .eval_suite import tasks_for, score_output, TASKS
from . import store

FAIL_THRESHOLD = 0.75     # outputs scoring below this are "failures"
MIN_HELDIN_GAIN = 0.03    # candidate must lift held-in average by at least this
MAX_HELDOUT_DROP = 0.02   # and must not drop held-out average by more than this


async def _run_task(department: str, prompt: str, extra_rule: str | None = None) -> str:
    """Run one task through the department harness, optionally with a candidate rule."""
    system = _build_system_prompt(department, extra_instructions=extra_rule)
    resp = await call_with_fallback(
        user_message    = prompt,
        system_prompt   = system,
        force_task_type = "advisory",
        max_tier        = 2,
    )
    return resp.content or ""


def _issue_bucket(issue: str) -> str:
    """Map a specific issue string to a coarse failure category."""
    i = issue.lower()
    if "banned word" in i or "buzzword" in i:           return "buzzwords / banned words"
    if "construction" in i:                              return "banned constructions"
    if "em-dash" in i:                                   return "em-dashes"
    if "specific" in i or "vague" in i or "number" in i: return "vagueness / no specifics"
    if "preamble" in i or "throat" in i or "lead" in i:  return "preamble / not leading with the point"
    if "short" in i:                                     return "too short / thin"
    return "other quality issues"


_PROPOSER_SYSTEM = """\
You improve an AI agent's instructions. Given a recurring failure pattern in a
department's outputs, write ONE short imperative rule (max 25 words) to add to
the agent's instructions so the failure stops happening. Be concrete and
specific to the pattern. Output ONLY the rule, no preamble, no quotes.
"""


async def run_department(department: str) -> dict:
    """Run one mine → propose → validate cycle for a single department."""
    held_in  = tasks_for(department, "held_in")
    held_out = tasks_for(department, "held_out")
    if not held_in:
        return {"department": department, "status": "skipped", "reason": "no held-in tasks"}

    # ── 1. Weakness mining ────────────────────────────────────────────────
    baseline_in = []
    all_issues  = []
    for t in held_in:
        out = await _run_task(department, t.prompt)
        sr  = await score_output(t.prompt, out)
        baseline_in.append(sr.score)
        if sr.score < FAIL_THRESHOLD:
            all_issues.extend(sr.issues)

    baseline_in_avg = sum(baseline_in) / len(baseline_in)

    if not all_issues:
        store.log_iteration({"department": department, "result": "no weaknesses found",
                             "baseline": round(baseline_in_avg, 3)})
        return {"department": department, "status": "clean", "baseline": round(baseline_in_avg, 3)}

    buckets = Counter(_issue_bucket(i) for i in all_issues)
    dominant, freq = buckets.most_common(1)[0]

    # ── 2. Harness proposal ───────────────────────────────────────────────
    prop = await call_with_fallback(
        user_message    = f"Department: {department}\nRecurring failure pattern: {dominant} (seen {freq}x)\nExample issues: {', '.join(all_issues[:6])}",
        system_prompt   = _PROPOSER_SYSTEM,
        force_task_type = "fast",
        max_tier        = 1,
    )
    rule = (prop.content or "").strip().strip('"').strip()
    rule = re.sub(r"^\s*[-*]\s*", "", rule)
    if not rule:
        return {"department": department, "status": "no proposal"}

    # ── 3. Proposal validation (regression on held-in + held-out) ─────────
    cand_in = []
    for t in held_in:
        out = await _run_task(department, t.prompt, extra_rule=rule)
        cand_in.append((await score_output(t.prompt, out)).score)
    cand_in_avg = sum(cand_in) / len(cand_in)

    base_out_avg = cand_out_avg = 0.0
    if held_out:
        b, c = [], []
        for t in held_out:
            b.append((await score_output(t.prompt, await _run_task(department, t.prompt))).score)
            c.append((await score_output(t.prompt, await _run_task(department, t.prompt, extra_rule=rule))).score)
        base_out_avg = sum(b) / len(b)
        cand_out_avg = sum(c) / len(c)

    heldin_gain  = cand_in_avg - baseline_in_avg
    heldout_drop = base_out_avg - cand_out_avg   # positive = regression

    accepted_for_queue = heldin_gain >= MIN_HELDIN_GAIN and heldout_drop <= MAX_HELDOUT_DROP

    result = {
        "department":      department,
        "failure_pattern": dominant,
        "rule":            rule,
        "baseline_in":     round(baseline_in_avg, 3),
        "candidate_in":    round(cand_in_avg, 3),
        "heldin_gain":     round(heldin_gain, 3),
        "heldout_delta":   round(-heldout_drop, 3),
        "queued":          accepted_for_queue,
    }

    if accepted_for_queue:
        slug = _DEPT_FILE.get(department, department.replace("_", "-"))
        target_file = f"AGENTS/departments/{slug}.md"
        store.add_proposal(
            department      = department,
            target_file     = target_file,
            failure_pattern = dominant,
            rule            = rule,
            baseline_score  = round(baseline_in_avg, 4),
            candidate_score = round(cand_in_avg, 4),
            heldout_delta   = round(-heldout_drop, 4),
            evidence        = {
                "dominant_issues": dict(buckets),
                "held_out_baseline": round(base_out_avg, 3),
                "held_out_candidate": round(cand_out_avg, 3),
                "n_held_in": len(held_in),
                "n_held_out": len(held_out),
            },
        )

    store.log_iteration(result)
    return result


async def run_loop(departments: list[str] | None = None) -> dict:
    """Run one optimization loop across the given departments (all if None)."""
    depts = departments or sorted({t.department for t in TASKS})
    results = []
    for d in depts:
        try:
            results.append(await run_department(d))
        except Exception as exc:
            results.append({"department": d, "status": "error", "error": str(exc)})
    queued = sum(1 for r in results if r.get("queued"))
    return {
        "ran_at":         time.time(),
        "departments":    len(depts),
        "proposals_queued": queued,
        "results":        results,
    }
