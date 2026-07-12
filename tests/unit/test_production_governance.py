from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("MEDIA_DERIVED_ROOT", str(tmp_path / "derived"))
    monkeypatch.setenv("REMOTION_DIR", str(tmp_path / "missing-remotion"))
    from media import registry, render
    from media.adapters import gateway
    from orchestrator import governance, production

    for module in (registry, render, governance, production, gateway):
        importlib.reload(module)
    return production, governance, registry, render, gateway


@pytest.mark.asyncio
async def test_state_machine_advances_and_writes_slices(stores, monkeypatch):
    production, governance, registry, render, gateway = stores

    async def fake_agent(**kwargs):
        slug = kwargs["subagent"]
        return {"answer": f'{{"{slug.replace("-", "_")}": "ok"}}'}

    monkeypatch.setattr(production, "call_wijerco_agent", fake_agent)
    pid = production.create_production("Launch post", "demo", "linkedin_short", "Aaron")

    seen = []
    for _ in range(7):
        result = await production.advance(pid, actor="test")
        seen.append(result["production"]["state"])

    assert seen == ["brief", "research", "outline", "draft", "asset_plan", "render", "review"]
    blocked = await production.advance(pid, actor="test")
    assert blocked["blocked"] is True
    governance.approve("public_claim", pid, "test", "")
    governance.approve("external_publish", pid, "test", "")
    result = await production.advance(pid, actor="test")
    assert result["production"]["state"] == "publish"
    prod = production.get_production(pid)
    assert prod["brief"]
    assert prod["research"]
    assert prod["script"]
    assert prod["asset_plan"]
    assert prod["edit_plan"]
    assert prod["review"]
    assert len(prod["events"]) == 8


def test_backward_transition_and_filters(stores):
    production, governance, registry, render, gateway = stores
    pid = production.create_production("Draft", "alpha", "policy_briefing", "Aaron")
    production.transition(pid, "review", "test", "manual")
    production.transition(pid, "draft", "test", "needs changes")

    prod = production.get_production(pid)
    assert prod["state"] == "draft"
    assert len(prod["events"]) == 2
    assert [p["production_id"] for p in production.list_productions(state="draft")] == [pid]
    assert [p["production_id"] for p in production.list_productions(project="alpha")] == [pid]


def test_board_envelope_shape(stores):
    production, governance, registry, render, gateway = stores
    production.create_production("Board card", None, "course_teaser", None)
    board = production.board()

    assert set(board) == {"Ideas", "Drafting", "In Production", "Review", "Published"}
    assert board["Ideas"][0]["title"] == "Board card"
    assert board["Ideas"][0]["cap"] == "course_teaser"
    assert board["Ideas"][0]["next_action"] == "Build brief"
    assert board["Ideas"][0]["gate_status"] == "clear"
    assert board["Ideas"][0]["asset_status"] == "none"
    assert board["Ideas"][0]["priority"] > 0


@pytest.mark.asyncio
async def test_publish_gate_blocks_and_approval_unblocks(stores, monkeypatch):
    production, governance, registry, render, gateway = stores

    async def fake_agent(**kwargs):
        return {"answer": "{}"}

    monkeypatch.setattr(production, "call_wijerco_agent", fake_agent)
    pid = production.create_production("Gate me", None, "linkedin_short", None)
    production.transition(pid, "review", "test", "ready")

    blocked = await production.advance(pid, actor="test")
    assert blocked["blocked"] is True
    assert blocked["gate"] == "public_claim"

    governance.approve("public_claim", pid, "test", "")
    governance.approve("external_publish", pid, "test", "")
    result = await production.advance(pid, actor="test")
    assert result["production"]["state"] == "publish"


@pytest.mark.asyncio
async def test_client_confidential_asset_blocks_publish(stores):
    production, governance, registry, render, gateway = stores
    asset_id = registry.add_asset(
        "document",
        "client.md",
        "upload",
        rights="client_confidential",
        status="ready",
    )
    pid = production.create_production("Sensitive", None, "policy_briefing", None)
    production.update_production(pid, linked_assets=[asset_id])
    production.transition(pid, "review", "test", "ready")
    governance.approve("public_claim", pid, "test", "")
    governance.approve("external_publish", pid, "test", "")

    blocked = await production.advance(pid, actor="test")
    assert blocked["blocked"] is True
    assert blocked["gate"] == "client_sensitive"


