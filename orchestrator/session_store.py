"""
Session Store — SQLite-backed conversation persistence
======================================================
Provides:
  • create_session()    — new session with optional title
  • add_message()       — append a turn to a session
  • get_messages()      — full history for a session
  • list_sessions()     — all sessions, most-recent first
  • delete_session()    — purge one session
  • expire_old()        — remove sessions older than TTL_HOURS (auto-called on startup)

Schema:
  sessions  (id TEXT PK, title TEXT, created_at REAL, updated_at REAL)
  messages  (id INTEGER PK, session_id TEXT FK, role TEXT, content TEXT,
             model_key TEXT, cost_usd REAL, ts REAL)
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

_DB_PATH   = Path(os.getenv("SESSION_DB_PATH", "data/sessions.db"))
_TTL_HOURS = float(os.getenv("SESSION_TTL_HOURS", "24"))


# ─────────────────────────────────────────────────────────────────────────────
# DB initialisation
# ─────────────────────────────────────────────────────────────────────────────

def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;

        CREATE TABLE IF NOT EXISTS sessions (
            id         TEXT PRIMARY KEY,
            title      TEXT NOT NULL DEFAULT 'New conversation',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            role       TEXT NOT NULL,      -- 'user' | 'assistant' | 'system'
            content    TEXT NOT NULL,
            model_key  TEXT,
            cost_usd   REAL DEFAULT 0.0,
            ts         REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, ts);
        CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(updated_at DESC);
    """)
    conn.commit()


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _init_db(conn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Session:
    id:         str
    title:      str
    created_at: float
    updated_at: float
    message_count: int = 0


@dataclass
class Message:
    id:         int
    session_id: str
    role:       str
    content:    str
    model_key:  str | None
    cost_usd:   float
    ts:         float


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def create_session(title: str = "New conversation") -> Session:
    now = time.time()
    sid = str(uuid.uuid4())
    with _db() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
            (sid, title, now, now),
        )
    return Session(id=sid, title=title, created_at=now, updated_at=now)


def get_session(session_id: str) -> Session | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT s.*, COUNT(m.id) AS message_count "
            "FROM sessions s LEFT JOIN messages m ON m.session_id=s.id "
            "WHERE s.id=? GROUP BY s.id",
            (session_id,),
        ).fetchone()
    if row is None:
        return None
    return Session(
        id=row["id"], title=row["title"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        message_count=row["message_count"],
    )


def list_sessions(limit: int = 50) -> list[Session]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT s.id, s.title, s.created_at, s.updated_at, COUNT(m.id) AS message_count "
            "FROM sessions s LEFT JOIN messages m ON m.session_id=s.id "
            "GROUP BY s.id ORDER BY s.updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [
        Session(id=r["id"], title=r["title"], created_at=r["created_at"],
                updated_at=r["updated_at"], message_count=r["message_count"])
        for r in rows
    ]


def add_message(
    session_id: str,
    role:       str,
    content:    str,
    model_key:  str | None = None,
    cost_usd:   float = 0.0,
) -> Message:
    """
    Append a message to a session. Creates the session if it doesn't exist.
    Auto-generates a title from the first user message.
    """
    now = time.time()
    with _db() as conn:
        # Upsert session
        existing = conn.execute("SELECT id, title FROM sessions WHERE id=?", (session_id,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?,?,?,?)",
                (session_id, "New conversation", now, now),
            )
            existing_title = "New conversation"
        else:
            existing_title = existing["title"]

        # Auto-title from first user message
        if role == "user" and existing_title == "New conversation":
            title = content[:60].strip().replace("\n", " ")
            if len(content) > 60:
                title += "…"
            conn.execute(
                "UPDATE sessions SET title=?, updated_at=? WHERE id=?",
                (title, now, session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET updated_at=? WHERE id=?",
                (now, session_id),
            )

        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, model_key, cost_usd, ts) "
            "VALUES (?,?,?,?,?,?)",
            (session_id, role, content, model_key, cost_usd, now),
        )
        msg_id = cursor.lastrowid

    return Message(
        id=msg_id, session_id=session_id, role=role, content=content,
        model_key=model_key, cost_usd=cost_usd, ts=now,
    )


def get_messages(session_id: str, limit: int = 100) -> list[Message]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY ts ASC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [
        Message(id=r["id"], session_id=r["session_id"], role=r["role"],
                content=r["content"], model_key=r["model_key"],
                cost_usd=r["cost_usd"], ts=r["ts"])
        for r in rows
    ]


def get_history_for_llm(session_id: str, max_turns: int = 20) -> list[dict]:
    """Return messages in [{role, content}] format for passing to LLM providers."""
    messages = get_messages(session_id, limit=max_turns * 2)
    return [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]


def delete_session(session_id: str) -> bool:
    with _db() as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    return cursor.rowcount > 0


def expire_old(ttl_hours: float = _TTL_HOURS) -> int:
    """Delete sessions older than ttl_hours. Returns number removed."""
    cutoff = time.time() - ttl_hours * 3600
    with _db() as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE updated_at < ?", (cutoff,))
    return cursor.rowcount


def session_cost(session_id: str) -> float:
    """Total API cost for all messages in a session."""
    with _db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) AS total FROM messages WHERE session_id=?",
            (session_id,),
        ).fetchone()
    return float(row["total"]) if row else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Startup expiry
# ─────────────────────────────────────────────────────────────────────────────

expire_old()   # run at import time so stale sessions are cleaned on restart
