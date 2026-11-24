"""
Records: one store for the parts of Aaron's professional life the brain tracks.

Phase 2 of the 15 Nov 2026 audit. Connectors write two things here:

  source_items  raw items as fetched (an email, a calendar event), one row per
                source id, with a data_class ("personal" for the Google link).
  records       the four record types the rest of the system reads:
                  meeting     from calendar events
                  person      from senders, recipients and attendees
                  commitment  something owed, by Aaron ("me") or to him ("other")
                  decision    a decision stated in mail
                each linked back to the source item it came from.

Kept in its own SQLite file (RECORDS_DB_PATH, default data/records.db), which
the nightly backup already covers because it backs up data/.

today() is the input to the Today view: meetings today, commitments due or
overdue, commitments due in the next 7 days, what others owe Aaron, and recent
decisions. Times are compared in Aaron's time zone (RECORDS_TZ, default
Australia/Melbourne).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generator
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("RECORDS_DB_PATH", str(_ROOT / "data" / "records.db")))
TZ = ZoneInfo(os.getenv("RECORDS_TZ", "Australia/Melbourne"))

KINDS = ("meeting", "commitment", "person", "decision")
STATUSES = ("open", "done", "dismissed", "cancelled")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    # The daemon and the API run in separate processes and can write at the
    # same time; wait for the other writer rather than failing at once.
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS source_items (
            item_id      TEXT PRIMARY KEY,
            source       TEXT NOT NULL,
            source_id    TEXT NOT NULL,
            kind         TEXT NOT NULL,
            title        TEXT,
            body         TEXT,
            sender       TEXT,
            people       TEXT,
            start_at     TEXT,
            end_at       TEXT,
            url          TEXT,
            data_class   TEXT NOT NULL,
            meta         TEXT,
            content_hash TEXT,
            fetched_at   TEXT NOT NULL,
            processed_at TEXT,
            skip_reason  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS records (
            record_id      TEXT PRIMARY KEY,
            kind           TEXT NOT NULL,
            dedupe_key     TEXT NOT NULL UNIQUE,
            title          TEXT NOT NULL,
            detail         TEXT,
            owner          TEXT,
            counterparty   TEXT,
            due_at         TEXT,
            start_at       TEXT,
            end_at         TEXT,
            status         TEXT NOT NULL,
            source_item_id TEXT,
            data_class     TEXT NOT NULL,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            meta           TEXT
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_items_pending ON source_items(kind, processed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_records_kind ON records(kind, status)")
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in ("people", "meta"):
        if k in d:
            try:
                d[k] = json.loads(d[k]) if d[k] else ([] if k == "people" else {})
            except Exception:
                d[k] = [] if k == "people" else {}
    return d


# ---------------------------------------------------------------------------
# key/value sync state
# ---------------------------------------------------------------------------

def get_kv(key: str, default: Any = None) -> Any:
    with _db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def try_lock(name: str, ttl_seconds: int = 1800) -> bool:
    """Take a named lock shared by every process using this database. A lock
    older than ttl_seconds is treated as left behind by a crashed run."""
    now = datetime.now(timezone.utc)
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM kv WHERE key=?", (f"lock:{name}",)).fetchone()
        if row:
            try:
                held = datetime.fromisoformat(json.loads(row["value"]))
                if (now - held).total_seconds() < ttl_seconds:
                    return False
            except Exception:
                pass
        conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (f"lock:{name}", json.dumps(now.isoformat())))
        return True


def release_lock(name: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM kv WHERE key=?", (f"lock:{name}",))


def set_kv(key: str, value: Any) -> None:
    with _db() as conn:
        conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, json.dumps(value)))


# ---------------------------------------------------------------------------
# source items
# ---------------------------------------------------------------------------

def _hash(*parts: Any) -> str:
    return hashlib.sha256("\x1f".join(str(p or "") for p in parts).encode("utf-8")).hexdigest()[:24]


def upsert_item(*, source: str, source_id: str, kind: str, data_class: str, title: str = "",
                body: str = "", sender: str = "", people: list[dict] | None = None,
                start_at: str | None = None, end_at: str | None = None, url: str = "",
                meta: dict | None = None) -> str:
    """Insert or refresh one fetched item. Returns "new", "changed" or "same".

    A changed item is marked unprocessed again so extraction re-reads it."""
    item_id = f"{source}:{source_id}"
    h = _hash(title, body, start_at, end_at, json.dumps(people or [], sort_keys=True))
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT content_hash FROM source_items WHERE item_id=?", (item_id,)).fetchone()
        if old and old["content_hash"] == h:
            return "same"
        vals = (source, source_id, kind, title, body, sender, json.dumps(people or []), start_at, end_at,
                url, data_class, json.dumps(meta or {}), h, _now())
        if old:
            conn.execute("""UPDATE source_items SET source=?, source_id=?, kind=?, title=?, body=?, sender=?,
                            people=?, start_at=?, end_at=?, url=?, data_class=?, meta=?, content_hash=?,
                            fetched_at=?, processed_at=NULL, skip_reason=NULL WHERE item_id=?""",
                         (*vals, item_id))
            return "changed"
        conn.execute("""INSERT INTO source_items (source, source_id, kind, title, body, sender, people,
                        start_at, end_at, url, data_class, meta, content_hash, fetched_at, item_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (*vals, item_id))
        return "new"


