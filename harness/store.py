"""
Harness Proposal Store
======================
SQLite-backed queue of harness-edit proposals awaiting Aaron's approval, plus
the apply/backup machinery that edits the WijerCo source files on acceptance.

A proposal = "append this learned rule to this department/agent file because
this failure pattern keeps happening, and here is the regression evidence."

Accepting appends the rule under a clearly marked, reversible section in the
target file (after backing the file up). Nothing is auto-applied.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Generator

_DB_PATH    = Path(os.getenv("HARNESS_DB_PATH", "data/harness.db"))
WIJERCO_PATH = Path(os.getenv("WIJERCO_PATH", r"C:\Users\ajwij\Claude Cowork\WijerCo"))
_BACKUP_DIR = Path(os.getenv("HARNESS_BACKUP_DIR", "data/harness_backups"))

_LEARNED_HEADER = "## Learned rules (Self-Harness)"


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS proposals (
            id              TEXT PRIMARY KEY,
            created_at      REAL,
            department      TEXT,
            target_file     TEXT,
            failure_pattern TEXT,
            rule            TEXT,
            baseline_score  REAL,
            candidate_score REAL,
            heldout_delta   REAL,
            status          TEXT DEFAULT 'pending',   -- pending|accepted|rejected
            decided_at      REAL,
            evidence        TEXT                       -- json
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS iterations (
            id          TEXT PRIMARY KEY,
            ran_at      REAL,
            summary     TEXT
        )
    """)
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@dataclass
class Proposal:
    id:              str
    created_at:      float
    department:      str
    target_file:     str
    failure_pattern: str
    rule:            str
    baseline_score:  float
    candidate_score: float
    heldout_delta:   float
    status:          str
    evidence:        dict


def add_proposal(
    department:      str,
    target_file:    str,
    failure_pattern: str,
    rule:           str,
    baseline_score: float,
    candidate_score: float,
    heldout_delta:  float,
    evidence:       dict,
) -> str:
    pid = str(uuid.uuid4())
    with _db() as conn:
        conn.execute(
            "INSERT INTO proposals (id,created_at,department,target_file,failure_pattern,"
            "rule,baseline_score,candidate_score,heldout_delta,status,evidence) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)",
            (pid, time.time(), department, target_file, failure_pattern, rule,
             baseline_score, candidate_score, heldout_delta, json.dumps(evidence)),
        )
    return pid


def list_proposals(status: str | None = "pending") -> list[dict]:
    with _db() as conn:
        if status:
            rows = conn.execute("SELECT * FROM proposals WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM proposals ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["evidence"] = json.loads(d.get("evidence") or "{}")
        out.append(d)
    return out


def get_proposal(pid: str) -> dict | None:
    with _db() as conn:
        r = conn.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["evidence"] = json.loads(d.get("evidence") or "{}")
    return d


def reject_proposal(pid: str) -> bool:
    with _db() as conn:
        cur = conn.execute("UPDATE proposals SET status='rejected', decided_at=? WHERE id=? AND status='pending'",
                           (time.time(), pid))
    return cur.rowcount > 0


def accept_proposal(pid: str) -> dict:
    """
    Apply the proposal: back up the target file, append the learned rule under a
    marked section, and mark the proposal accepted. Returns a result dict.
    """
    p = get_proposal(pid)
    if not p:
        return {"status": "error", "message": "proposal not found"}
    if p["status"] != "pending":
        return {"status": "error", "message": f"proposal already {p['status']}"}

    target = WIJERCO_PATH / p["target_file"]
    if not target.exists():
        return {"status": "error", "message": f"target file missing: {target}"}

    # Backup
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = _BACKUP_DIR / f"{target.name}.{stamp}.bak"
    shutil.copy2(target, backup)

    # Append the rule under the learned-rules section
    text = target.read_text(encoding="utf-8", errors="replace")
    rule_line = f"- {p['rule'].strip()}  _(self-harness {stamp})_"
    if _LEARNED_HEADER in text:
        text = text.rstrip() + "\n" + rule_line + "\n"
    else:
        text = text.rstrip() + f"\n\n{_LEARNED_HEADER}\n\n*Rules discovered by the Self-Harness loop and approved by Aaron.*\n\n{rule_line}\n"
    target.write_text(text, encoding="utf-8")

    with _db() as conn:
        conn.execute("UPDATE proposals SET status='accepted', decided_at=? WHERE id=?",
                     (time.time(), pid))

    return {
        "status":  "accepted",
        "applied_to": str(p["target_file"]),
        "backup":  str(backup),
        "rule":    p["rule"],
    }


def log_iteration(summary: dict) -> None:
    with _db() as conn:
        conn.execute("INSERT INTO iterations (id, ran_at, summary) VALUES (?,?,?)",
                     (str(uuid.uuid4()), time.time(), json.dumps(summary)))


def list_iterations(limit: int = 20) -> list[dict]:
    with _db() as conn:
        rows = conn.execute("SELECT * FROM iterations ORDER BY ran_at DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["summary"] = json.loads(d.get("summary") or "{}")
        out.append(d)
    return out
