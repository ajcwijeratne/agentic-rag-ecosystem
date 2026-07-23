"""Publication outcomes: the measured half of the production loop.

One row per publication (production x channel). Named engagement columns carry
the common signals; an `extra` JSON holds anything else a channel returns.
Upsert semantics: the latest numbers for a publication win and `measured_at`
moves forward. Lives in the same SQLite file as productions and publications
so a join across the three needs no cross-database work.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

_DB_PATH = Path(os.getenv("MEDIA_DB_PATH", "data/media.db"))

# Named metric columns. Anything outside this set lands in `extra` (JSON).
METRICS = ("views", "likes", "comments", "shares", "replies",
           "meeting_requests", "followers_gained")

# Word -> canonical metric. Drives both text parsing and column mapping.
_ALIASES = {
    "view": "views", "views": "views", "impression": "views", "impressions": "views",
    "like": "likes", "likes": "likes", "reaction": "likes", "reactions": "likes",
    "comment": "comments", "comments": "comments",
    "share": "shares", "shares": "shares", "repost": "shares", "reposts": "shares",
    "reply": "replies", "replies": "replies", "dm": "replies", "dms": "replies",
    "meeting": "meeting_requests", "meetings": "meeting_requests",
    "meeting_request": "meeting_requests", "meeting_requests": "meeting_requests",
    "lead": "meeting_requests", "leads": "meeting_requests",
    "follower": "followers_gained", "followers": "followers_gained",
    "follow": "followers_gained", "follows": "followers_gained",
}

# Engagement weighting. A meeting request from a personal-brand clip is worth
# far more than a view; the weights make ranking reflect business value, not
# vanity reach. Tune in one place.
_WEIGHTS = {"views": 1, "likes": 5, "comments": 12, "shares": 15, "replies": 12,
            "meeting_requests": 60, "followers_gained": 8}


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
        CREATE TABLE IF NOT EXISTS outcomes (
            outcome_id       TEXT PRIMARY KEY,
            publication_id   TEXT NOT NULL,
            production_id    TEXT NOT NULL,
            channel          TEXT NOT NULL,
            views            INTEGER,
            likes            INTEGER,
            comments         INTEGER,
            shares           INTEGER,
            replies          INTEGER,
            meeting_requests INTEGER,
            followers_gained INTEGER,
            extra            TEXT,
            source           TEXT,
            note             TEXT,
            measured_at      TEXT NOT NULL,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL,
            UNIQUE(publication_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_outcomes_production ON outcomes(production_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_outcomes_channel ON outcomes(channel)")
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
        item["extra"] = json.loads(item.get("extra") or "{}")
    except Exception:
        item["extra"] = {}
    item["engagement"] = engagement_score(item)
    return item


def _split_metrics(metrics: dict[str, Any]) -> tuple[dict[str, int], dict[str, Any]]:
    """Split a metrics dict into named columns and leftover extras."""
    named: dict[str, int] = {}
    extra: dict[str, Any] = {}
    for key, value in (metrics or {}).items():
        canon = _ALIASES.get(str(key).strip().lower(), str(key).strip().lower())
        if canon in METRICS:
            try:
                named[canon] = int(value)
            except (TypeError, ValueError):
                continue
        else:
            extra[str(key)] = value
    return named, extra


def engagement_score(item: dict[str, Any]) -> int:
    """Weighted score so ranking reflects business value, not raw reach."""
    total = 0.0
    for metric, weight in _WEIGHTS.items():
        try:
            total += float(item.get(metric) or 0) * weight
        except (TypeError, ValueError):
            continue
    return int(total)


def get_outcome(publication_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM outcomes WHERE publication_id=?", (publication_id,)
        ).fetchone()
    return _row(row)


def record_outcome(
    publication_id: str,
    production_id: str,
    channel: str,
    metrics: dict[str, Any],
    *,
    source: str = "manual",
    note: str = "",
    measured_at: str | None = None,
) -> dict:
    """Upsert one publication's outcome. Provided metrics overwrite prior
    values for those keys; untouched metrics are preserved. Latest wins."""
    named, extra = _split_metrics(metrics)
    ts = measured_at or _now()
    existing = get_outcome(publication_id)
    with _db() as conn:
        if existing:
            merged_extra = {**(existing.get("extra") or {}), **extra}
            sets = [f"{k}=?" for k in named]
            params: list[Any] = [named[k] for k in named]
            sets += ["extra=?", "source=?", "measured_at=?", "updated_at=?"]
            params += [json.dumps(merged_extra, ensure_ascii=False), source, ts, _now()]
            if note:
                sets.append("note=?")
                params.append(note)
            params.append(publication_id)
            conn.execute(
                f"UPDATE outcomes SET {', '.join(sets)} WHERE publication_id=?", params
            )
        else:
            cols = ["outcome_id", "publication_id", "production_id", "channel"]
            vals: list[Any] = [str(uuid.uuid4()), publication_id, production_id, channel]
            for metric in METRICS:
                cols.append(metric)
                vals.append(named.get(metric))
            cols += ["extra", "source", "note", "measured_at", "created_at", "updated_at"]
            vals += [json.dumps(extra, ensure_ascii=False), source, note, ts, _now(), _now()]
            placeholders = ",".join("?" for _ in cols)
            conn.execute(
                f"INSERT INTO outcomes ({','.join(cols)}) VALUES ({placeholders})", vals
            )
    return get_outcome(publication_id) or {}


def list_outcomes(
    production_id: str | None = None,
    channel: str | None = None,
    since: str | None = None,
    limit: int = 500,
) -> list[dict]:
    clauses: list[str] = []
    params: list[Any] = []
    for key, value in (("production_id", production_id), ("channel", channel)):
        if value:
            clauses.append(f"{key}=?")
            params.append(value)
    if since:
        clauses.append("measured_at>=?")
        params.append(since)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(max(1, min(int(limit), 2000)))
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM outcomes {where} ORDER BY measured_at DESC LIMIT ?", params
        ).fetchall()
    return [_row(r) or {} for r in rows]


def outcomes_for_production(production_id: str) -> dict[str, Any]:
    """Aggregate every channel outcome for one production."""
    rows = list_outcomes(production_id=production_id)
    totals = {metric: 0 for metric in METRICS}
    for row in rows:
        for metric in METRICS:
            totals[metric] += int(row.get(metric) or 0)
    return {
        "production_id": production_id,
        "channels": [r.get("channel") for r in rows],
        "totals": totals,
        "engagement": sum(int(r.get("engagement") or 0) for r in rows),
        "outcomes": rows,
    }


# ---------------------------------------------------------------------------
# Text ingestion: "outcome <id> 4200 views 38 comments [linkedin]"
# ---------------------------------------------------------------------------

_CHANNELS = ("youtube", "linkedin")
_PAIR_RE = re.compile(r"([\d][\d.,]*\s*[kKmM]?)\s+([a-zA-Z_]+)")


def _to_int(token: str) -> int | None:
    raw = token.strip().lower().replace(",", "").replace(" ", "")
    mult = 1
    if raw.endswith("k"):
        mult, raw = 1000, raw[:-1]
    elif raw.endswith("m"):
        mult, raw = 1_000_000, raw[:-1]
    try:
        return int(round(float(raw) * mult))
    except ValueError:
        return None


def parse_outcome_text(text: str) -> dict[str, Any]:
    """Parse a free-text outcome report into target, channel, and metrics."""
    body = re.sub(r"^\s*outcome\s+", "", text.strip(), flags=re.IGNORECASE)
    tokens = body.split()
    target = tokens[0] if tokens else ""
    channel = next((t.lower() for t in tokens[1:] if t.lower() in _CHANNELS), None)
    metrics: dict[str, int] = {}
    for number, word in _PAIR_RE.findall(body):
        canon = _ALIASES.get(word.strip().lower())
        value = _to_int(number)
        if canon and value is not None:
            metrics[canon] = value
    return {"target": target, "channel": channel, "metrics": metrics, "raw": text.strip()}


def record_from_text(text: str, *, source: str = "telegram") -> dict[str, Any]:
    """Resolve a pasted outcome report to publication(s) and record it.

    `target` may be a production_id or a publication_id. When a production has
    more than one channel, a channel word in the message picks which one."""
    from publishers import store as pub_store

    parsed = parse_outcome_text(text)
    target, channel, metrics = parsed["target"], parsed["channel"], parsed["metrics"]
    if not target or not metrics:
        return {"ok": False, "error": "need a target id and at least one number", **parsed}

    pubs = pub_store.list_publications(production_id=target)
    if channel:
        pubs = [p for p in pubs if p.get("channel") == channel]
    if not pubs:
        single = pub_store.get_publication(target)
        if single:
            pubs = [single]
    if not pubs:
        return {"ok": False, "error": f"no publication found for {target!r}", **parsed}
    if len(pubs) > 1 and not channel:
        return {"ok": False, "error": "production has multiple channels; add a channel word",
                "channels": [p.get("channel") for p in pubs], **parsed}

    recorded = [
        record_outcome(p["publication_id"], p["production_id"], p["channel"], metrics,
                       source=source, note=parsed["raw"])
        for p in pubs
    ]
    return {"ok": True, "recorded": recorded, "target": target,
            "channel": channel, "metrics": metrics}


# ---------------------------------------------------------------------------
# Reporting: best / worst performer for the brief
# ---------------------------------------------------------------------------

def _iso_days_ago(days: int) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _top_driver(item: dict[str, Any]) -> str:
    """The metric contributing most to the weighted score, in words."""
    best_metric, best_contrib = "", 0.0
    for metric, weight in _WEIGHTS.items():
        contrib = float(item.get(metric) or 0) * weight
        if contrib > best_contrib:
            best_metric, best_contrib = metric, contrib
    if not best_metric:
        return ""
    return f"{int(item.get(best_metric) or 0)} {best_metric.replace('_', ' ')}"


def _label(production_id: str) -> tuple[str, str]:
    try:
        from . import production as production_store

        prod = production_store.get_production(production_id)
        if prod:
            return prod.get("title") or production_id, prod.get("format") or ""
    except Exception:
        pass
    return production_id, ""


def highlight(days: int = 7, limit: int = 20) -> dict[str, Any]:
    """Best and worst performer over the window, with the driving metric.

    Falls back to all-time when the window holds fewer than two measured
    outcomes, so a quiet week still produces a line."""
    rows = list_outcomes(since=_iso_days_ago(days), limit=limit)
    scope = f"last {days} days"
    if len(rows) < 2:
        rows = list_outcomes(limit=limit)
        scope = "to date"
    ranked = sorted(rows, key=lambda r: int(r.get("engagement") or 0), reverse=True)
    if not ranked:
        return {"line": "", "best": None, "worst": None, "scope": scope, "count": 0}

    def _pack(item: dict[str, Any]) -> dict[str, Any]:
        title, fmt = _label(item.get("production_id") or "")
        return {"production_id": item.get("production_id"), "title": title, "format": fmt,
                "channel": item.get("channel"), "engagement": int(item.get("engagement") or 0),
                "driver": _top_driver(item)}

    best = _pack(ranked[0])
    line = f"Best performer ({scope}): {best['title']}"
    if best["driver"]:
        line += f", {best['driver']}"
    worst = None
    if len(ranked) >= 2:
        worst = _pack(ranked[-1])
        line += f". Weakest: {worst['title']}"
        if worst["driver"]:
            line += f", {worst['driver']}"
    line += "."
    return {"line": line, "best": best, "worst": worst, "scope": scope, "count": len(ranked)}
