"""
Media Asset Registry
====================
SQLite-backed source of truth for every media file the system has touched:
where it came from, what rights attach to it, and which indexes point at it.

The ingestion, retrieval, production-pipeline, and governance layers all read
from here. One asset row per file; transcripts and parent/child relations live
in companion tables.

Mirrors the access style of harness/store.py: a @contextmanager connection,
sqlite3.Row rows, CREATE TABLE IF NOT EXISTS on open, and JSON-serialised dict
or list columns. WAL is on so the ingestion services can read while one writer
appends.

Tables:
  assets       — one row per media file
  transcripts  — timestamped transcript per audio/video asset
  asset_links  — parent/child relations (keyframe_of, audio_of, ...)
  asset_moments — timeline/section records for clips, slides, and web captures
  collections  — named project/content packs of assets
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

_DB_PATH = Path(os.getenv("MEDIA_DB_PATH", "data/media.db"))

# Allowed enum values. Writes that fall outside these raise ValueError, so a
# typo never reaches a query filter that silently returns nothing.
ASSET_TYPES = ("audio", "video", "image", "slide_deck", "web_page", "document")
RIGHTS = ("owned", "licensed", "client_confidential", "third_party", "unknown")
STATUSES = ("ingesting", "ready", "quarantined", "failed", "archived")
RELATIONS = ("derived_from", "keyframe_of", "audio_of", "thumbnail_of")


def _now() -> str:
    """ISO 8601 UTC, second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS assets (
            asset_id      TEXT PRIMARY KEY,
            type          TEXT NOT NULL,
            path          TEXT NOT NULL,
            source        TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            duration      REAL,
            dimensions    TEXT,
            transcript_id TEXT,
            embedding_ids TEXT,                    -- json: {collection: [point_id, ...]}
            rights        TEXT NOT NULL DEFAULT 'unknown',
            status        TEXT NOT NULL DEFAULT 'ingesting',
            project       TEXT,
            tags          TEXT,                    -- json: [str, ...]
            meta          TEXT                     -- json: worker metadata (scenes, slides, url)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS transcripts (
            transcript_id TEXT PRIMARY KEY,
            asset_id      TEXT NOT NULL,
            language      TEXT,
            segments      TEXT,                    -- json: [{start,end,text,speaker}, ...]
            text          TEXT,
            created_at    TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asset_links (
            id              TEXT PRIMARY KEY,
            asset_id        TEXT NOT NULL,         -- the child
            linked_asset_id TEXT NOT NULL,         -- the parent
            relation        TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asset_moments (
            moment_id      TEXT PRIMARY KEY,
            asset_id       TEXT NOT NULL,
            kind           TEXT NOT NULL,
            label          TEXT,
            t_start        REAL,
            t_end          REAL,
            text           TEXT,
            thumbnail_path TEXT,
            child_asset_id TEXT,
            meta           TEXT,
            created_at     TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS asset_collections (
            collection_id TEXT PRIMARY KEY,
            name          TEXT NOT NULL,
            project       TEXT,
            purpose       TEXT,
            status        TEXT NOT NULL DEFAULT 'draft',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            meta          TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS collection_assets (
            collection_id TEXT NOT NULL,
            asset_id      TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'reference',
            added_at      TEXT NOT NULL,
            PRIMARY KEY (collection_id, asset_id)
        )
    """)
    # Indexes for the common filter paths.
    conn.execute("CREATE INDEX IF NOT EXISTS ix_assets_type    ON assets(type)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_assets_project ON assets(project)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_assets_status  ON assets(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_links_asset    ON asset_links(asset_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_links_parent   ON asset_links(linked_asset_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_moments_asset  ON asset_moments(asset_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_moments_kind   ON asset_moments(kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_tx_asset       ON transcripts(asset_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_col_project    ON asset_collections(project)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_col_assets_col ON collection_assets(collection_id)")
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Serialisation helpers
# --------------------------------------------------------------------------- #

def _row_to_asset(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["embedding_ids"] = json.loads(d.get("embedding_ids") or "{}")
    d["tags"] = json.loads(d.get("tags") or "[]")
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d


def _validate(type_: str, rights: str, status: str) -> None:
    if type_ not in ASSET_TYPES:
        raise ValueError(f"type must be one of {ASSET_TYPES}, got {type_!r}")
    if rights not in RIGHTS:
        raise ValueError(f"rights must be one of {RIGHTS}, got {rights!r}")
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #

def add_asset(
    type_:      str,
    path:       str,
    source:     str,
    *,
    duration:   float | None = None,
    dimensions: str | None = None,
    rights:     str = "unknown",
    status:     str = "ingesting",
    project:    str | None = None,
    tags:       list[str] | None = None,
    meta:       dict | None = None,
    asset_id:   str | None = None,
) -> str:
    """Insert a new asset row and return its id."""
    _validate(type_, rights, status)
    aid = asset_id or str(uuid.uuid4())
    now = _now()
    with _db() as conn:
        conn.execute(
            "INSERT INTO assets (asset_id,type,path,source,created_at,updated_at,"
            "duration,dimensions,transcript_id,embedding_ids,rights,status,project,tags,meta) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, type_, path, source, now, now, duration, dimensions, None,
             json.dumps({}), rights, status, project, json.dumps(tags or []), json.dumps(meta or {})),
        )
    return aid


def get_asset(asset_id: str, *, with_relations: bool = True) -> dict | None:
    """Return one asset, optionally with its transcript and linked assets."""
    with _db() as conn:
        r = conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
        if not r:
            return None
        asset = _row_to_asset(r)
        if with_relations:
            if asset.get("transcript_id"):
                tr = conn.execute(
                    "SELECT * FROM transcripts WHERE transcript_id=?",
                    (asset["transcript_id"],),
                ).fetchone()
                asset["transcript"] = _row_to_transcript(tr) if tr else None
            children = conn.execute(
                "SELECT asset_id, relation FROM asset_links WHERE linked_asset_id=?",
                (asset_id,),
            ).fetchall()
            parents = conn.execute(
                "SELECT linked_asset_id, relation FROM asset_links WHERE asset_id=?",
                (asset_id,),
            ).fetchall()
            asset["children"] = [dict(c) for c in children]
            asset["parents"] = [dict(p) for p in parents]
            moments = conn.execute(
                "SELECT * FROM asset_moments WHERE asset_id=? "
                "ORDER BY COALESCE(t_start, 999999999), created_at",
                (asset_id,),
            ).fetchall()
            asset["moments"] = [_row_to_moment(m) for m in moments]
    return asset


def list_assets(
    *,
    type_:   str | None = None,
    project: str | None = None,
    status:  str | None = None,
    rights:  str | None = None,
    tag:     str | None = None,
    q:       str | None = None,
    limit:   int = 200,
) -> list[dict]:
    """Filter assets. `q` is free text over tags and transcript text."""
    clauses: list[str] = []
    params: list[Any] = []
    if type_:
        clauses.append("a.type = ?"); params.append(type_)
    if project:
        clauses.append("a.project = ?"); params.append(project)
    if status:
        clauses.append("a.status = ?"); params.append(status)
    if rights:
        clauses.append("a.rights = ?"); params.append(rights)
    if tag:
        clauses.append("a.tags LIKE ?"); params.append(f'%"{tag}"%')
    if q:
        clauses.append(
            "(a.tags LIKE ? OR EXISTS (SELECT 1 FROM transcripts t "
            "WHERE t.asset_id = a.asset_id AND t.text LIKE ?) "
            "OR EXISTS (SELECT 1 FROM asset_moments m "
            "WHERE m.asset_id = a.asset_id AND (m.text LIKE ? OR m.label LIKE ?)))"
        )
        params.extend([f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT a.* FROM assets a {where} ORDER BY a.created_at DESC LIMIT ?"
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_asset(r) for r in rows]


def update_asset(asset_id: str, **fields: Any) -> bool:
    """Patch editable fields: rights, status, project, tags, dimensions, duration."""
    allowed = {"rights", "status", "project", "tags", "dimensions", "duration",
               "embedding_ids", "transcript_id", "meta"}
    sets: list[str] = []
    params: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"field {k!r} is not editable")
        if k == "rights" and v not in RIGHTS:
            raise ValueError(f"rights must be one of {RIGHTS}")
        if k == "status" and v not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        if k in ("tags", "embedding_ids", "meta"):
            v = json.dumps(v)
        sets.append(f"{k} = ?"); params.append(v)
    if not sets:
        return False
    sets.append("updated_at = ?"); params.append(_now())
    params.append(asset_id)
    with _db() as conn:
        cur = conn.execute(f"UPDATE assets SET {', '.join(sets)} WHERE asset_id=?", params)
    return cur.rowcount > 0


def set_status(asset_id: str, status: str) -> bool:
    return update_asset(asset_id, status=status)


def set_embeddings(asset_id: str, embedding_ids: dict[str, list[str]]) -> bool:
    """Record which Qdrant points index this asset, keyed by collection."""
    return update_asset(asset_id, embedding_ids=embedding_ids)


def delete_asset(asset_id: str, *, hard: bool = False) -> bool:
    """Soft delete moves the row to 'archived'. Hard delete removes the row and
    its transcript and links."""
    if not hard:
        return set_status(asset_id, "archived")
    with _db() as conn:
        conn.execute("DELETE FROM transcripts WHERE asset_id=?", (asset_id,))
        conn.execute("DELETE FROM asset_moments WHERE asset_id=? OR child_asset_id=?",
                     (asset_id, asset_id))
        conn.execute("DELETE FROM asset_links WHERE asset_id=? OR linked_asset_id=?",
                     (asset_id, asset_id))
        cur = conn.execute("DELETE FROM assets WHERE asset_id=?", (asset_id,))
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Transcripts
# --------------------------------------------------------------------------- #

def _row_to_transcript(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["segments"] = json.loads(d.get("segments") or "[]")
    return d


def _row_to_moment(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d


def add_transcript(
    asset_id: str,
    *,
    language: str | None = None,
    segments: list[dict] | None = None,
    text:     str = "",
) -> str:
    """Write a transcript and link it back onto the asset row."""
    tid = str(uuid.uuid4())
    with _db() as conn:
        conn.execute(
            "INSERT INTO transcripts (transcript_id,asset_id,language,segments,text,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (tid, asset_id, language, json.dumps(segments or []), text, _now()),
        )
        conn.execute("UPDATE assets SET transcript_id=?, updated_at=? WHERE asset_id=?",
                     (tid, _now(), asset_id))
    return tid


def get_transcript(transcript_id: str) -> dict | None:
    with _db() as conn:
        r = conn.execute("SELECT * FROM transcripts WHERE transcript_id=?",
                         (transcript_id,)).fetchone()
    return _row_to_transcript(r) if r else None


# --------------------------------------------------------------------------- #
# Moments
# --------------------------------------------------------------------------- #

def add_moment(
    asset_id: str,
    *,
    kind: str,
    label: str | None = None,
    t_start: float | None = None,
    t_end: float | None = None,
    text: str | None = None,
    thumbnail_path: str | None = None,
    child_asset_id: str | None = None,
    meta: dict | None = None,
    moment_id: str | None = None,
) -> str:
    """Add a navigable media moment: transcript segment, keyframe, slide, page."""
    mid = moment_id or str(uuid.uuid4())
    with _db() as conn:
        if not conn.execute("SELECT 1 FROM assets WHERE asset_id=?", (asset_id,)).fetchone():
            raise ValueError("asset not found")
        conn.execute(
            "INSERT INTO asset_moments (moment_id,asset_id,kind,label,t_start,t_end,text,"
            "thumbnail_path,child_asset_id,meta,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                mid,
                asset_id,
                kind,
                label,
                t_start,
                t_end,
                text,
                thumbnail_path,
                child_asset_id,
                json.dumps(meta or {}),
                _now(),
            ),
        )
    return mid


def list_moments(
    asset_id: str,
    *,
    kind: str | None = None,
    q: str | None = None,
    limit: int = 500,
) -> list[dict]:
    clauses = ["asset_id=?"]
    params: list[Any] = [asset_id]
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if q:
        clauses.append("(text LIKE ? OR label LIKE ?)")
        params.extend([f"%{q}%", f"%{q}%"])
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM asset_moments WHERE "
            + " AND ".join(clauses)
            + " ORDER BY COALESCE(t_start, 999999999), created_at LIMIT ?",
            params,
        ).fetchall()
    return [_row_to_moment(r) for r in rows]


def delete_moments(asset_id: str) -> int:
    """Remove moment rows for an asset. Used by re-ingestion and tests."""
    with _db() as conn:
        cur = conn.execute("DELETE FROM asset_moments WHERE asset_id=?", (asset_id,))
    return cur.rowcount


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #

def add_link(asset_id: str, linked_asset_id: str, relation: str) -> str:
    """Record that `asset_id` (child) relates to `linked_asset_id` (parent)."""
    if relation not in RELATIONS:
        raise ValueError(f"relation must be one of {RELATIONS}, got {relation!r}")
    lid = str(uuid.uuid4())
    with _db() as conn:
        conn.execute(
            "INSERT INTO asset_links (id,asset_id,linked_asset_id,relation) VALUES (?,?,?,?)",
            (lid, asset_id, linked_asset_id, relation),
        )
    return lid


def get_links(asset_id: str) -> dict[str, list[dict]]:
    with _db() as conn:
        children = conn.execute(
            "SELECT asset_id, relation FROM asset_links WHERE linked_asset_id=?",
            (asset_id,)).fetchall()
        parents = conn.execute(
            "SELECT linked_asset_id, relation FROM asset_links WHERE asset_id=?",
            (asset_id,)).fetchall()
    return {
        "children": [dict(c) for c in children],
        "parents":  [dict(p) for p in parents],
    }


def stats() -> dict:
    """Counts by type and status, for the Command Centre and health checks."""
    with _db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
        by_type = conn.execute(
            "SELECT type, COUNT(*) n FROM assets GROUP BY type").fetchall()
        by_status = conn.execute(
            "SELECT status, COUNT(*) n FROM assets GROUP BY status").fetchall()
    return {
        "total":     total,
        "by_type":   {r["type"]: r["n"] for r in by_type},
        "by_status": {r["status"]: r["n"] for r in by_status},
    }


# --------------------------------------------------------------------------- #
# Collections / Project Packs
# --------------------------------------------------------------------------- #

COLLECTION_STATUSES = ("draft", "ready", "archived")


def create_collection(
    name: str,
    *,
    project: str | None = None,
    purpose: str | None = None,
    status: str = "draft",
    meta: dict | None = None,
    collection_id: str | None = None,
) -> str:
    if status not in COLLECTION_STATUSES:
        raise ValueError(f"status must be one of {COLLECTION_STATUSES}")
    cid = collection_id or str(uuid.uuid4())
    now = _now()
    with _db() as conn:
        conn.execute(
            "INSERT INTO asset_collections (collection_id,name,project,purpose,status,created_at,updated_at,meta) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (cid, name, project, purpose, status, now, now, json.dumps(meta or {})),
        )
    return cid


def add_to_collection(collection_id: str, asset_id: str, role: str = "reference") -> bool:
    with _db() as conn:
        if not conn.execute("SELECT 1 FROM asset_collections WHERE collection_id=?", (collection_id,)).fetchone():
            return False
        if not conn.execute("SELECT 1 FROM assets WHERE asset_id=?", (asset_id,)).fetchone():
            return False
        conn.execute(
            "INSERT OR REPLACE INTO collection_assets (collection_id,asset_id,role,added_at) VALUES (?,?,?,?)",
            (collection_id, asset_id, role, _now()),
        )
        conn.execute("UPDATE asset_collections SET updated_at=? WHERE collection_id=?", (_now(), collection_id))
    return True


def remove_from_collection(collection_id: str, asset_id: str) -> bool:
    with _db() as conn:
        cur = conn.execute(
            "DELETE FROM collection_assets WHERE collection_id=? AND asset_id=?",
            (collection_id, asset_id),
        )
        if cur.rowcount:
            conn.execute("UPDATE asset_collections SET updated_at=? WHERE collection_id=?", (_now(), collection_id))
    return cur.rowcount > 0


def list_collections(*, project: str | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    if project:
        clauses.append("project=?"); params.append(project)
    if status:
        clauses.append("status=?"); params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM asset_collections {where} ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_collection_row(r, include_assets=False) for r in rows]


def get_collection(collection_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM asset_collections WHERE collection_id=?",
            (collection_id,),
        ).fetchone()
        if not row:
            return None
        links = conn.execute(
            "SELECT ca.role, ca.added_at, a.* FROM collection_assets ca "
            "JOIN assets a ON a.asset_id=ca.asset_id WHERE ca.collection_id=? "
            "ORDER BY ca.added_at DESC",
            (collection_id,),
        ).fetchall()
    collection = _collection_row(row, include_assets=False)
    assets = []
    for link in links:
        asset = _row_to_asset(link)
        asset["collection_role"] = link["role"]
        asset["collection_added_at"] = link["added_at"]
        assets.append(asset)
    collection["assets"] = assets
    collection["readiness"] = collection_readiness(assets)
    return collection


def update_collection(collection_id: str, **fields: Any) -> bool:
    allowed = {"name", "project", "purpose", "status", "meta"}
    sets: list[str] = []
    params: list[Any] = []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(f"field {k!r} is not editable")
        if k == "status" and v not in COLLECTION_STATUSES:
            raise ValueError(f"status must be one of {COLLECTION_STATUSES}")
        if k == "meta":
            v = json.dumps(v or {})
        sets.append(f"{k} = ?")
        params.append(v)
    if not sets:
        return False
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(collection_id)
    with _db() as conn:
        cur = conn.execute(f"UPDATE asset_collections SET {', '.join(sets)} WHERE collection_id=?", params)
    return cur.rowcount > 0


def archive_collection(collection_id: str) -> bool:
    return update_collection(collection_id, status="archived")


def collection_readiness(assets: list[dict]) -> dict:
    total = len(assets)
    ready = sum(1 for a in assets if a.get("status") == "ready")
    rights_ok = sum(1 for a in assets if a.get("rights") in ("owned", "licensed"))
    indexed = sum(1 for a in assets if a.get("embedding_ids"))
    risky = [a["asset_id"] for a in assets if a.get("rights") in ("unknown", "third_party", "client_confidential")]
    return {
        "total": total,
        "ready": ready,
        "rights_ok": rights_ok,
        "indexed": indexed,
        "risky_assets": risky,
        "is_ready": total > 0 and ready == total and rights_ok == total,
    }


def _collection_row(row: sqlite3.Row, *, include_assets: bool = False) -> dict:
    d = dict(row)
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d
