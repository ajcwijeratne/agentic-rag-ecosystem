"""
Persistent evaluation run store.

Phase 1 uses this as the quality ledger: each run records a suite summary plus
one row per case. SQLite keeps it inspectable, durable, and cheap to query from
the Command Centre or API endpoints.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

_DB_PATH = Path(os.getenv("EVAL_DB_PATH", "data/evals.db"))


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS eval_runs (
            id          TEXT PRIMARY KEY,
            suite       TEXT NOT NULL,
            mode        TEXT NOT NULL,
            started_at  REAL NOT NULL,
            finished_at REAL,
            status      TEXT NOT NULL,
            summary     TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS eval_results (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      TEXT NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
            case_id     TEXT NOT NULL,
            suite       TEXT NOT NULL,
            target      TEXT,
            passed      INTEGER NOT NULL,
            score       REAL NOT NULL,
            latency_ms  INTEGER NOT NULL DEFAULT 0,
            cost_usd    REAL NOT NULL DEFAULT 0.0,
            issues      TEXT NOT NULL DEFAULT '[]',
            detail      TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS eval_case_states (
            suite       TEXT NOT NULL,
            case_id     TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'new',
            note        TEXT NOT NULL DEFAULT '',
            updated_at  REAL NOT NULL,
            PRIMARY KEY (suite, case_id)
        );

        CREATE INDEX IF NOT EXISTS idx_eval_runs_started
            ON eval_runs(started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_eval_results_run
            ON eval_results(run_id);
        """
    )
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def create_run(suite: str, mode: str) -> str:
    run_id = str(uuid.uuid4())
    with _db() as conn:
        conn.execute(
            "INSERT INTO eval_runs (id, suite, mode, started_at, status) VALUES (?,?,?,?,?)",
            (run_id, suite, mode, time.time(), "running"),
        )
    return run_id


def add_result(
    run_id: str,
    *,
    case_id: str,
    suite: str,
    target: str | None,
    passed: bool,
    score: float,
    latency_ms: int = 0,
    cost_usd: float = 0.0,
    issues: list[str] | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO eval_results
              (run_id, case_id, suite, target, passed, score, latency_ms, cost_usd, issues, detail)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                run_id,
                case_id,
                suite,
                target,
                1 if passed else 0,
                float(score),
                int(latency_ms),
                float(cost_usd),
                json.dumps(issues or [], ensure_ascii=False),
                json.dumps(detail or {}, ensure_ascii=False, default=str),
            ),
        )


def finish_run(run_id: str, status: str = "complete") -> dict[str, Any]:
    summary = summarise_run(run_id)
    with _db() as conn:
        conn.execute(
            "UPDATE eval_runs SET finished_at=?, status=?, summary=? WHERE id=?",
            (time.time(), status, json.dumps(summary, ensure_ascii=False), run_id),
        )
    return summary


def summarise_run(run_id: str) -> dict[str, Any]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM eval_results WHERE run_id=?",
            (run_id,),
        ).fetchall()
    total = len(rows)
    passed = sum(1 for r in rows if r["passed"])
    avg_score = sum(float(r["score"]) for r in rows) / total if total else 0.0
    total_cost = sum(float(r["cost_usd"]) for r in rows)
    avg_latency = sum(int(r["latency_ms"]) for r in rows) / total if total else 0.0
    by_suite: dict[str, dict[str, Any]] = {}
    issue_counts: dict[str, int] = {}
    for r in rows:
        bucket = by_suite.setdefault(r["suite"], {"total": 0, "passed": 0, "avg_score": 0.0})
        bucket["total"] += 1
        bucket["passed"] += int(r["passed"])
        bucket["avg_score"] += float(r["score"])
        for issue in json.loads(r["issues"] or "[]"):
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
    for bucket in by_suite.values():
        bucket["pass_rate"] = round(bucket["passed"] / bucket["total"], 4) if bucket["total"] else 0.0
        bucket["avg_score"] = round(bucket["avg_score"] / bucket["total"], 4) if bucket["total"] else 0.0
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "avg_score": round(avg_score, 4),
        "avg_latency_ms": round(avg_latency, 2),
        "cost_usd": round(total_cost, 6),
        "by_suite": by_suite,
        "top_issues": sorted(
            [{"issue": k, "count": v} for k, v in issue_counts.items()],
            key=lambda x: x["count"],
            reverse=True,
        )[:10],
    }


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM eval_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_run_row(r) for r in rows]


