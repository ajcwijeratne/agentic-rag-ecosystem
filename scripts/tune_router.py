"""
Tune the routing classifier against a labelled query set, offline.

Primary mode: reads a labelled set (JSONL, one object per line with a `query`
and a gold `suggested` task type) and reports accuracy, a confusion matrix, the
low-confidence rate, and a grid search over ROUTER_MIN_CONFIDENCE /
ROUTER_MIN_MARGIN that minimises confident-wrong routing (the costly error) then
maximises accuracy. Prints the recommended thresholds.

Fallback mode: if no labelled file is found, replays harness/eval_suite.py TASKS
against each task's department-implied type (a proxy, self-consistency only).

Run:
  python -m scripts.tune_router
Env:
  ROUTER_LABELS_PATH   default data/router_labels.template.jsonl
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.classifier import classify, DEFAULT_TASK   # noqa: E402

LABELS_PATH = Path(os.getenv("ROUTER_LABELS_PATH", str(ROOT / "data" / "router_labels.template.jsonl")))

# Thresholds to sweep in the grid search.
CONF_GRID   = [round(0.30 + 0.05 * i, 2) for i in range(11)]   # 0.30 .. 0.80
MARGIN_GRID = [round(0.05 * i, 2) for i in range(9)]           # 0.00 .. 0.40


def _load_labels(path: Path) -> list[tuple[str, str]]:
    """Return [(query, gold_task_type)] from a JSONL file, or [] if unusable."""
    rows: list[tuple[str, str]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        q = obj.get("query")
        gold = obj.get("gold") or obj.get("task_type") or obj.get("suggested")
        if q and gold:
            rows.append((q, gold))
    return rows


def _scored(query: str):
    """Return (top_task, top_p, margin, scores) from the classifier's softmax,
    independent of the live thresholds so we can simulate any threshold."""
    res = classify(query)
    scores = res.scores or {}
    if not scores:
        return DEFAULT_TASK, 0.0, 0.0, {}
    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top_task, top_p = ordered[0]
    runner_p = ordered[1][1] if len(ordered) > 1 else 0.0
    return top_task, top_p, top_p - runner_p, scores


def _predict(top_task, top_p, margin, conf_thr, margin_thr):
    """Apply thresholds: confident enough -> top_task, else the safe default."""
    if top_p >= conf_thr and margin >= margin_thr:
        return top_task
    return DEFAULT_TASK


def run_labelled(rows: list[tuple[str, str]]) -> int:
    n = len(rows)
    scored = [(q, gold, *_scored(q)) for q, gold in rows]

    # Current live-threshold behaviour, for the confusion matrix and baseline.
    from orchestrator.classifier import MIN_CONFIDENCE, MIN_MARGIN
    confusion: dict[str, Counter] = defaultdict(Counter)
    correct = low_conf = confident_wrong = 0
    rowlog = []
    for q, gold, top, p, margin, _s in scored:
        pred = _predict(top, p, margin, MIN_CONFIDENCE, MIN_MARGIN)
        confusion[gold][pred] += 1
        if pred == gold:
            correct += 1
        if pred == DEFAULT_TASK and top != DEFAULT_TASK and p < MIN_CONFIDENCE:
            low_conf += 1
        if pred != DEFAULT_TASK and pred != gold:
            confident_wrong += 1
        rowlog.append((gold, pred, top, round(p, 3), round(margin, 3), q[:48].replace("\n", " ")))

    print(f"Labelled set: {LABELS_PATH}")
    print(f"Rows: {n}")
    print(f"Current thresholds  conf>={MIN_CONFIDENCE}  margin>={MIN_MARGIN}")
    print(f"  Accuracy:         {correct}/{n} = {correct / n:.1%}")
    print(f"  Confident-wrong:  {confident_wrong}/{n} = {confident_wrong / n:.1%}  (routed to a non-default task that was wrong)")
    print(f"  Low-conf default: {low_conf}/{n}")
    print()

    gold_types = sorted({g for _, g, *_ in scored})
    pred_types = sorted({r[1] for r in rowlog})
    print("Confusion (rows = gold, cols = predicted):")
    print("  " + " " * 14 + "".join(f"{p[:11]:<13}" for p in pred_types))
    for g in gold_types:
        line = f"  {g:<14}"
        for p in pred_types:
            line += f"{confusion[g].get(p, 0):<13}"
        print(line)
    print()

    # Grid search: minimise confident-wrong, then maximise accuracy.
    best = None
    for ct in CONF_GRID:
        for mt in MARGIN_GRID:
            acc = cw = 0
            for _q, gold, top, p, margin, _s in scored:
                pred = _predict(top, p, margin, ct, mt)
                if pred == gold:
                    acc += 1
                if pred != DEFAULT_TASK and pred != gold:
                    cw += 1
            cand = (cw, -acc, ct, mt)   # sort key: fewer wrong, then more correct
            if best is None or cand < best:
                best = cand
    cw, neg_acc, ct, mt = best

    # Guard against overfitting a thin / skewed set. If almost every gold label
    # is the safe default, or no threshold anywhere produces a confident-wrong,
    # the set has no discriminative power and we should NOT loosen thresholds.
    non_default = sum(1 for _q, gold, *_ in scored if gold != DEFAULT_TASK)
    from orchestrator.classifier import MIN_CONFIDENCE as CUR_C, MIN_MARGIN as CUR_M
    worst_cw = 0
    for ct2 in CONF_GRID:
        for mt2 in MARGIN_GRID:
            worst_cw = max(worst_cw, sum(
                1 for _q, gold, top, p, margin, _s in scored
                if _predict(top, p, margin, ct2, mt2) != DEFAULT_TASK
                and _predict(top, p, margin, ct2, mt2) != gold))

    print("Recommendation:")
    if non_default < 8 or worst_cw == 0:
        print(f"  KEEP current thresholds (conf>={CUR_C}, margin>={CUR_M}).")
        print(f"  The set is thin/skewed ({non_default}/{n} non-default golds; no"
              f" threshold anywhere mis-routes), so it validates routing SAFETY"
              f" but cannot justify loosening. Looser thresholds only raise risk"
              f" on non-advisory queries this set does not cover.")
        print(f"  To calibrate meaningfully, add more non-advisory examples with"
              f" labels assigned independently of the classifier's suggestion.")
    else:
        print(f"  ROUTER_MIN_CONFIDENCE={ct}  ROUTER_MIN_MARGIN={mt}")
        print(f"  -> accuracy {(-neg_acc)}/{n} = {(-neg_acc)/n:.1%}, confident-wrong {cw}/{n}")
    return 0


# Proxy mapping used only when no labelled file is present.
DEPT_EXPECTED = {
    "marketing_sales":       "creative",
    "research_intelligence": "reasoning",
    "learning_design":       "advisory",
    "academic_development":  "advisory",
    "operations":            "advisory",
    "support":               "creative",
}


def run_proxy() -> int:
    from harness.eval_suite import TASKS
    correct = 0
    confusion: dict[str, Counter] = defaultdict(Counter)
    for t in TASKS:
        res = classify(t.prompt)
        expected = DEPT_EXPECTED.get(t.department, "advisory")
        confusion[expected][res.task_type] += 1
        if res.task_type == expected:
            correct += 1
    n = len(TASKS)
    print("No labelled file found; using department-implied proxy (self-consistency only).")
    print(f"Tasks: {n}")
    print(f"Accuracy vs department-implied type: {correct}/{n} = {correct / n:.1%}")
    return 0


def main() -> int:
    rows = _load_labels(LABELS_PATH)
    if rows:
        return run_labelled(rows)
    return run_proxy()


if __name__ == "__main__":
    raise SystemExit(main())
