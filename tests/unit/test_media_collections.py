from __future__ import annotations

import importlib

from fastapi.testclient import TestClient


def test_collection_readiness_tracks_rights_and_status(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    from media import registry

    registry = importlib.reload(registry)
    ready = registry.add_asset("image", "media_input/hero.png", "upload", rights="owned", status="ready")
    risky = registry.add_asset("video", "media_input/client.mp4", "upload", rights="client_confidential", status="ready")
    cid = registry.create_collection("Launch pack", project="demo", purpose="linkedin")

    assert registry.add_to_collection(cid, ready, role="hero") is True
    assert registry.add_to_collection(cid, risky, role="b-roll") is True

    collection = registry.get_collection(cid)

    assert collection["readiness"]["total"] == 2
    assert collection["readiness"]["ready"] == 2
    assert collection["readiness"]["rights_ok"] == 1
    assert collection["readiness"]["is_ready"] is False
    assert risky in collection["readiness"]["risky_assets"]


def test_collection_routes_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    from media import registry
    import orchestrator.main as main

    registry = importlib.reload(registry)
    main = importlib.reload(main)
    asset_id = registry.add_asset("document", "media_input/brief.pdf", "upload", rights="owned", status="ready")
    client = TestClient(main.app)

    created = client.post(
        "/asset-collections",
        headers={"x-api-key": "admin-key"},
        json={"name": "Evidence pack", "project": "demo", "purpose": "proposal"},
    )
    assert created.status_code == 200
    cid = created.json()["collection_id"]

    added = client.post(
        f"/asset-collections/{cid}/assets",
        headers={"x-api-key": "admin-key"},
        json={"asset_id": asset_id, "role": "evidence"},
    )
    assert added.status_code == 200
    assert added.json()["readiness"]["is_ready"] is True

    listed = client.get("/asset-collections?project=demo", headers={"x-api-key": "test-key"})
    assert listed.status_code == 200
    assert listed.json()["items"][0]["collection_id"] == cid

    patched = client.patch(
        f"/asset-collections/{cid}",
        headers={"x-api-key": "admin-key"},
        json={"status": "ready", "purpose": "proposal pack"},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == "ready"
    assert patched.json()["purpose"] == "proposal pack"

    archived = client.delete(
        f"/asset-collections/{cid}",
        headers={"x-api-key": "admin-key"},
    )
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
