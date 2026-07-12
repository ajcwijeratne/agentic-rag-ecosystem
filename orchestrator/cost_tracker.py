"""
Cost Tracker — in-memory session spend ledger.

Tracks:
  • Per-call cost + token counts
  • Running session total
  • Breakdown by provider and model
  • Breakdown by task type

Thread-safe via a simple list (FastAPI runs async, single process).
The ledger resets when the server restarts.
For persistence, swap the list for SQLite or append to a JSON file.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Any

LOG_PATH = Path(Path(__file__).parent.parent / "logs" / "cost_log.jsonl")


@dataclass
class CostEntry:
    timestamp:     float
    model_key:     str
    model_label:   str
    provider:      str
    task_type:     str
    input_tokens:  int
    output_tokens: int
    cost_usd:      float
    latency_ms:    int
    query_preview: str       # first 80 chars of the query


class CostTracker:
    def __init__(self, persist: bool = True):
        self._entries: list[CostEntry] = []
        self._lock    = Lock()
        self._persist = persist
        if persist:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        model_key:     str,
        model_label:   str,
        provider:      str,
        task_type:     str,
        input_tokens:  int,
        output_tokens: int,
        cost_usd:      float,
        latency_ms:    int,
        query:         str,
    ) -> None:
        entry = CostEntry(
            timestamp     = time.time(),
            model_key     = model_key,
            model_label   = model_label,
            provider      = provider,
            task_type     = task_type,
            input_tokens  = input_tokens,
            output_tokens = output_tokens,
            cost_usd      = cost_usd,
            latency_ms    = latency_ms,
            query_preview = query[:80],
        )
        with self._lock:
            self._entries.append(entry)
        if self._persist:
            try:
                with LOG_PATH.open("a") as f:
                    f.write(json.dumps(asdict(entry)) + "\n")
            except Exception:
                pass

    def session_summary(self) -> dict[str, Any]:
        with self._lock:
            entries = list(self._entries)

        total_cost = sum(e.cost_usd for e in entries)
        total_in   = sum(e.input_tokens for e in entries)
        total_out  = sum(e.output_tokens for e in entries)
        total_calls= len(entries)

        # By provider
        by_provider: dict[str, dict] = {}
        for e in entries:
            p = by_provider.setdefault(e.provider, {"calls": 0, "cost_usd": 0.0, "tokens": 0})
            p["calls"]    += 1
            p["cost_usd"] += e.cost_usd
            p["tokens"]   += e.input_tokens + e.output_tokens

        # By model
        by_model: dict[str, dict] = {}
        for e in entries:
            m = by_model.setdefault(e.model_label, {"calls": 0, "cost_usd": 0.0})
            m["calls"]    += 1
            m["cost_usd"] += e.cost_usd

        # By task
        by_task: dict[str, dict] = {}
        for e in entries:
            t = by_task.setdefault(e.task_type, {"calls": 0, "cost_usd": 0.0})
            t["calls"]    += 1
            t["cost_usd"] += e.cost_usd

        # Recent calls
        recent = [
            {
                "model":     e.model_label,
                "provider":  e.provider,
                "task":      e.task_type,
                "cost_usd":  round(e.cost_usd, 6),
                "in_tok":    e.input_tokens,
                "out_tok":   e.output_tokens,
                "latency_ms":e.latency_ms,
                "query":     e.query_preview,
            }
            for e in reversed(entries[-20:])
        ]

        return {
            "total_calls":    total_calls,
            "total_cost_usd": round(total_cost, 6),
            "total_tokens":   total_in + total_out,
            "input_tokens":   total_in,
            "output_tokens":  total_out,
            "by_provider":    {k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in by_provider.items()},
            "by_model":       {k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in by_model.items()},
            "by_task":        {k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in by_task.items()},
            "recent_calls":   recent,
        }

    def monthly_summary(self, months: int = 12) -> dict[str, Any]:
        """
        Aggregate the persistent JSONL ledger by calendar month.
        Survives restarts (reads the on-disk log, not just the session list).

        Returns the most recent `months` months, newest first, each with
        totals plus breakdowns by provider, model, and WijerCo department.
        """
        from datetime import datetime, timezone

        rows: list[dict] = []
        if LOG_PATH.exists():
            try:
                with LOG_PATH.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            rows.append(json.loads(line))
            except Exception:
                pass

        buckets: dict[str, dict] = {}
        for r in rows:
            ts = r.get("timestamp", 0)
            month = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")
            b = buckets.setdefault(month, {
                "month":          month,
                "calls":          0,
                "cost_usd":       0.0,
                "input_tokens":   0,
                "output_tokens":  0,
                "by_provider":    {},
                "by_model":       {},
                "by_department":  {},
            })
            cost = r.get("cost_usd", 0.0)
            b["calls"]         += 1
            b["cost_usd"]      += cost
            b["input_tokens"]  += r.get("input_tokens", 0)
            b["output_tokens"] += r.get("output_tokens", 0)

            prov = r.get("provider", "unknown")
            bp = b["by_provider"].setdefault(prov, {"calls": 0, "cost_usd": 0.0})
            bp["calls"] += 1; bp["cost_usd"] += cost

            model = r.get("model_label", "unknown")
            bm = b["by_model"].setdefault(model, {"calls": 0, "cost_usd": 0.0})
            bm["calls"] += 1; bm["cost_usd"] += cost

            # WijerCo department from task_type "wijerco/{dept}"
            task = r.get("task_type", "")
            if task.startswith("wijerco/"):
                dept = task.split("/", 1)[1]
                bd = b["by_department"].setdefault(dept, {"calls": 0, "cost_usd": 0.0})
                bd["calls"] += 1; bd["cost_usd"] += cost

        # Round and sort newest first
        def _round(d):
            for v in d.values():
                v["cost_usd"] = round(v["cost_usd"], 6)
            return d

        ordered = sorted(buckets.values(), key=lambda x: x["month"], reverse=True)[:months]
        for b in ordered:
            b["cost_usd"]     = round(b["cost_usd"], 6)
            b["total_tokens"] = b["input_tokens"] + b["output_tokens"]
            b["by_provider"]  = _round(b["by_provider"])
            b["by_model"]     = _round(b["by_model"])
            b["by_department"]= _round(b["by_department"])

        grand_cost = round(sum(r.get("cost_usd", 0.0) for r in rows), 6)
        return {
            "months":          ordered,
            "lifetime_cost":   grand_cost,
            "lifetime_calls":  len(rows),
        }

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# Singleton instance used across the application
tracker = CostTracker(persist=True)


# ---------------------------------------------------------------------------
# Budget circuit breaker
# ---------------------------------------------------------------------------
# The persisted jsonl ledger (LOG_PATH) survives restarts, so month-to-date
# spend is computed from disk, not from the in-memory session list. The daemon
# calls budget_status() before every cloud dispatch:
#   level == "ok"    spend below the warn threshold, dispatch freely
#   level == "warn"  past BUDGET_WARN_RATIO, notify once per month
#   level == "stop"  at or past the full budget, no paid dispatch
# MONTHLY_BUDGET_USD <= 0 disables the breaker (level stays "ok").

import os as _os
from datetime import datetime as _dt


def month_to_date_cost(month: str | None = None) -> float:
    """Sum cost_usd from the persisted ledger for the given YYYY-MM (default: now)."""
    target = month or _dt.now().strftime("%Y-%m")
    total = 0.0
    try:
        with LOG_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    ts = entry.get("timestamp")
                    if ts and _dt.fromtimestamp(float(ts)).strftime("%Y-%m") == target:
                        total += float(entry.get("cost_usd") or 0.0)
                except Exception:
                    continue
    except FileNotFoundError:
        pass
    return total


def budget_status() -> dict[str, Any]:
    """Current month spend against MONTHLY_BUDGET_USD."""
    budget = float(_os.getenv("MONTHLY_BUDGET_USD", "0") or 0)
    warn_ratio = float(_os.getenv("BUDGET_WARN_RATIO", "0.8") or 0.8)
    month = _dt.now().strftime("%Y-%m")
    spent = month_to_date_cost(month)
    if budget <= 0:
        return {"month": month, "spent_usd": round(spent, 4), "budget_usd": 0.0,
                "ratio": 0.0, "level": "ok", "enabled": False}
    ratio = spent / budget
    level = "stop" if ratio >= 1.0 else "warn" if ratio >= warn_ratio else "ok"
    return {"month": month, "spent_usd": round(spent, 4), "budget_usd": budget,
            "ratio": round(ratio, 4), "level": level, "enabled": True}