def get_run(run_id: str) -> dict[str, Any] | None:
    with _db() as conn:
        run = conn.execute("SELECT * FROM eval_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return None
        results = conn.execute(
            "SELECT * FROM eval_results WHERE run_id=? ORDER BY id ASC",
            (run_id,),
        ).fetchall()
    out = _run_row(run)
    out["results"] = [_result_row(r) for r in results]
    return out


_CASE_STATUSES = {"new", "triaged", "fixed", "verified"}


def update_case_state(suite: str, case_id: str, *, status: str | None = None, note: str | None = None) -> dict[str, Any]:
    """Set status/note for a logical eval case across runs."""
    if status is not None and status not in _CASE_STATUSES:
        raise ValueError(f"status must be one of {sorted(_CASE_STATUSES)}")
    current = get_case_state(suite, case_id)
    next_status = status if status is not None else current["status"]
    next_note = note if note is not None else current["note"]
    now = time.time()
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO eval_case_states (suite, case_id, status, note, updated_at)
            VALUES (?,?,?,?,?)
            ON CONFLICT(suite, case_id) DO UPDATE SET
                status=excluded.status,
                note=excluded.note,
                updated_at=excluded.updated_at
            """,
            (suite, case_id, next_status, next_note, now),
        )
    return get_case_state(suite, case_id)


def get_case_state(suite: str, case_id: str) -> dict[str, Any]:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM eval_case_states WHERE suite=? AND case_id=?",
            (suite, case_id),
        ).fetchone()
    if not row:
        return {"suite": suite, "case_id": case_id, "status": "new", "note": "", "updated_at": None}
    return {
        "suite": row["suite"],
        "case_id": row["case_id"],
        "status": row["status"],
        "note": row["note"],
        "updated_at": row["updated_at"],
    }


def list_case_states(status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    with _db() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM eval_case_states WHERE status=? ORDER BY updated_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM eval_case_states ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [
        {
            "suite": r["suite"],
            "case_id": r["case_id"],
            "status": r["status"],
            "note": r["note"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def list_case_work_items(status: str | None = None, limit: int = 200, include_verified: bool = False) -> list[dict[str, Any]]:
    """Latest eval case results joined to their triage state.

    This powers the Quality Work Queue. It includes failed cases even when no
    explicit state row exists yet, so fresh failures appear as `new`.
    """
    with _db() as conn:
        rows = conn.execute(
            """
            SELECT er.*, run.started_at, run.id AS latest_run_id
            FROM eval_results er
            JOIN eval_runs run ON run.id = er.run_id
            ORDER BY run.started_at DESC, er.id DESC
            """
        ).fetchall()

    latest: dict[tuple[str, str], sqlite3.Row] = {}
    for r in rows:
        key = (r["suite"], r["case_id"])
        if key not in latest:
            latest[key] = r

    items: list[dict[str, Any]] = []
    for r in latest.values():
        state = get_case_state(r["suite"], r["case_id"])
        if status and state["status"] != status:
            continue
        if not include_verified and state["status"] == "verified":
            continue
        if bool(r["passed"]) and state["status"] == "new":
            continue
        item = _result_row(r)
        item.update({
            "status": state["status"],
            "note": state["note"],
            "updated_at": state["updated_at"],
            "latest_run_id": r["latest_run_id"],
            "latest_started_at": r["started_at"],
        })
        items.append(item)

    order = {"new": 0, "triaged": 1, "fixed": 2, "verified": 3}
    items.sort(key=lambda x: (order.get(x["status"], 9), -(x.get("latest_started_at") or 0)))
    return items[:limit]


def latest_run(suite: str | None = None) -> dict[str, Any] | None:
    with _db() as conn:
        if suite:
            row = conn.execute(
                "SELECT * FROM eval_runs WHERE suite=? ORDER BY started_at DESC LIMIT 1",
                (suite,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM eval_runs ORDER BY started_at DESC LIMIT 1",
            ).fetchone()
    return _run_row(row) if row else None


def _run_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "suite": row["suite"],
        "mode": row["mode"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "summary": json.loads(row["summary"] or "{}"),
    }


def _result_row(row: sqlite3.Row) -> dict[str, Any]:
    case_state = get_case_state(row["suite"], row["case_id"])
    return {
        "case_id": row["case_id"],
        "suite": row["suite"],
        "target": row["target"],
        "passed": bool(row["passed"]),
        "score": row["score"],
        "latency_ms": row["latency_ms"],
        "cost_usd": row["cost_usd"],
        "issues": json.loads(row["issues"] or "[]"),
        "detail": json.loads(row["detail"] or "{}"),
        "case_status": case_state["status"],
        "case_note": case_state["note"],
        "case_updated_at": case_state["updated_at"],
    }
