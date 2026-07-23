"""Learning hook: the only judge that matters, audience response.

The Self-Harness loop optimises drafts against style rules. This closes a
different loop. Once a month it correlates recorded outcomes with the levers a
content strategist actually controls, format, topic, and hook style, then
writes the findings into project memory where the content strategist agents
already read. Next month's plans start from what worked, not from taste.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from . import outcomes

MIN_SAMPLES = 2  # a group needs at least this many pieces to rank


def _month_label() -> str:
    return datetime.now(timezone.utc).strftime("%B %Y")


def _productions(rows: list[dict]) -> dict[str, dict]:
    from . import production as pstore

    ids = {r.get("production_id") for r in rows if r.get("production_id")}
    out: dict[str, dict] = {}
    for pid in ids:
        prod = pstore.get_production(str(pid))
        if prod:
            out[str(pid)] = prod
    return out


def _topic(prod: dict) -> str:
    project = (prod.get("project") or "").strip()
    if project:
        return project
    title = (prod.get("title") or "").strip()
    return " ".join(title.split()[:4]) or "untagged"


def _hook(prod: dict) -> str | None:
    """Best-effort hook style from the brief or script; None when unlabelled."""
    for slice_name in ("brief", "script"):
        data = prod.get(slice_name)
        if isinstance(data, dict):
            for key in ("hook_style", "hook", "angle", "opening"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()[:60]
    return None


def _rank(rows: list[dict], prods: dict[str, dict], keyfn: Callable[[dict], str | None]) -> list[dict]:
    """Average engagement per group, ranked, groups below MIN_SAMPLES dropped."""
    groups: dict[str, list[int]] = defaultdict(list)
    for row in rows:
        prod = prods.get(str(row.get("production_id")))
        if not prod:
            continue
        key = keyfn(prod)
        if not key:
            continue
        groups[key].append(int(row.get("engagement") or 0))
    ranked = [
        {"key": key, "avg": round(sum(v) / len(v)), "n": len(v)}
        for key, v in groups.items() if len(v) >= MIN_SAMPLES
    ]
    ranked.sort(key=lambda g: g["avg"], reverse=True)
    return ranked


def _findings(rows: list[dict], prods: dict[str, dict]) -> tuple[list[str], dict]:
    by_format = _rank(rows, prods, lambda p: p.get("format"))
    by_topic = _rank(rows, prods, _topic)
    by_hook = _rank(rows, prods, _hook)
    month = _month_label()
    lines: list[str] = [f"Performance review {month}: {len(rows)} measured piece(s)."]

    def _fmt(group: dict) -> str:
        return f"{group['key']} (avg {group['avg']} eng, n={group['n']})"

    if by_format:
        best = _fmt(by_format[0])
        worst = f" Weakest format: {_fmt(by_format[-1])}." if len(by_format) > 1 else ""
        lines.append(f"Best format: {best}.{worst}")
    if by_topic:
        lines.append(f"Best topic: {_fmt(by_topic[0])}.")
    if by_hook:
        lines.append(f"Best hook: {_fmt(by_hook[0])}.")
    if by_format and by_topic:
        lines.append(f"Make more: {by_format[0]['key']} on {by_topic[0]['key']}.")
    return lines, {"by_format": by_format, "by_topic": by_topic, "by_hook": by_hook}


def run_learning_reflection() -> dict[str, Any]:
    """Correlate outcomes with format/topic/hook and write findings to memory."""
    from . import operating

    rows = outcomes.list_outcomes(limit=2000)
    if len(rows) < MIN_SAMPLES:
        return {"ok": False, "reason": "not enough measured outcomes yet", "count": len(rows)}
    prods = _productions(rows)
    lines, tables = _findings(rows, prods)
    content = "\n".join(lines)

    # Write to every project represented, plus a stable content-strategy bucket
    # the strategist agents read regardless of which project they are planning.
    projects = {(p.get("project") or "").strip() for p in prods.values() if p.get("project")}
    projects.add("content_strategy")
    written = []
    for project in sorted(projects):
        mem_id = operating.add_project_memory(
            project, content, source="learning", meta={"kind": "performance_review", **tables}
        )
        written.append({"project": project, "memory_id": mem_id})
    return {"ok": True, "count": len(rows), "findings": lines,
            "memories_written": written, "tables": tables}
