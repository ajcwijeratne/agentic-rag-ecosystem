from __future__ import annotations

import importlib
import json

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    from orchestrator import governance, production
    import orchestrator.main as main

    governance = importlib.reload(governance)
    production = importlib.reload(production)
    main = importlib.reload(main)
    return TestClient(main.app), governance, production, tmp_path / "audit.jsonl"


def test_governance_pending_approve_history_and_audit(tmp_path, monkeypatch):
    client, governance, production, audit_log = _client(tmp_path, monkeypatch)
    pid = production.create_production("Publish check", "demo", "linkedin_short", "Aaron")
    production.transition(pid, "review", "test", "ready")

    pending = client.get("/governance/pending", headers={"x-api-key": "test-key"})
    assert pending.status_code == 200
    gates = {item["gate"] for item in pending.json()["items"]}
    assert {"public_claim", "external_publish"} <= gates

    approved = client.post(
        "/governance/approve",
        headers={"x-api-key": "admin-key"},
        json={"gate": "public_claim", "target_id": pid, "actor": "operator", "note": "Evidence checked"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"

    history = client.get(f"/governance/approvals?target_id={pid}", headers={"x-api-key": "test-key"})
    assert history.status_code == 200
    assert history.json()["items"][0]["gate"] == "public_claim"
    assert history.json()["items"][0]["note"] == "Evidence checked"

    records = [json.loads(line) for line in audit_log.read_text(encoding="utf-8").splitlines()]
    assert records[-1]["event"] == "governance.approve"
    assert records[-1]["gate"] == "public_claim"
