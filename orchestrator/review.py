"""Monthly review: the 30-minute discipline, assembled for you.

Pulls the four things worth looking at once a month into one report: the
harness's pending self-improvement proposals, how the router scored against the
gold set, the month's spend against budget, and one operational rehearsal. It
writes a dated markdown file under logs/reviews/ and returns a short summary the
daemon notifies. Every source is optional; a missing one degrades to a note.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
REVIEWS_DIR = _ROOT / "logs" / "reviews"


def _safe(fn, default):
    try:
        return fn()
    except Exception as exc:
        return {"error": str(exc)[:200], **(default if isinstance(default, dict) else {})}


def _collect() -> dict[str, Any]:
    from . import cost_tracker, router_eval

    budget = _safe(cost_tracker.budget_status, {})
    months = _safe(lambda: cost_tracker.tracker.monthly_summary(months=1), {})
    routing = _safe(router_eval.evaluate, {"ok": False})

    def _proposals():
        from harness import store
        return {"pending": store.list_proposals(status="pending"),
                "recent_iterations": store.list_iterations(limit=5)}
    harness = _safe(_proposals, {"pending": []})

    def _rehearsal():
        from . import deployment
        return deployment.operational_rehearsal()
    rehearsal = _safe(_rehearsal, {})

    def _perf():
        from . import outcomes
        return outcomes.highlight(days=30)
    performance = _safe(_perf, {})

    return {"budget": budget, "cost_months": months, "routing": routing,
            "harness": harness, "rehearsal": rehearsal, "performance": performance}


def _render(month: str, data: dict[str, Any]) -> tuple[str, list[str]]:
    """Return (markdown report, short highlight lines for a notification)."""
    b = data.get("budget") or {}
    r = data.get("routing") or {}
    h = data.get("harness") or {}
    reh = data.get("rehearsal") or {}
    perf = data.get("performance") or {}
    pending = h.get("pending") or []

    highlights: list[str] = []
    spend = f"${b.get('spent_usd', 0):.2f}"
    if b.get("enabled"):
        spend += f" / ${b.get('budget_usd', 0):.2f} ({b.get('level')})"
    highlights.append(f"Spend: {spend}")
    if r.get("ok"):
        highlights.append(f"Routing accuracy: {r.get('accuracy', 0):.0%} on {r.get('total')} labelled")
    else:
        highlights.append("Routing: unlabelled (run router_eval --template)")
    highlights.append(f"Harness proposals pending: {len(pending)}")
    if perf.get("line"):
        highlights.append(perf["line"])

    lines = [f"# Monthly review — {month}", "", "## Highlights"]
    lines += [f"- {h_}" for h_ in highlights]
    lines += ["", "## Budget", f"- Month-to-date spend: {spend}"]
    cm = (data.get("cost_months") or {}).get("months") or []
    if cm:
        top = cm[0]
        by_task = ", ".join(f"{k} {v.get('calls',0)}" for k, v in (top.get('by_department') or {}).items())
        lines.append(f"- Calls: {top.get('calls', 0)}, tokens: {top.get('total_tokens', 0)}"
                     + (f", by dept: {by_task}" if by_task else ""))

    lines += ["", "## Routing"]
    if r.get("ok"):
        lines.append(f"- Accuracy {r.get('accuracy'):.0%} ({r.get('correct')}/{r.get('total')}), "
                     f"embedding {'on' if r.get('embedding_enabled') else 'off'}")
        for m in (r.get("misses") or [])[:8]:
            lines.append(f"  - miss: '{m['query']}' -> {m['predicted']} (should be {m['truth']})")
    else:
        lines.append(f"- {r.get('reason', 'no data')}")

    lines += ["", "## Harness proposals"]
    if pending:
        for p in pending[:8]:
            lines.append(f"- {p.get('title') or p.get('id')}: {str(p.get('rationale') or '')[:120]}")
    else:
        lines.append("- none pending")

    lines += ["", "## Rehearsal"]
    lines.append(f"- {reh.get('summary') or reh.get('status') or 'no rehearsal data'}")

    return "\n".join(lines), highlights


def run_monthly_review() -> dict[str, Any]:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    data = _collect()
    report, highlights = _render(month, data)
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    path = REVIEWS_DIR / f"{month}.md"
    path.write_text(report, encoding="utf-8")
    return {"ok": True, "month": month, "report_path": str(path),
            "highlights": highlights, "data": data}
