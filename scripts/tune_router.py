"""
Tune the routing classifier against the eval set, offline.

Replays harness/eval_suite.py TASKS through orchestrator.classifier.classify and
reports a confusion matrix of predicted task type against each task's
department-implied type, plus accuracy and the low-confidence rate. Use it to set
ROUTER_MIN_CONFIDENCE / ROUTER_MIN_MARGIN and to revise keyword weights.

Note: department-implied type is a proxy label, not hand-labelled ground truth.
This measures self-consistency and threshold behaviour, not absolute correctness.
Replace DEPT_EXPECTED with a hand-labelled set when one exists.

Run:
  python -m scripts.tune_router
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.classifier import classify          # noqa: E402
from harness.eval_suite import TASKS                   # noqa: E402

# Proxy mapping: department to the task type we'd expect its prompts to need.
DEPT_EXPECTED = {
    "marketing_sales":       "creative",
    "research_intelligence": "reasoning",
    "learning_design":       "advisory",
    "academic_development":  "advisory",
    "operations":            "advisory",
    "support":               "creative",
}


def main() -> int:
    confusion: dict[str, Counter] = defaultdict(Counter)
    correct = 0
    low_conf = 0
    rows = []

    for t in TASKS:
        res = classify(t.prompt)
        expected = DEPT_EXPECTED.get(t.department, "advisory")
        confusion[expected][res.task_type] += 1
        if res.task_type == expected:
            correct += 1
        if res.decided_by == "low_confidence_default":
            low_conf += 1
        rows.append((t.id, t.department, expected, res.task_type,
                     round(res.confidence, 3), round(res.margin, 3), res.decided_by))

    n = len(TASKS)
    print(f"Tasks: {n}")
    print(f"Accuracy vs department-implied type: {correct}/{n} = {correct / n:.2%}")
    print(f"Low-confidence defaults: {low_conf}/{n} = {low_conf / n:.2%}")
    print()

    print("Per-task:")
    print(f"  {'id':<6}{'dept':<24}{'expected':<14}{'predicted':<16}{'conf':<7}{'margin':<8}{'decided_by'}")
    for r in rows:
        print(f"  {r[0]:<6}{r[1]:<24}{r[2]:<14}{r[3]:<16}{r[4]:<7}{r[5]:<8}{r[6]}")
    print()

    print("Confusion (rows = expected, cols = predicted):")
    predicted_types = sorted({p for c in confusion.values() for p in c})
    header = "  " + " " * 16 + "".join(f"{p[:12]:<14}" for p in predicted_types)
    print(header)
    for expected in sorted(confusion):
        line = f"  {expected:<16}"
        for p in predicted_types:
            line += f"{confusion[expected].get(p, 0):<14}"
        print(line)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