def test_mcp_adapter_paid_gate(stores, monkeypatch):
    production, governance, registry, render, gateway = stores
    monkeypatch.setenv("ADAPTER_IMAGE", "mcp:higgsfield")

    with pytest.raises(PermissionError):
        gateway.select("image", {"target_id": "job-1"})

    governance.approve("paid_job", "job-1", "test", "")
    adapter = gateway.select("image", {"target_id": "job-1"})
    assert adapter.name == "higgsfield"


def test_canva_document_path_uses_paid_gate_and_mcp_tool(stores, monkeypatch):
    production, governance, registry, render, gateway = stores
    from media.adapters import mcp

    calls = []

    class FakeBridge:
        def call_tool(self, name, arguments=None):
            calls.append((name, arguments))
            return {"url": "https://canva.test/doc", "design_id": "doc-1"}

    monkeypatch.setenv("ADAPTER_DOCUMENT", "mcp:canva")
    monkeypatch.setenv("CANVA_DOCUMENT_CREATE_TOOL", "fake_canva_create_document")
    monkeypatch.setattr(mcp, "MCPBridge", lambda: FakeBridge())

    pid = production.create_production("One pager", "demo", "policy_briefing", "Aaron")
    prod = production.get_production(pid)

    with pytest.raises(PermissionError):
        gateway.create_document_from_production(prod)

    governance.approve("paid_job", pid, "test", "")
    result = gateway.create_document_from_production(prod, instructions="Use the final review notes")

    assert result["provider"] == "mcp:canva"
    assert result["kind"] == "document"
    assert result["url"] == "https://canva.test/doc"
    assert calls[0][0] == "fake_canva_create_document"
    assert calls[0][1]["production_id"] == pid
    assert calls[0][1]["instructions"] == "Use the final review notes"


def test_canva_presentation_copy_path(stores, monkeypatch):
    production, governance, registry, render, gateway = stores
    from media.adapters import mcp

    calls = []

    class FakeBridge:
        def call_tool(self, name, arguments=None):
            calls.append((name, arguments))
            return {"url": "https://canva.test/deck", "design_id": "deck-1"}

    monkeypatch.setenv("ADAPTER_PRESENTATION", "mcp:canva")
    monkeypatch.setenv("CANVA_PRESENTATION_COPY_TOOL", "fake_canva_copy_presentation")
    monkeypatch.setattr(mcp, "MCPBridge", lambda: FakeBridge())

    pid = production.create_production("Sales deck", "demo", "proposal_walkthrough", "Aaron")
    governance.approve("paid_job", pid, "test", "")
    prod = production.get_production(pid)

    result = gateway.copy_presentation_from_production(prod, "template-123", share=True)

    assert result["provider"] == "mcp:canva"
    assert result["kind"] == "presentation"
    assert result["design_id"] == "deck-1"
    assert calls[0][0] == "fake_canva_copy_presentation"
    assert calls[0][1]["template_id"] == "template-123"
    assert calls[0][1]["output"]["share"] is True


@pytest.mark.asyncio
async def test_generated_media_blocks_publish_until_reviewed(stores, monkeypatch):
    production, governance, registry, render, gateway = stores

    async def fake_agent(**kwargs):
        return {"answer": "{}"}

    monkeypatch.setattr(production, "call_wijerco_agent", fake_agent)
    asset_id = registry.add_asset(
        "image",
        "generated.png",
        "derived",
        rights="owned",
        status="ready",
        tags=["generated", "image"],
        meta={
            "governance": {
                "generated": True,
                "generation_capability": "image",
                "review_status": "pending",
                "gate": "generated_image",
            }
        },
    )
    pid = production.create_production("Generated", None, "linkedin_short", None)
    production.update_production(pid, linked_assets=[asset_id])
    production.transition(pid, "review", "test", "ready")
    governance.approve("public_claim", pid, "test", "")
    governance.approve("external_publish", pid, "test", "")

    blocked = await production.advance(pid, actor="test")
    assert blocked["blocked"] is True
    assert blocked["gate"] == "generated_image"
    assert blocked["pending"][0]["assets"][0]["asset_id"] == asset_id

    governance.approve("generated_image", pid, "reviewer", "Looks good")
    reviewed = registry.get_asset(asset_id)
    assert reviewed["meta"]["governance"]["review_status"] == "approved"
    result = await production.advance(pid, actor="test")
    assert result["production"]["state"] == "publish"
