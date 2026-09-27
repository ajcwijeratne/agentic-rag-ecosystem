"""
Outputs: document requests open a live .md note in the vault.

No models and no retrieval: both seams are stubbed. The drafting job, the
files it writes, the stream the pane reads, edits, stop, and the voice socket
wiring are all exercised for real.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from orchestrator import outputs

KEY = {"x-api-key": "test-key"}

DOC = (
    "Here's the brief:\n\n"
    "# Content brief: TEQSA signals\n\n"
    "## Audience\n\nOnline program leaders — mostly deans.\n\n"
    "## Key messages\n\n- One\n- Two\n"
)


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("said, kind", [
    ("Build a content brief from the latest sector intel", "brief"),
    ("Draft a proposal for a micro-credential in learning design", "proposal"),
    ("Hey Apex, can you put together a project plan for the LMS migration?", "plan"),
    ("Apex, write up a summary of today's moderation meeting", "summary"),
    ("please prepare an agenda for Monday's leadership meeting", "agenda"),
    ("Write an email to Kari about the pilot timeline", "email"),
    ("Could you create a checklist for course launch", "checklist"),
    ("turn that into a briefing note", "brief"),
    ("Draft a decision paper on moving to trimesters", "report"),
    ("write a document on our assessment policy", "document"),
])
def test_document_requests_are_detected(said, kind):
    req = outputs.detect(said)
    assert req is not None and req.kind == kind
    assert req.words[0] < req.words[1]


@pytest.mark.parametrize("said", [
    "Summarise this week's TEQSA and sector signals",
    "What needs a reply in client support today?",
    "What should the brief say about retention?",
    "Should I write a proposal for this?",
    "Create a task to call Kari tomorrow",
    "Build a workflow that emails me the brief",
    "make a note to check the rubric",
    "explain the plan",
    "Outline the options",
    "",
])
def test_ordinary_questions_stay_chat(said):
    assert outputs.detect(said) is None


def test_titles_read_like_titles():
    assert outputs.detect("Build a content brief from the latest sector intel.").title == \
        "Content brief from the latest sector intel"
    assert outputs.detect("can you draft me a quick proposal for Swinburne please").title == \
        "Proposal for Swinburne"
    long = outputs.detect("write a report on " + "very " * 40 + "long things").title
    assert len(long) <= 72 and not long.endswith(" ")


def test_tidy_strips_chat_wrapping_and_em_dashes():
    out = outputs.tidy("```markdown\n" + DOC + "```")
    assert out.startswith("# Content brief") and "—" not in out and "Here's" not in out


def test_spoken_lines_are_short_and_plain():
    req = outputs.detect("draft a brief on retention")
    assert "brief" in outputs.spoken_start(req) and "—" not in outputs.spoken_start(req)
    assert outputs.spoken_done("brief", 823) == "The brief is ready, about 820 words. It's saved in Outputs."


# ---------------------------------------------------------------------------
# API and drafting job
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.delenv("RBAC_ROLE_KEYS", raising=False)
    monkeypatch.setattr(outputs, "SAVE_EVERY_S", 0.0)
    monkeypatch.setattr(outputs, "DELTA_EVERY_S", 0.0)
    outputs._JOBS.clear()

    async def no_context(query):
        return []

    monkeypatch.setattr(outputs, "_retrieve", no_context)
    monkeypatch.setattr(outputs, "_department", lambda text: "marketing_sales")
    monkeypatch.setattr(outputs, "_log_turn", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(outputs.router)
    with TestClient(app) as c:
        yield c, tmp_path / "vault" / "13_Command Centre" / "Outputs"
    outputs._JOBS.clear()


def _fake_stream(monkeypatch, text=DOC, delay=0.0, seen=None):
    async def gen(user_message, system, history):
        if seen is not None:
            seen.update(user=user_message, system=system)
        for piece in text.split(" "):
            if delay:
                await asyncio.sleep(delay)
            yield {"token": piece + " ", "done": False}
        yield {"token": "", "done": True, "model_key": "anthropic/claude-sonnet-4-6", "cost_usd": 0.0123}

    monkeypatch.setattr(outputs, "_stream", gen)


def _read_stream(c, oid):
    events = []
    with c.stream("GET", f"/outputs/{oid}/stream") as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data: "):
                ev = json.loads(line[6:])
                events.append(ev)
                if ev["type"] in ("doc_done", "doc_stopped", "doc_error"):
                    break
    return events


def test_draft_writes_a_live_note_in_the_vault(client, monkeypatch):
    c, folder = client
    seen = {}
    _fake_stream(monkeypatch, seen=seen)
    r = c.post("/outputs/draft", json={"request": "Build a content brief from the latest sector intel",
                                        "source": "overview"}, headers=KEY)
    assert r.status_code == 200
    start = r.json()
    assert start["type"] == "doc_start" and start["kind"] == "brief"
    oid = start["id"]
    assert oid.endswith("Content brief from the latest sector intel")
    assert start["path"] == f"13_Command Centre/Outputs/{oid}.md"
    assert start["obsidian_uri"].startswith("obsidian://open?vault=vault&file=13_Command%20Centre")

    events = _read_stream(c, oid)
    assert events[0]["type"] == "doc_start"
    streamed = "".join(e["text"] for e in events if e["type"] == "doc_delta")
    assert "## Audience" in streamed
    done = events[-1]
    assert done["type"] == "doc_done" and done["words"] > 10 and done["cost_usd"] == 0.0123
    assert done["spoken"].startswith("The brief is ready")

    note = (folder / f"{oid}.md").read_text(encoding="utf-8")
    assert note.startswith("---\n") and 'type: "output"' in note and 'status: "draft"' in note
    assert "# Content brief: TEQSA signals" in note and "Here's the brief" not in note
    assert "—" not in note
    assert "Aim for 600 to 1000 words" in seen["system"]
    assert seen["user"] == "Build a content brief from the latest sector intel"

    listed = c.get("/outputs").json()["items"]
    assert listed[0]["id"] == oid and listed[0]["status"] == "draft" and listed[0]["kind"] == "brief"
    got = c.get(f"/outputs/{oid}").json()
    assert got["content"].startswith("# Content brief")


def test_edits_save_back_to_the_same_file(client, monkeypatch):
    c, folder = client
    _fake_stream(monkeypatch)
    oid = c.post("/outputs/draft", json={"request": "draft a brief on retention"}, headers=KEY).json()["id"]
    _read_stream(c, oid)
    r = c.put(f"/outputs/{oid}", json={"content": "# Retention brief\n\nShorter now.\n"}, headers=KEY)
    assert r.status_code == 200 and r.json()["status"] == "edited" and r.json()["words"] == 4
    note = (folder / f"{oid}.md").read_text(encoding="utf-8")
    assert "Shorter now." in note and 'request: "draft a brief on retention"' in note
    d = c.get(f"/outputs/{oid}/download")
    assert d.status_code == 200 and d.headers["content-type"].startswith("text/markdown")
    assert b"Shorter now." in d.content and ".md" in d.headers["content-disposition"]


def test_stop_keeps_what_was_written(client, monkeypatch):
    c, folder = client
    _fake_stream(monkeypatch, text=" ".join(f"word{i}" for i in range(400)), delay=0.01)
    oid = c.post("/outputs/draft", json={"request": "write a report on first-year retention"},
                 headers=KEY).json()["id"]
    assert c.put(f"/outputs/{oid}", json={"content": "x"}, headers=KEY).status_code == 409
    import time as _t
    _t.sleep(0.3)
    assert c.post(f"/outputs/{oid}/stop", headers=KEY).json() == {"stopped": True}
    events = _read_stream(c, oid)
    assert events[-1]["type"] == "doc_stopped"
    note = (folder / f"{oid}.md").read_text(encoding="utf-8")
    assert 'status: "partial"' in note and "word1" in note and "word399" not in note


def test_a_second_request_gets_its_own_file(client, monkeypatch):
    c, _ = client
    _fake_stream(monkeypatch)
    a = c.post("/outputs/draft", json={"request": "draft a brief on retention"}, headers=KEY).json()["id"]
    _read_stream(c, a)
    b = c.post("/outputs/draft", json={"request": "draft a brief on retention"}, headers=KEY).json()["id"]
    _read_stream(c, b)
    assert a != b and b.endswith("(2)")


def test_writes_need_the_key_and_ids_cannot_escape(client, monkeypatch):
    c, _ = client
    _fake_stream(monkeypatch)
    assert c.post("/outputs/draft", json={"request": "draft a brief on x"}).status_code in (401, 403)
    assert c.get("/outputs/..%2F..%2Fsecrets").status_code in (400, 404)
    assert c.get("/outputs/2026-01-01 nope").status_code == 404


def test_detect_route(client):
    c, _ = client
    assert c.post("/outputs/detect", json={"text": "Draft a plan for Monday"}).json()["kind"] == "plan"
    assert c.post("/outputs/detect", json={"text": "what is the plan"}).json() == {"document": False}


def test_model_failure_is_reported_not_silent(client, monkeypatch):
    c, folder = client

    async def boom(user_message, system, history):
        raise RuntimeError("provider down")
        yield  # pragma: no cover

    monkeypatch.setattr(outputs, "_stream", boom)
    oid = c.post("/outputs/draft", json={"request": "draft a memo on the pilot"}, headers=KEY).json()["id"]
    events = _read_stream(c, oid)
    assert events[-1]["type"] == "doc_error" and "provider down" in events[-1]["message"]
    assert 'status: "error"' in (folder / f"{oid}.md").read_text(encoding="utf-8")


def test_follow_survives_a_dead_socket(tmp_path, monkeypatch):
    """The voice socket closing mid-draft must not stop the draft."""
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(outputs, "SAVE_EVERY_S", 0.0)
    monkeypatch.setattr(outputs, "DELTA_EVERY_S", 0.0)
    monkeypatch.setattr(outputs, "_department", lambda text: "operations")
    monkeypatch.setattr(outputs, "_log_turn", lambda *a, **k: None)

    async def no_context(query):
        return []

    monkeypatch.setattr(outputs, "_retrieve", no_context)
    _fake_stream(monkeypatch, delay=0.005)
    outputs._JOBS.clear()

    async def scenario():
        job = outputs.start(outputs.detect("draft an agenda for Monday"), source="voice")
        sent = []

        async def flaky_send(ev):
            sent.append(ev["type"])
            if len(sent) == 3:
                raise ConnectionError("socket gone")

        await outputs.follow(job, flaky_send)
        await job.task
        return job, sent

    job, sent = asyncio.run(scenario())
    assert sent[0] == "doc_start" and len(sent) == 3
    assert job.status == "draft" and "## Key messages" in job.content
    outputs._JOBS.clear()


def test_voice_says_one_line_and_drafts_outside_the_turn(tmp_path, monkeypatch):
    """A spoken document request: a short spoken line, the draft on its own track."""
    from orchestrator import voice

    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "vault"))
    monkeypatch.setattr(outputs, "SAVE_EVERY_S", 0.0)
    monkeypatch.setattr(outputs, "DELTA_EVERY_S", 0.0)
    monkeypatch.setattr(outputs, "_department", lambda text: "operations")
    monkeypatch.setattr(outputs, "_log_turn", lambda *a, **k: None)

    async def no_context(query):
        return []

    monkeypatch.setattr(outputs, "_retrieve", no_context)
    _fake_stream(monkeypatch, delay=0.002)
    outputs._JOBS.clear()

    async def scenario():
        turn, side, followers = [], [], set()

        async def send(ev):
            turn.append(ev)

        async def send_untagged(ev):
            side.append(ev)

        assert await voice.answer_document("what is on today", "s1", send, send_untagged, followers) is False
        handled = await voice.answer_document("Apex, draft an agenda for Monday's meeting", "s1",
                                              send, send_untagged, followers)
        assert handled and len(followers) == 1
        await asyncio.gather(*list(followers))
        return turn, side

    turn, side = asyncio.run(scenario())
    assert [e["type"] for e in turn] == ["speak", "answer"]
    assert turn[0]["text"].startswith("On it.") and turn[1]["doc"]["kind"] == "agenda"
    kinds = [e["type"] for e in side]
    assert kinds[0] == "doc_start" and "doc_delta" in kinds and kinds[-1] == "doc_done"
    assert side[-1]["spoken"].startswith("The agenda is ready")
    outputs._JOBS.clear()
