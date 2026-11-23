"""Phase 2: records store, Google sync, extraction, drafts and holds behind approval.

Addresses are example.com placeholders; the owner is OWNER."""

from __future__ import annotations

import asyncio
import base64
import email
from datetime import datetime, timezone
from pathlib import Path

import pytest

OWNER = "owner@example.com"
PA = "person.a@example.org"
PB = "person.b@example.net"


@pytest.fixture
def rec(tmp_path, monkeypatch):
    import orchestrator.records as records
    monkeypatch.setattr(records, "DB_PATH", tmp_path / "records.db")
    monkeypatch.delenv("GOOGLE_ACCOUNT_EMAIL", raising=False)
    return records


class _Call:
    def __init__(self, result, log=None, name=None, kwargs=None):
        self._r, self._log, self._name, self._kw = result, log, name, kwargs

    def execute(self):
        if self._log is not None:
            self._log.append((self._name, self._kw))
        return self._r


class FakeCalendar:
    def __init__(self, items):
        self.items, self.calls = items, []

    def events(self):
        outer = self

        class E:
            def list(self, **kw):
                return _Call({"items": outer.items}, outer.calls, "list", kw)

            def insert(self, **kw):
                return _Call({"id": "ev-new", "htmlLink": "https://calendar.example/ev-new"}, outer.calls, "insert", kw)
        return E()


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _msg(mid, *, frm, to, subject, body, labels=("INBOX",), date="Mon, 23 Nov 2026 09:00:00 +1100", extra=None):
    headers = [{"name": "From", "value": frm}, {"name": "To", "value": to},
               {"name": "Subject", "value": subject}, {"name": "Date", "value": date}] + (extra or [])
    return {"id": mid, "threadId": "t-" + mid, "labelIds": list(labels), "snippet": body[:50],
            "payload": {"mimeType": "multipart/alternative", "headers": headers,
                        "parts": [{"mimeType": "text/plain", "body": {"data": _b64(body)}},
                                  {"mimeType": "text/html", "body": {"data": _b64("<p>html</p>")}}]}}


class FakeGmail:
    def __init__(self, msgs):
        self.msgs = {m["id"]: m for m in msgs}
        self.gets, self.calls = [], []

    def users(self):
        outer = self

        class Drafts:
            def create(self, **kw):
                return _Call({"id": "d-1"}, outer.calls, "drafts.create", kw)

        class Messages:
            def list(self, **kw):
                return _Call({"messages": [{"id": i} for i in outer.msgs]})

            def get(self, userId, id, format):
                outer.gets.append(id)
                return _Call(outer.msgs[id])

        class U:
            def getProfile(self, userId):
                return _Call({"emailAddress": OWNER})

            def messages(self):
                return Messages()

            def drafts(self):
                return Drafts()
        return U()


# ---------------------------------------------------------------------------
# Records store
# ---------------------------------------------------------------------------

def test_item_upsert_reports_new_same_changed(rec):
    kw = dict(source="gmail", source_id="1", kind="email", data_class="personal", title="Hi", body="a")
    assert rec.upsert_item(**kw) == "new"
    rec.mark_processed("gmail:1")
    assert rec.upsert_item(**kw) == "same"
    assert rec.get_item("gmail:1")["processed_at"]
    assert rec.upsert_item(**{**kw, "body": "b"}) == "changed"
    assert rec.get_item("gmail:1")["processed_at"] is None


def test_record_status_survives_reupsert(rec):
    rid = rec.upsert_record("commitment", "k1", title="Send deck", data_class="personal", owner="me")
    rec.set_status(rid, "done")
    rec.upsert_record("commitment", "k1", title="Send deck", data_class="personal", owner="me")
    assert rec.get_record(rid)["status"] == "done"


def test_touch_person_counts_contacts(rec):
    rec.touch_person(PA.upper(), "Person A", seen_at="2026-11-20T00:00:00+00:00", data_class="personal")
    rec.touch_person(PA, "", seen_at="2026-11-22T00:00:00+00:00", data_class="personal")
    people = rec.list_records(kind="person")
    assert len(people) == 1
    assert people[0]["title"] == "Person A"
    assert people[0]["meta"]["contacts"] == 2
    assert people[0]["meta"]["last_contact"].startswith("2026-11-22")
    assert rec.touch_person("not-an-address", "x", data_class="personal") is None


