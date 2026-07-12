"""
Per-request structured trace. One JSON line per request stitches the whole
pipeline together: route, agents called, latency by agent, retrieval count,
chunks after assembly, model chosen, fallback events, token use, cost, total
latency, errors, and final confidence.

Cheap to use: coarse spans (per node, per agent), best-effort writes that never
raise, and size-based rotation so logs/traces.jsonl does not grow without bound.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any

_LOG_PATH = Path(os.getenv("TRACE_LOG_PATH",
                           str(Path(__file__).parent.parent / "logs" / "traces.jsonl")))
_MAX_BYTES = int(os.getenv("TRACE_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
_lock = Lock()


class RequestTrace:
    def __init__(self, request_id: str | None = None, query: str = ""):
        self.request_id = request_id or str(uuid.uuid4())
        self.t0 = time.perf_counter()
        self.fields: dict[str, Any] = {
            "request_id": self.request_id,
            "query":      (query or "")[:200],
        }
        self.spans: dict[str, dict] = {}     # name -> {start, ms}
        self.events: list[dict] = []
        self._open: dict[str, float] = {}

    # -- spans -------------------------------------------------------------- #

    def start_span(self, name: str) -> None:
        self._open[name] = time.perf_counter()

    def end_span(self, name: str, **extra: Any) -> None:
        start = self._open.pop(name, None)
        if start is None:
            return
        rec = {"ms": round((time.perf_counter() - start) * 1000, 2)}
        rec.update(extra)
        self.spans[name] = rec

    # -- fields / events ---------------------------------------------------- #

    def set(self, key: str, value: Any) -> None:
        self.fields[key] = value

    def update(self, **kwargs: Any) -> None:
        self.fields.update(kwargs)

    def add_event(self, kind: str, **detail: Any) -> None:
        self.events.append({"kind": kind, "t_ms": round((time.perf_counter() - self.t0) * 1000, 2), **detail})

    # -- finish ------------------------------------------------------------- #

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.fields,
            "total_latency_ms": round((time.perf_counter() - self.t0) * 1000, 2),
            "spans":  self.spans,
            "events": self.events,
        }

    def finish(self) -> dict[str, Any]:
        record = self.to_dict()
        _write(record)
        return record


def _write(record: dict) -> None:
    try:
        with _lock:
            _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed()
            with _LOG_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _rotate_if_needed() -> None:
    try:
        if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > _MAX_BYTES:
            backup = _LOG_PATH.with_suffix(".jsonl.1")
            if backup.exists():
                backup.unlink()
            _LOG_PATH.rename(backup)
    except Exception:
        pass


def new_trace(query: str = "", request_id: str | None = None) -> RequestTrace:
    return RequestTrace(request_id=request_id, query=query)


def read_traces(limit: int = 50) -> list[dict]:
    """Most recent traces, newest first. Empty on any error."""
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
