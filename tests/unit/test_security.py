"""Security tests: path-traversal confinement and the admin/auth gates.

These use a minimal FastAPI app and FastAPI's TestClient with a patched client
host, so no live services are needed.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient


# ── Path traversal ──────────────────────────────────────────────────────────

def test_confine_to_roots_rejects_traversal(tmp_path):
    from common.security import confine_to_roots
    from fastapi import HTTPException
    root = tmp_path / "media"
    root.mkdir()
    # In-root resolves.
    inside = confine_to_roots(str(root / "ok.txt"), [root])
    assert str(inside).startswith(str(root.resolve()))
    # Traversal escapes -> 403.
    with pytest.raises(HTTPException) as exc:
        confine_to_roots(str(root / ".." / ".." / "etc" / "passwd"), [root])
    assert exc.value.status_code == 403


def test_confine_to_roots_rejects_absolute_escape(tmp_path):
    from common.security import confine_to_roots
    from fastapi import HTTPException
    root = tmp_path / "media"
    root.mkdir()
    with pytest.raises(HTTPException) as exc:
        confine_to_roots("/etc/shadow", [root])
    assert exc.value.status_code == 403


# ── Admin gate ──────────────────────────────────────────────────────────────

def _client_with_admin_route(monkeypatch, client_host: str):
    """Build an app with a require_admin-protected route and a fixed client host."""
    monkeypatch.setenv("ADMIN_API_KEY", "secret-admin")
    monkeypatch.setenv("API_KEY", "secret-user")
    import common.security as sec
    importlib.reload(sec)

    app = FastAPI()

    @app.delete("/danger", dependencies=[Depends(sec.require_admin)])
    def danger():
        return {"status": "executed"}

    @app.middleware("http")
    async def _force_host(request: Request, call_next):
        # Override the client tuple so loopback detection can be tested.
        request.scope["client"] = (client_host, 12345)
        return await call_next(request)

    return TestClient(app), sec


def test_admin_blocks_remote_without_key(monkeypatch):
    client, sec = _client_with_admin_route(monkeypatch, "203.0.113.7")
    try:
        r = client.delete("/danger")
        assert r.status_code == 403
        assert "executed" not in r.text
    finally:
        importlib.reload(sec)


def test_admin_blocks_remote_with_wrong_key(monkeypatch):
    client, sec = _client_with_admin_route(monkeypatch, "203.0.113.7")
    try:
        r = client.delete("/danger", headers={"x-api-key": "wrong"})
        assert r.status_code == 403
    finally:
        importlib.reload(sec)


def test_admin_allows_remote_with_correct_key(monkeypatch):
    client, sec = _client_with_admin_route(monkeypatch, "203.0.113.7")
    try:
        r = client.delete("/danger", headers={"x-api-key": "secret-admin"})
        assert r.status_code == 200
        assert r.json()["status"] == "executed"
    finally:
        importlib.reload(sec)


def test_admin_allows_loopback(monkeypatch):
    client, sec = _client_with_admin_route(monkeypatch, "127.0.0.1")
    try:
        r = client.delete("/danger")
        assert r.status_code == 200
    finally:
        importlib.reload(sec)


def test_admin_503_when_no_key_configured(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    import common.security as sec
    importlib.reload(sec)

    app = FastAPI()

    @app.delete("/danger", dependencies=[Depends(sec.require_admin)])
    def danger():
        return {"status": "executed"}

    @app.middleware("http")
    async def _force_host(request: Request, call_next):
        request.scope["client"] = ("203.0.113.7", 1)
        return await call_next(request)

    client = TestClient(app)
    try:
        r = client.delete("/danger")
        assert r.status_code == 503
    finally:
        importlib.reload(sec)