def test_today_buckets(rec):
    now = datetime(2026, 11, 23, 1, 0, tzinfo=timezone.utc)  # 12:00 Melbourne
    rec.upsert_record("meeting", "m1", title="Coffee", data_class="personal", start_at="2026-11-23T15:00:00+11:00")
    rec.upsert_record("meeting", "m2", title="Tomorrow", data_class="personal", start_at="2026-11-24T09:00:00+11:00")
    rec.upsert_record("meeting", "m3", title="Cancelled", data_class="personal",
                      start_at="2026-11-23T10:00:00+11:00", status="cancelled")
    rec.upsert_record("commitment", "c1", title="Overdue", data_class="personal", owner="me", due_at="2026-11-20")
    rec.upsert_record("commitment", "c2", title="Soon", data_class="personal", owner="me", due_at="2026-11-27")
    rec.upsert_record("commitment", "c3", title="Later", data_class="personal", owner="me", due_at="2026-12-20")
    rec.upsert_record("commitment", "c4", title="They owe", data_class="personal", owner="other")
    t = rec.today(now=now)
    assert t["date"] == "2026-11-23"
    assert [m["title"] for m in t["meetings"]] == ["Coffee"]
    assert [c["title"] for c in t["due"]] == ["Overdue"]
    assert [c["title"] for c in t["upcoming"]] == ["Soon"]
    assert [c["title"] for c in t["waiting_on"]] == ["They owe"]


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def test_sync_calendar_records_meetings_and_people(rec):
    from orchestrator import google_sync
    cal = FakeCalendar([
        {"id": "e1", "summary": "Board prep", "start": {"dateTime": "2026-11-23T15:00:00+11:00"},
         "end": {"dateTime": "2026-11-23T16:00:00+11:00"}, "status": "confirmed",
         "attendees": [{"email": OWNER, "self": True}, {"email": PA.upper(), "displayName": "Person A"}]},
        {"id": "e2", "summary": "Dropped", "start": {"date": "2026-11-24"}, "end": {"date": "2026-11-25"},
         "status": "cancelled"},
    ])
    out = google_sync.sync_calendar(cal, now=datetime(2026, 11, 23, tzinfo=timezone.utc))
    assert out["events"] == 2 and out["new"] == 2 and out["cancelled"] == 1
    meetings = {m["title"]: m for m in rec.list_records(kind="meeting")}
    assert meetings["Board prep"]["status"] == "open"
    assert meetings["Board prep"]["meta"]["attendees"] == ["Person A"]
    assert meetings["Dropped"]["status"] == "cancelled"
    assert [p["counterparty"] for p in rec.list_records(kind="person")] == [PA]
    assert cal.calls[0][1]["calendarId"] == "primary"
    assert google_sync.sync_calendar(cal, now=datetime(2026, 11, 23, tzinfo=timezone.utc))["new"] == 0


def test_sync_gmail_items_people_bulk_and_no_refetch(rec):
    from orchestrator import google_sync
    gm = FakeGmail([
        _msg("m1", frm=f"Person A <{PA}>", to=OWNER, subject="Workshop", body="Can you send the agenda by Friday?"),
        _msg("m2", frm=f"Owner <{OWNER}>", to=f"Person B <{PB}>", subject="Re: proposal",
             body="I will send the revised proposal on 1 December.", labels=("SENT",)),
        _msg("m3", frm="News <news@shop.example>", to=OWNER, subject="Sale",
             body="50% off", extra=[{"name": "List-Unsubscribe", "value": "<mailto:x@shop.example>"}]),
    ])
    out = google_sync.sync_gmail(gm)
    assert out == {"listed": 3, "new": 3, "changed": 0}
    m1 = rec.get_item("gmail:m1")
    assert m1["body"].startswith("Can you send the agenda") and m1["meta"]["direction"] == "in"
    assert rec.get_item("gmail:m2")["meta"]["direction"] == "out"
    assert rec.get_item("gmail:m3")["meta"]["bulk"] is True
    assert sorted(p["counterparty"] for p in rec.list_records(kind="person")) == sorted([PA, PB])
    assert rec.get_kv("google_account") == OWNER
    google_sync.sync_gmail(gm)
    assert gm.gets == ["m1", "m2", "m3"]


def test_extraction_writes_commitments_and_skips_bulk(rec):
    from orchestrator import google_sync
    google_sync.sync_gmail(FakeGmail([
        _msg("m1", frm=f"Person A <{PA}>", to=OWNER, subject="Workshop",
             body="Can you send the agenda by Friday? We decided to run it online."),
        _msg("m3", frm="noreply@shop.example", to=OWNER, subject="Sale", body="50% off"),
    ]))
    prompts = []

    async def llm(user, system):
        prompts.append(user)
        return ('```json\n{"commitments": [{"what": "Send the workshop agenda", "owner": "me", "who": "Person A", '
                '"due": "2026-11-27"}, {"what": "Book the room", "owner": "them", "who": "Person A", "due": "soon"}], '
                '"decisions": [{"what": "Run the workshop online", "who": "Person A"}]}\n```')

    out = asyncio.run(google_sync.extract_pending(limit=5, llm=llm))
    assert out["read"] == 1 and out["skipped"] == 1 and out["commitments"] == 2 and out["decisions"] == 1
    assert len(prompts) == 1 and "Workshop" in prompts[0]
    cs = {c["title"]: c for c in rec.list_records(kind="commitment")}
    assert cs["Send the workshop agenda"]["owner"] == "me" and cs["Send the workshop agenda"]["due_at"] == "2026-11-27"
    assert cs["Book the room"]["owner"] == "other" and cs["Book the room"]["due_at"] is None
    assert cs["Send the workshop agenda"]["source_item_id"] == "gmail:m1"
    assert rec.get_item("gmail:m3")["skip_reason"] == "bulk"
    assert rec.pending_items("email") == []


