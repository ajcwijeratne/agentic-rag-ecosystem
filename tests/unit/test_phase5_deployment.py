from __future__ import annotations

import importlib
import json
import sqlite3

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("DB_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("AGENT_RELEASES_PATH", str(tmp_path / "releases.json"))
    monkeypatch.setenv("API_KEY", "operator-key")
    monkeypatch.setenv("ADMIN_API_KEY", "admin-key")
    monkeypatch.setenv("RBAC_ROLE_KEYS", json.dumps({
        "viewer": "viewer-key",
        "operator": "operator-key",
        "admin": "admin-key",
    }))
    (tmp_path / "releases.json").write_text(
        json.dumps({"release": "test-release", "agents": {"demo": {"version": "1.0.0"}}}),
        encoding="utf-8",
    )
    import orchestrator.deployment as deployment
    import orchestrator.main as main
    import common.rbac as rbac
    import common.security as security

    deployment = importlib.reload(deployment)
    main = importlib.reload(main)
    monkeypatch.setattr(rbac, "is_loopback", lambda request: False)
    monkeypatch.setattr(security, "is_loopback", lambda request: False)
    return TestClient(main.app), deployment


def test_migration_backup_and_release_manifest(tmp_path, monkeypatch):
    client, deployment = _client(tmp_path, monkeypatch)

    migrated = deployment.migrate()
    assert migrated["schema_version"] == 1

    backup = deployment.backup_database()
    assert backup["status"] == "ok"
    assert deployment.list_backups()[0]["path"] == backup["path"]

    status = deployment.status()
    assert status["database"]["schema_version"] == 1
    assert status["releases"]["release"] == "test-release"


def test_ops_routes_enforce_roles(tmp_path, monkeypatch):
    client, deployment = _client(tmp_path, monkeypatch)

    viewer_status = client.get("/ops/status", headers={"x-api-key": "viewer-key"})
    assert viewer_status.status_code == 200

    viewer_backup = client.post("/ops/backup", headers={"x-api-key": "viewer-key"})
    assert viewer_backup.status_code == 403

    admin_backup = client.post("/ops/backup", headers={"x-api-key": "admin-key"})
    assert admin_backup.status_code == 200
    assert admin_backup.json()["status"] == "ok"

    releases = client.get("/ops/releases", headers={"x-api-key": "viewer-key"})
    assert releases.status_code == 200
    assert releases.json()["release"] == "test-release"


def test_restore_rehearsal_and_execution(tmp_path, monkeypatch):
    client, deployment = _client(tmp_path, monkeypatch)
    deployment.migrate()
    backup = deployment.backup_database()

    with sqlite3.connect(str(deployment.DB_PATH)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS restore_probe (value TEXT)")
        conn.execute("INSERT INTO restore_probe (value) VALUES ('after-backup')")
        conn.commit()

    dry_run = client.post(
        "/ops/restore",
        headers={"x-api-key": "admin-key"},
        json={"path": backup["path"]},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["status"] == "ready"
    assert dry_run.json()["dry_run"] is True

    restore = client.post(
        "/ops/restore",
        headers={"x-api-key": "admin-key"},
        json={"path": backup["path"], "dry_run": False},
    )
    assert restore.status_code == 200
    assert restore.json()["status"] == "ok"

    with sqlite3.connect(str(deployment.DB_PATH)) as conn:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='restore_probe'"
        ).fetchone()
    assert exists is None


def test_release_snapshot_and_rollback_rehearsal(tmp_path, monkeypatch):
    client, deployment = _client(tmp_path, monkeypatch)

    snapshot = client.post(
        "/ops/releases/snapshot",
        headers={"x-api-key": "admin-key"},
        json={"note": "pre-release rehearsal"},
    )
    assert snapshot.status_code == 200
    path = snapshot.json()["path"]

    dry_run = client.post(
        "/ops/releases/rollback",
        headers={"x-api-key": "admin-key"},
        json={"path": path},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["status"] == "ready"
    assert dry_run.json()["dry_run"] is True


def test_monitoring_and_rehearsal_routes(tmp_path, monkeypatch):
    client, deployment = _client(tmp_path, monkeypatch)
    deployment.migrate()
    deployment.backup_database()

    monitoring = client.get("/ops/monitoring", headers={"x-api-key": "operator-key"})
    assert monitoring.status_code == 200
    assert monitoring.json()["status"] in {"ok", "attention"}

    rehearsal = client.get("/ops/rehearsal", headers={"x-api-key": "operator-key"})
    assert rehearsal.status_code == 200
    assert "checks" in rehearsal.json()


def test_rbac_me_reports_role(tmp_path, monkeypatch):
    client, deployment = _client(tmp_path, monkeypatch)

    response = client.get("/ops/me", headers={"x-api-key": "operator-key"})

    assert response.status_code == 200
    assert response.json()["role"] == "operator"


def test_rbac_admin_key_works_for_legacy_admin_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    monkeypatch.setenv("DB_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setenv("AGENT_RELEASES_PATH", str(tmp_path / "releases.json"))
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.setenv("RBAC_ROLE_KEYS", json.dumps({
        "viewer": "viewer-key",
        "operator": "operator-key",
        "admin": "rbac-admin-key",
    }))
    import orchestrator.deployment as deployment
    import orchestrator.main as main
    import common.rbac as rbac
    import common.security as security

    importlib.reload(deployment)
    main = importlib.reload(main)
    monkeypatch.setattr(rbac, "is_loopback", lambda request: False)
    monkeypatch.setattr(security, "is_loopback", lambda request: False)
    client = TestClient(main.app)

    response = client.post(
        "/operating/plans",
        headers={"x-api-key": "rbac-admin-key"},
        json={"title": "RBAC admin route", "objective": "Verify admin role key"},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "RBAC admin route"
