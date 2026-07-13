"""Persistent, idempotent publication records."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

CHANNELS = ("youtube", "linkedin")
STATUSES = ("pending", "publishing", "handoff_ready", "published", "failed")

_DB_PATH = Path(os.getenv("PUBLICATION_DB_PATH", os.getenv("MEDIA_DB_PATH", "data/media.db")))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS publications (
            publication_id TEXT PRIMARY KEY,
            production_id  TEXT NOT NULL,
            channel        TEXT NOT NULL,
            status         TEXT NOT NULL,
            url            TEXT,
            external_id    TEXT,
            actor          TEXT,
            error          TEXT,
            meta           TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            published_at   TEXT,
            UNIQUE(production_id, channel)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_publications_production ON publications(production_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_publications_status ON publications(status)")
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    try:
        item["meta"] = json.loads(item.get("meta") or "{}")
    except Exception:
        item["meta"] = {}
    return item


def get_publication(publication_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM publications WHERE publication_id=?", (publication_id,)
        ).fetchone()
    return _row(row)


def get_for_production(production_id: str, channel: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM publications WHERE production_id=? AND channel=?",
            (production_id, channel),
        ).fetchone()
    return _row(row)


def list_publications(
    production_id: str | None = None,
    channel: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    for key, value in (("production_id", production_id), ("channel", channel), ("status", status)):
        if value:
            clauses.append(f"{key}=?")
            params.append(value)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(int(limit), 1000)))
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM publications {where} ORDER BY created_at DESC LIMIT ?", params
        ).fetchall()
    return [_row(row) or {} for row in rows]


def create_or_get(
    production_id: str,
    channel: str,
    actor: str = "operator",
    meta: dict[str, Any] | None = None,
) -> dict:
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}")
    existing = get_for_production(production_id, channel)
    if existing:
        return existing
    publication_id = str(uuid.uuid4())
    now = _now()
    with _db() as conn:
        conn.execute(
            "INSERT INTO publications (publication_id,production_id,channel,status,url,external_id,"
            "actor,error,meta,created_at,updated_at,published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                publication_id,
                production_id,
                channel,
                "pending",
                None,
                None,
                actor,
                None,
                json.dumps(meta or {}, ensure_ascii=False),
                now,
                now,
                None,
            ),
        )
    return get_publication(publication_id) or {}


def update_publication(publication_id: str, **fields: Any) -> dict:
    allowed = {"status", "url", "external_id", "actor", "error", "meta", "published_at"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"field {key!r} is not editable")
        if key == "status" and value not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        if key == "meta":
            value = json.dumps(value or {}, ensure_ascii=False)
        sets.append(f"{key}=?")
        params.append(value)
    if not sets:
        current = get_publication(publication_id)
        if not current:
            raise KeyError("publication not found")
        return current
    sets.append("updated_at=?")
    params.extend([_now(), publication_id])
    with _db() as conn:
        cur = conn.execute(
            f"UPDATE publications SET {', '.join(sets)} WHERE publication_id=?", params
        )
    if cur.rowcount == 0:
        raise KeyError("publication not found")
    return get_publication(publication_id) or {}


def mark_published(
    publication_id: str,
    *,
    url: str,
    external_id: str | None = None,
    actor: str = "operator",
    meta: dict[str, Any] | None = None,
) -> dict:
    return update_publication(
        publication_id,
        status="published",
        url=url,
        external_id=external_id,
        actor=actor,
        error=None,
        meta=meta or {},
        published_at=_now(),
    )
