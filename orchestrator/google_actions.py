"""
Google actions: Gmail drafts and tentative calendar holds, each one behind
Aaron's approval (decided 23 Nov 2026).

  propose_draft()  records a draft an agent or Aaron wants written
  propose_hold()   records a private, tentative calendar block
  pending()        proposals waiting, shown in Approvals as gate google_action
  on_gate_decision() runs when the google_action gate is decided. Approved:
                   the draft or hold is created. Rejected: nothing is created.

What this module will never do: send mail (no drafts.send or messages.send
call exists here), invite anyone (holds carry no attendees and are inserted
with sendUpdates="none"), or change or delete existing mail or events.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses
from typing import Any

from . import records

GATE = "google_action"
KINDS = ("gmail_draft", "calendar_hold")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ensure() -> None:
    with records._db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS google_actions (
                action_id   TEXT PRIMARY KEY,
                kind        TEXT NOT NULL,
                title       TEXT NOT NULL,
                payload     TEXT NOT NULL,
                status      TEXT NOT NULL,
                proposed_by TEXT,
                reason      TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                result      TEXT,
                error       TEXT
            )
        """)


def _row(r: Any) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("payload", "result"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else {}
        except Exception:
            d[k] = {}
    return d


def get(action_id: str) -> dict | None:
    _ensure()
    with records._db() as conn:
        return _row(conn.execute("SELECT * FROM google_actions WHERE action_id=?", (action_id,)).fetchone())


def list_actions(status: str | None = None, limit: int = 50) -> list[dict]:
    _ensure()
    q, args = "SELECT * FROM google_actions", []
    if status:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(limit, 500)))
    with records._db() as conn:
        return [_row(r) for r in conn.execute(q, args).fetchall()]


def _insert(kind: str, title: str, payload: dict, proposed_by: str, reason: str) -> dict:
    _ensure()
    aid = str(uuid.uuid4())
    now = _now()
    with records._db() as conn:
        conn.execute("""INSERT INTO google_actions (action_id, kind, title, payload, status, proposed_by, reason,
                        created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)""",
                     (aid, kind, title, json.dumps(payload), "proposed", proposed_by, reason, now, now))
    return get(aid)


def _addresses(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    raw = value if isinstance(value, list) else [value]
    out = [a.strip().lower() for _, a in getaddresses(raw) if a.strip()]
    bad = [a for a in out if not _EMAIL.match(a)]
    if bad:
        raise ValueError(f"not an email address: {', '.join(bad)}")
    return out


def propose_draft(*, to: str | list[str], subject: str, body: str, cc: str | list[str] | None = None,
                  reply_to_item: str | None = None, proposed_by: str = "operator", reason: str = "") -> dict:
    to_list, cc_list = _addresses(to), _addresses(cc)
    if not to_list:
        raise ValueError("a draft needs at least one recipient")
    if not (subject or "").strip() or not (body or "").strip():
        raise ValueError("a draft needs a subject and a body")
    thread_id = None
    if reply_to_item:
        item = records.get_item(reply_to_item)
        if not item or item.get("source") != "gmail":
            raise ValueError(f"unknown Gmail item: {reply_to_item}")
        thread_id = (item.get("meta") or {}).get("thread_id")
    payload = {"to": to_list, "cc": cc_list, "subject": subject.strip()[:300], "body": body.strip()[:20000],
               "thread_id": thread_id}
    return _insert("gmail_draft", f"Draft to {', '.join(to_list)}: {payload['subject']}", payload, proposed_by,
                   reason or body.strip()[:280])


def _parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=records.TZ)
    return dt


def propose_hold(*, title: str, start: str, end: str, note: str = "", proposed_by: str = "operator",
                 reason: str = "") -> dict:
    if not (title or "").strip():
        raise ValueError("a hold needs a title")
    s, e = _parse_time(start), _parse_time(end)
    if e <= s:
        raise ValueError("a hold must end after it starts")
    payload = {"title": title.strip()[:200], "start": s.isoformat(), "end": e.isoformat(), "note": (note or "")[:2000]}
    when = s.astimezone(records.TZ).strftime("%a %d %b, %I:%M%p").replace(" 0", " ")
    return _insert("calendar_hold", f"Hold: {payload['title']} ({when})", payload, proposed_by,
                   reason or f"{when} to {e.astimezone(records.TZ).strftime('%I:%M%p').lstrip('0')}, tentative, private")


def pending() -> list[dict[str, Any]]:
    """Items for governance.pending(), in the shape the Approvals views read."""
    return [{"gate": GATE, "target_id": a["action_id"], "title": a["title"], "state": a["kind"].replace("_", " "),
             "reason": a.get("reason") or "", "kind": a["kind"]} for a in list_actions(status="proposed", limit=100)]


def _set(action_id: str, status: str, *, result: dict | None = None, error: str | None = None) -> None:
    with records._db() as conn:
        conn.execute("UPDATE google_actions SET status=?, result=?, error=?, updated_at=? WHERE action_id=?",
                     (status, json.dumps(result or {}), error, _now(), action_id))


def _create_draft(payload: dict, svc: Any) -> dict:
    msg = EmailMessage()
    msg["To"] = ", ".join(payload["to"])
    if payload.get("cc"):
        msg["Cc"] = ", ".join(payload["cc"])
    msg["Subject"] = payload["subject"]
    msg.set_content(payload["body"])
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    message: dict[str, Any] = {"raw": raw}
    if payload.get("thread_id"):
        message["threadId"] = payload["thread_id"]
    draft = svc.users().drafts().create(userId="me", body={"message": message}).execute()
    return {"draft_id": draft.get("id"), "link": "https://mail.google.com/mail/u/0/#drafts"}


def _create_hold(payload: dict, svc: Any) -> dict:
    body = {"summary": payload["title"], "description": payload.get("note") or "Hold created by the WijerCo brain.",
            "start": {"dateTime": payload["start"]}, "end": {"dateTime": payload["end"]},
            "status": "tentative", "visibility": "private", "transparency": "opaque"}
    ev = svc.events().insert(calendarId="primary", body=body, sendUpdates="none").execute()
    return {"event_id": ev.get("id"), "link": ev.get("htmlLink")}


def execute(action_id: str, *, gmail: Any = None, calendar: Any = None) -> dict:
    """Create an approved action. Only called after approval."""
    from .google_link import service

    a = get(action_id)
    if not a:
        raise ValueError(f"unknown action: {action_id}")
    if a["status"] != "proposed":
        return a
    try:
        if a["kind"] == "gmail_draft":
            result = _create_draft(a["payload"], gmail or service("gmail", "v1"))
        elif a["kind"] == "calendar_hold":
            result = _create_hold(a["payload"], calendar or service("calendar", "v3"))
        else:
            raise ValueError(f"unknown action kind: {a['kind']}")
        _set(action_id, "created", result=result)
    except Exception as exc:  # noqa: BLE001 - recorded on the action
        _set(action_id, "failed", error=str(exc)[:500])
    return get(action_id)


def on_gate_decision(row: dict) -> None:
    """governance.approve() calls this for every google_action decision."""
    target = row.get("target_id")
    if not target or not get(target):
        return
    if row.get("status") == "approved":
        execute(target)
    elif row.get("status") == "rejected":
        _set(target, "rejected")
