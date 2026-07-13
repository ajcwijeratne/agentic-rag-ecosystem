from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def publishing(tmp_path, monkeypatch):
    db = tmp_path / "media.db"
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("MEDIA_DB_PATH", str(db))
    monkeypatch.setenv("PUBLICATION_DB_PATH", str(db))
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(vault))
    monkeypatch.setenv("WRITING_PIPELINE_PATH", "09_Writing Pipline")
    from orchestrator import governance, production
    from publishers import linkedin, obsidian, service, store, youtube

    for module in (governance, production, store, linkedin, obsidian, youtube, service):
        importlib.reload(module)
    return production, governance, store, service, linkedin, youtube


@pytest.mark.asyncio
async def test_linkedin_handoff_requires_publish_gate_and_is_idempotent(publishing, monkeypatch):
    production, governance, store, service, linkedin, youtube = publishing
    pid = production.create_production("Launch post", "demo", "linkedin_short", "Aaron")
    production.update_production(pid, script={"linkedin_post": "Ready to paste"})
    production.transition(pid, "publish", "test", "manual bypass")

    with pytest.raises(PermissionError):
        await service.publish(pid, "linkedin", actor="test")

    governance.approve("public_claim", pid, "test")
    governance.approve("external_publish", pid, "test")
    calls = []

    async def fake_handoff(prod, options=None):
        calls.append(prod["production_id"])
        return {"channel": "linkedin", "status": "handoff_ready", "copy": "Ready to paste"}

    monkeypatch.setattr(linkedin, "prepare_handoff", fake_handoff)
    first = await service.publish(pid, "linkedin", actor="test")
    second = await service.publish(pid, "linkedin", actor="test")

    assert first["status"] == "handoff_ready"
    assert second["publication_id"] == first["publication_id"]
    assert calls == [pid]
    assert len(store.list_publications(production_id=pid)) == 1


@pytest.mark.asyncio
async def test_linkedin_handoff_joins_ordered_draft_sections(publishing, monkeypatch):
    production, governance, store, service, linkedin, youtube = publishing
    notifications = []

    async def fake_notify(**kwargs):
        notifications.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr(linkedin, "notify", fake_notify)
    record = {
        "production_id": "production-1",
        "title": "A governed post",
        "script": {
            "draft": {
                "script": {
                    "section_order": ["open", "body", "close"],
                    "script": {
                        "open": "Opening paragraph.",
                        "body": "Evidence paragraph.",
                        "close": "Closing paragraph.",
                    },
                }
            }
        },
        "linked_assets": [],
    }

    result = await linkedin.prepare_handoff(record)

    expected = "Opening paragraph.\n\nEvidence paragraph.\n\nClosing paragraph."
    assert result["copy"] == expected
    assert expected in notifications[0]["body"]


@pytest.mark.asyncio
async def test_obsidian_sync_starts_only_when_linkedin_handoff_is_ready(publishing, monkeypatch):
    production, governance, store, service, linkedin, youtube = publishing
    pid = production.create_production("Ready post", "demo", "linkedin_short", "Aaron")
    production.update_production(pid, script={"linkedin_post": "Final governed copy."})
    vault = service.obsidian._vault()
    assert not list(vault.rglob("*.md"))

    production.transition(pid, "publish", "test", "ready")
    governance.approve("public_claim", pid, "test")
    governance.approve("external_publish", pid, "test")

    async def fake_notify(**kwargs):
        return {"status": "ok"}

    monkeypatch.setattr(linkedin, "notify", fake_notify)
    publication = await service.publish(pid, "linkedin", actor="test")

    sync = publication["meta"]["obsidian_sync"]
    note = vault / sync["path"]
    assert publication["status"] == "handoff_ready"
    assert note.parent.name == "03_Ready"
    assert "Final governed copy." in note.read_text(encoding="utf-8")
    assert not list((vault / "09_Writing Pipline" / "00_Ideas").glob("*.md"))


def test_confirmation_moves_same_obsidian_note_to_published(publishing):
    production, governance, store, service, linkedin, youtube = publishing
    pid = production.create_production("Published post", "demo", "linkedin_short", "Aaron")
    production.update_production(pid, script={"linkedin_post": "Published copy."})
    production.transition(pid, "publish", "test", "ready")
    item = store.create_or_get(pid, "linkedin", "test")
    item = store.update_publication(
        item["publication_id"],
        status="handoff_ready",
        meta={"copy": "Published copy."},
    )
    item = service._sync_obsidian(production.get_production(pid), item)
    ready_path = service.obsidian._vault() / item["meta"]["obsidian_sync"]["path"]
    assert ready_path.is_file()

    confirmed = service.confirm_publication(
        item["publication_id"],
        url="https://www.linkedin.com/feed/update/urn:li:activity:2",
        actor="Aaron",
    )

    published_path = service.obsidian._vault() / confirmed["meta"]["obsidian_sync"]["path"]
    assert confirmed["status"] == "published"
    assert published_path.parent.name == "04_Published"
    assert published_path.is_file()
    assert not ready_path.exists()
    assert "urn:li:activity:2" in published_path.read_text(encoding="utf-8")


def test_manual_confirmation_records_url_and_timestamp(publishing):
    production, governance, store, service, linkedin, youtube = publishing
    item = store.create_or_get("prod-1", "linkedin", "test")
    item = store.update_publication(item["publication_id"], status="handoff_ready")
    confirmed = service.confirm_publication(
        item["publication_id"],
        url="https://www.linkedin.com/feed/update/urn:li:activity:1",
        actor="Aaron",
        note="posted from phone",
    )

    assert confirmed["status"] == "published"
    assert confirmed["published_at"]
    assert confirmed["meta"]["confirmation_note"] == "posted from phone"


@pytest.mark.asyncio
async def test_failed_youtube_upload_is_persisted(publishing, monkeypatch):
    production, governance, store, service, linkedin, youtube = publishing
    pid = production.create_production("Video", "demo", "talking_head_clip", "Aaron")
    production.transition(pid, "publish", "test", "ready")
    governance.approve("public_claim", pid, "test")
    governance.approve("external_publish", pid, "test")

    async def fail_upload(prod, options=None):
        raise RuntimeError("YouTube OAuth is not configured")

    monkeypatch.setattr(youtube, "upload", fail_upload)
    with pytest.raises(RuntimeError, match="OAuth"):
        await service.publish(pid, "youtube", actor="test")

    saved = store.get_for_production(pid, "youtube")
    assert saved["status"] == "failed"
    assert "OAuth" in saved["error"]


@pytest.mark.asyncio
async def test_transition_to_publish_dispatches_declared_targets(publishing, monkeypatch):
    production, governance, store, service, linkedin, youtube = publishing
    pid = production.create_production(
        "Targeted post",
        "demo",
        "linkedin_short",
        "Aaron",
        publish_targets=[{"channel": "linkedin"}],
    )
    production.transition(pid, "review", "test", "ready")
    governance.approve("public_claim", pid, "test")
    governance.approve("external_publish", pid, "test")

    async def fake_agent(**kwargs):
        return {"answer": "{}"}

    async def fake_targets(prod, actor="operator"):
        return [{"channel": "linkedin", "status": "handoff_ready"}]

    monkeypatch.setattr(production, "call_wijerco_agent", fake_agent)
    monkeypatch.setattr(service, "publish_targets", fake_targets)
    result = await production.advance(pid, actor="test")

    assert result["production"]["state"] == "publish"
    publication_output = result["agent_output"][-1]
    assert publication_output["subagent"] == "publication-service"
    assert publication_output["output"][0]["status"] == "handoff_ready"
