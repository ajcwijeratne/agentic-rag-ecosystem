"""
Decision log — one JSON line per routing decision, so choices can be tuned
against data instead of edited by hand. Never raises.

Readers: scripts/tune_router.py replays these and the eval set to set thresholds.
Writers: router.route_query, classifier (via router), wijerco_router.classify_intent.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_LOG_PATH = Path(os.getenv("ROUTING_LOG_PATH",
                           str(Path(__file__).parent.parent / "logs" / "routing_decisions.jsonl")))
_lock = Lock()


def log_decision(kind: str, query: str, result: dict) -> None:
    """Append one decision record. `kind` is e.g. 'task_route' or 'intent'."""
    record = {
        "ts":    datetime.now(timezone.utc).isoformat(),
        "kind":  kind,
        "query": (query or "")[:200],
        **result,
    }
    try:
        with _lock:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_decisions(limit: int = 200) -> list[dict]:
    """Return the most recent decisions, newest first. Empty on any error."""
    if not _LOG_PATH.exists():
        return []
    try:
        with _LOG_PATH.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        out = []
        for line in reversed(lines[-limit:]):
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out
    except Exception:
        return []
