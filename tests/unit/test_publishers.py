from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def publishing(tmp_path, monkeypatch):
    db = tmp_path / "media.db"
    monkeypatch.setenv("MEDIA_DB_PATH", str(db))
    monkeypatch.setenv("PUBLICATION_DB_PATH", str(db))
    from orchestrator import governance, production
    from publishers import linkedin, service, store, youtube

    for module in (governance, production, store, linkedin, youtube, service):
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
