"""Offline tests for consolidation and signed approval links."""

from __future__ import annotations

import asyncio
import importlib
import time

import pytest


@pytest.fixture()
def iso_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("CONSOLIDATION_STATE_PATH", str(tmp_path / "consol.json"))
    from orchestrator import operating
    importlib.reload(operating)
    yield tmp_path


# ---------------------------------------------------------------------------
# Consolidation
# ---------------------------------------------------------------------------

def test_digest_groups_by_project_and_skips_seen(iso_env, monkeypatch):
    from orchestrator import operating
    from memory import consolidation
    monkeypatch.setattr(consolidation, "STATE_PATH", iso_env / "consol.json")

    plan = operating.generate_plan_from_goal("Ship the TEQSA briefing",
                                             project="TEQSA", create=True)
    plan_id = plan["plan"]["plan_id"]
    t1 = operating.add_task(plan_id, "Research the requirement", type="agent", status="done")
    t2 = operating.add_task(None, "Standalone note", type="agent", status="done")

    summary = asyncio.run(consolidation.digest_completed_tasks())
    assert summary["tasks"] == 2
    assert summary["digests"] == 2  # TEQSA + general

    memories = operating.list_project_memory(limit=50)
    projects = {m["project"] for m in memories}
    assert "TEQSA" in projects and "general" in projects

    # Second run digests nothing new.
    summary2 = asyncio.run(consolidation.digest_completed_tasks())
    assert summary2["tasks"] == 0


def test_promotion_requires_recurrence(iso_env, monkeypatch):
    from orchestrator import operating
    from memory import consolidation
    monkeypatch.setattr(consolidation, "STATE_PATH", iso_env / "consol.json")

    stored = []

    class FakeStore:
        async def add(self, entity, content, source="x"):
            stored.append((entity, content))
            return "id"

    # memory/__init__ re-exports `store` as `memory_store`, shadowing the
    # submodule name; import_module returns the real module either way.
    ms = importlib.import_module("memory.memory_store")
    monkeypatch.setattr(ms, "store", FakeStore())

    # One-off fact: never promoted.
    operating.add_project_memory("Swinburne", "Contact prefers Tuesday meetings")
    summary = asyncio.run(consolidation.promote_repeated_facts())
    assert summary["promotions"] == 0

    # The same fact observed three times: promoted once, and only once.
    for _ in range(3):
        operating.add_project_memory("Swinburne", "Client contact prefers Tuesday meetings for reviews")
    summary = asyncio.run(consolidation.promote_repeated_facts())
    assert summary["promotions"] == 1
    assert any("Tuesday" in c for _, c in stored)

    summary = asyncio.run(consolidation.promote_repeated_facts())
    assert summary["promotions"] == 0  # hash ledger blocks re-promotion


# ---------------------------------------------------------------------------
# Approval links
# ---------------------------------------------------------------------------

def test_token_roundtrip_and_tamper(monkeypatch):
    monkeypatch.setenv("APPROVAL_LINK_SECRET", "test-secret")
    from orchestrator import inbox
    from fastapi import HTTPException

    token = inbox.make_approval_token("approve", "external_publish", "prod-9")
    assert token
    data = inbox.verify_approval_token(token)
    assert data["v"] == "approve" and data["g"] == "external_publish" and data["t"] == "prod-9"

    # Tampered signature fails.
    bad = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    with pytest.raises(HTTPException) as exc:
        inbox.verify_approval_token(bad)
    assert exc.value.status_code == 403


def test_token_expiry(monkeypatch):
    monkeypatch.setenv("APPROVAL_LINK_SECRET", "test-secret")
    from orchestrator import inbox
    from fastapi import HTTPException

    token = inbox.make_approval_token("approve", "paid_job", "x", ttl_hours=-1)
    with pytest.raises(HTTPException) as exc:
        inbox.verify_approval_token(token)
    assert exc.value.status_code == 410


def test_links_render_only_with_public_base(monkeypatch):
    monkeypatch.setenv("APPROVAL_LINK_SECRET", "test-secret")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    from orchestrator import inbox
    assert inbox.approval_links("paid_job", "x") == {}

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://minipc.tail1234.ts.net")
    links = inbox.approval_links("paid_job", "x")
    assert set(links) == {"approve", "reject"}
    assert links["approve"].startswith("https://minipc.tail1234.ts.net/governance/approve-link?token=")


def test_no_secret_disables_links(monkeypatch):
    for var in ("APPROVAL_LINK_SECRET", "ADMIN_API_KEY", "API_KEY"):
        monkeypatch.delenv(var, raising=False)
    from orchestrator import inbox
    assert inbox.make_approval_token("approve", "paid_job", "x") is None