def test_extraction_gives_up_after_two_bad_answers(rec):
    from orchestrator import google_sync
    google_sync.sync_gmail(FakeGmail([_msg("m1", frm=PA, to=OWNER, subject="s", body="b")]))

    async def bad(user, system):
        return "I cannot help with that."

    asyncio.run(google_sync.extract_pending(llm=bad))
    assert rec.get_item("gmail:m1")["processed_at"] is None
    asyncio.run(google_sync.extract_pending(llm=bad))
    assert rec.get_item("gmail:m1")["skip_reason"] == "extraction_failed"


# ---------------------------------------------------------------------------
# Drafts and holds behind approval
# ---------------------------------------------------------------------------

@pytest.fixture
def gov(tmp_path, monkeypatch, rec):
    import orchestrator.governance as governance
    monkeypatch.setattr(governance, "_DB_PATH", tmp_path / "media.db")
    return governance


def test_draft_and_hold_validation(rec):
    from orchestrator import google_actions
    with pytest.raises(ValueError):
        google_actions.propose_draft(to="not an address", subject="s", body="b")
    with pytest.raises(ValueError):
        google_actions.propose_draft(to=PA, subject="", body="b")
    with pytest.raises(ValueError):
        google_actions.propose_hold(title="x", start="2026-11-24T10:00", end="2026-11-24T09:00")


def test_approved_draft_is_created_never_sent(gov, monkeypatch):
    from orchestrator import google_actions, google_link
    gm = FakeGmail([])
    monkeypatch.setattr(google_link, "service", lambda api, ver: gm)
    a = google_actions.propose_draft(to=f"Person A <{PA}>", subject="Agenda", body="Attached.", proposed_by="agent")
    items = [i for i in gov.pending()["items"] if i["gate"] == "google_action"]
    assert [i["target_id"] for i in items] == [a["action_id"]]
    assert gm.calls == []
    gov.approve("google_action", a["action_id"], actor="test")
    done = google_actions.get(a["action_id"])
    assert done["status"] == "created" and done["result"]["draft_id"] == "d-1"
    name, kw = gm.calls[0]
    assert name == "drafts.create"
    msg = email.message_from_bytes(base64.urlsafe_b64decode(kw["body"]["message"]["raw"]))
    assert msg["To"] == PA and msg["Subject"] == "Agenda"
    assert [i for i in gov.pending()["items"] if i["gate"] == "google_action"] == []


def test_rejected_hold_creates_nothing_and_approved_hold_invites_no_one(gov, monkeypatch):
    from orchestrator import google_actions, google_link
    cal = FakeCalendar([])
    monkeypatch.setattr(google_link, "service", lambda api, ver: cal)
    r = google_actions.propose_hold(title="Focus", start="2026-11-24T09:00", end="2026-11-24T11:00")
    gov.approve("google_action", r["action_id"], status="rejected")
    assert google_actions.get(r["action_id"])["status"] == "rejected" and cal.calls == []
    a = google_actions.propose_hold(title="Prep", start="2026-11-24T13:00", end="2026-11-24T14:00")
    gov.approve("google_action", a["action_id"])
    name, kw = cal.calls[0]
    assert name == "insert" and kw["sendUpdates"] == "none"
    assert kw["body"]["status"] == "tentative" and "attendees" not in kw["body"]
    assert kw["body"]["start"]["dateTime"].endswith("+11:00")


def test_google_code_has_no_send_calls():
    root = Path(__file__).resolve().parents[2] / "orchestrator"
    for name in ("google_actions.py", "google_sync.py", "google_link.py"):
        assert ".send(" not in (root / name).read_text(encoding="utf-8"), name


# ---------------------------------------------------------------------------
# Daily brief and daemon
# ---------------------------------------------------------------------------

def test_daily_brief_carries_today(rec, tmp_path, monkeypatch):
    import orchestrator.operating as operating
    import orchestrator.governance as governance
    monkeypatch.setattr(operating, "_DB_PATH", tmp_path / "media.db")
    monkeypatch.setattr(governance, "_DB_PATH", tmp_path / "media.db")
    rec.upsert_record("meeting", "m1", title="Now-ish", data_class="personal",
                      start_at=datetime.now(timezone.utc).isoformat())
    brief = operating.daily_brief()
    assert any(m["title"] == "Now-ish" for m in brief["today"]["meetings"])
    assert "meeting(s) today" in brief["summary"]


def test_daemon_google_sync_is_off_without_token(tmp_path, monkeypatch):
    import orchestrator.daemon as daemon
    import orchestrator.google_link as google_link
    import orchestrator.google_sync as google_sync
    monkeypatch.setattr(google_link, "TOKEN_PATH", tmp_path / "missing.json")
    called = []

    async def fake(**kw):
        called.append(kw)
        return {}

    monkeypatch.setattr(google_sync, "run_once", fake)
    state: dict = {}
    asyncio.run(daemon._maybe_google_sync(state))
    assert called == [] and "google_sync_at" not in state