def get_item(item_id: str) -> dict | None:
    with _db() as conn:
        return _row(conn.execute("SELECT * FROM source_items WHERE item_id=?", (item_id,)).fetchone())


def pending_items(kind: str, limit: int = 10) -> list[dict]:
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM source_items WHERE kind=? AND processed_at IS NULL ORDER BY start_at DESC, fetched_at DESC LIMIT ?",
            (kind, limit)).fetchall()
    return [_row(r) for r in rows]


def mark_processed(item_id: str, skip_reason: str | None = None) -> None:
    with _db() as conn:
        conn.execute("UPDATE source_items SET processed_at=?, skip_reason=? WHERE item_id=?",
                     (_now(), skip_reason, item_id))


def list_items(kind: str | None = None, limit: int = 50) -> list[dict]:
    q, args = "SELECT * FROM source_items", []
    if kind:
        q += " WHERE kind=?"
        args.append(kind)
    q += " ORDER BY COALESCE(start_at, fetched_at) DESC LIMIT ?"
    args.append(limit)
    with _db() as conn:
        return [_row(r) for r in conn.execute(q, args).fetchall()]


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

def upsert_record(kind: str, dedupe_key: str, *, title: str, data_class: str, detail: str = "",
                  owner: str | None = None, counterparty: str | None = None, due_at: str | None = None,
                  start_at: str | None = None, end_at: str | None = None, status: str | None = None,
                  source_item_id: str | None = None, meta: dict | None = None) -> str:
    """Create a record, or update the one with the same dedupe_key.

    An existing record's status is kept unless a new status is given, so a
    commitment Aaron marked done stays done when its email is re-read."""
    if kind not in KINDS:
        raise ValueError(f"unknown record kind: {kind}")
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    now = _now()
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT record_id, status, meta FROM records WHERE dedupe_key=?", (dedupe_key,)).fetchone()
        if old:
            merged = {**(json.loads(old["meta"]) if old["meta"] else {}), **(meta or {})}
            conn.execute("""UPDATE records SET title=?, detail=?, owner=?, counterparty=?, due_at=?, start_at=?,
                            end_at=?, status=?, source_item_id=COALESCE(?, source_item_id), data_class=?,
                            updated_at=?, meta=? WHERE record_id=?""",
                         (title, detail, owner, counterparty, due_at, start_at, end_at,
                          status or old["status"], source_item_id, data_class, now, json.dumps(merged),
                          old["record_id"]))
            return old["record_id"]
        rid = str(uuid.uuid4())
        conn.execute("""INSERT INTO records (record_id, kind, dedupe_key, title, detail, owner, counterparty, due_at,
                        start_at, end_at, status, source_item_id, data_class, created_at, updated_at, meta)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (rid, kind, dedupe_key, title, detail, owner, counterparty, due_at, start_at, end_at,
                      status or "open", source_item_id, data_class, now, now, json.dumps(meta or {})))
        return rid


def touch_person(email: str, name: str = "", *, seen_at: str | None = None, data_class: str,
                 source_item_id: str | None = None) -> str | None:
    """Record or refresh a person by email address, counting contacts."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return None
    key = f"person:{email}"
    with _db() as conn:
        old = conn.execute("SELECT title, meta FROM records WHERE dedupe_key=?", (key,)).fetchone()
    meta = json.loads(old["meta"]) if old and old["meta"] else {}
    meta["email"] = email
    meta["contacts"] = int(meta.get("contacts", 0)) + 1
    if seen_at and seen_at > str(meta.get("last_contact") or ""):
        meta["last_contact"] = seen_at
    title = (name or "").strip() or (old["title"] if old else email)
    return upsert_record("person", key, title=title, data_class=data_class, counterparty=email,
                         source_item_id=source_item_id, meta=meta)


def get_record(record_id: str) -> dict | None:
    with _db() as conn:
        return _row(conn.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone())


def set_status(record_id: str, status: str) -> bool:
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    with _db() as conn:
        cur = conn.execute("UPDATE records SET status=?, updated_at=? WHERE record_id=?", (status, _now(), record_id))
        return cur.rowcount > 0


def list_records(kind: str | None = None, status: str | None = None, limit: int = 100) -> list[dict]:
    q, where, args = "SELECT * FROM records", [], []
    if kind:
        where.append("kind=?")
        args.append(kind)
    if status:
        where.append("status=?")
        args.append(status)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY COALESCE(start_at, due_at, updated_at) DESC LIMIT ?"
    args.append(max(1, min(limit, 1000)))
    with _db() as conn:
        return [_row(r) for r in conn.execute(q, args).fetchall()]


def counts() -> dict[str, Any]:
    with _db() as conn:
        rec = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM records GROUP BY kind").fetchall()}
        items = {r["kind"]: r["n"] for r in conn.execute(
            "SELECT kind, COUNT(*) AS n FROM source_items GROUP BY kind").fetchall()}
        pending = conn.execute("SELECT COUNT(*) AS n FROM source_items WHERE processed_at IS NULL AND kind='email'").fetchone()["n"]
    return {"records": rec, "items": items, "emails_pending_extraction": pending}


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------

def _local_date(value: str | None) -> date | None:
    """Local calendar date of an ISO date or datetime string."""
    if not value:
        return None
    try:
        if len(value) == 10:
            return date.fromisoformat(value)
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(TZ).date()
    except Exception:
        return None


def _brief(r: dict) -> dict[str, Any]:
    keep = ("record_id", "kind", "title", "detail", "owner", "counterparty", "due_at", "start_at", "end_at",
            "status", "source_item_id")
    out = {k: r.get(k) for k in keep}
    meta = r.get("meta") or {}
    for k in ("location", "link", "attendees", "all_day", "direction"):
        if k in meta:
            out[k] = meta[k]
    return out


def today(now: datetime | None = None) -> dict[str, Any]:
    now = (now or datetime.now(timezone.utc)).astimezone(TZ)
    d0 = now.date()
    week = d0 + timedelta(days=7)
    meetings, due, upcoming, waiting, decisions = [], [], [], [], []
    for r in list_records(kind="meeting", limit=500):
        if r["status"] != "cancelled" and _local_date(r.get("start_at")) == d0:
            meetings.append(_brief(r))
    for r in list_records(kind="commitment", status="open", limit=1000):
        dd = _local_date(r.get("due_at"))
        if r.get("owner") == "other":
            waiting.append(_brief(r))
        elif dd and dd <= d0:
            due.append(_brief(r))
        elif dd and dd <= week:
            upcoming.append(_brief(r))
    since = (now - timedelta(days=7)).astimezone(timezone.utc).isoformat()
    for r in list_records(kind="decision", limit=200):
        if (r.get("created_at") or "") >= since:
            decisions.append(_brief(r))
    meetings.sort(key=lambda x: x.get("start_at") or "")
    due.sort(key=lambda x: x.get("due_at") or "")
    upcoming.sort(key=lambda x: x.get("due_at") or "")
    waiting.sort(key=lambda x: x.get("due_at") or "9999")
    return {
        "date": d0.isoformat(),
        "meetings": meetings,
        "due": due,
        "upcoming": upcoming,
        "waiting_on": waiting[:20],
        "decisions": decisions[:10],
    }
