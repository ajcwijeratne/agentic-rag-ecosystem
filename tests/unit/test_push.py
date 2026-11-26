"""Phase 3: phone notifications for approvals, failures and commitments due."""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import datetime

import pytest


@pytest.fixture
def push(tmp_path, monkeypatch):
    import orchestrator.records as records
    monkeypatch.setattr(records, "DB_PATH", tmp_path / "records.db")
    import orchestrator.push as push
    monkeypatch.setattr(push, "VAPID_PATH", tmp_path / "vapid.json")
    return push


def _sub(i: int = 1) -> dict:
    return {"endpoint": f"https://push.example/send/{i}",
            "keys": {"p256dh": base64.urlsafe_b64encode(os.urandom(65)).decode().rstrip("="),
                     "auth": base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")}}


class _Gone(Exception):
    def __init__(self, code):
        super().__init__(f"HTTP {code}")
        self.response = type("R", (), {"status_code": code})()


def test_vapid_keys_are_made_once_in_browser_form(push):
    k1 = push.vapid_keys()
    k2 = push.vapid_keys()
    assert k1 == k2
    assert len(base64.urlsafe_b64decode(k1["public"] + "==")) == 65   # uncompressed P-256 point


def test_subscribe_validates_and_dedupes(push):
    with pytest.raises(ValueError):
        push.subscribe({"endpoint": "http://insecure", "keys": {}})
    push.subscribe(_sub(1), label="phone")
    push.subscribe(_sub(1), label="phone again")
    push.subscribe(_sub(2))
    assert len(push.list_subs()) == 2


def test_send_reaches_every_device_and_drops_gone_ones(push):
    push.subscribe(_sub(1))
    push.subscribe(_sub(2))
    push.subscribe(_sub(3))
    calls = []

    def sender(subscription_info, data, vapid_private_key, vapid_claims, ttl):
        calls.append((subscription_info["endpoint"], data))
        if subscription_info["endpoint"].endswith("/2"):
            raise _Gone(410)
        if subscription_info["endpoint"].endswith("/3"):
            raise _Gone(500)

    out = push.send("approval", "Waiting on you", "A draft", url="https://x.example/app?view=approvals", sender=sender)
    assert out == {"sent": 1, "failed": 1, "removed": 1}
    assert [s["endpoint"] for s in push.list_subs()] == ["https://push.example/send/1", "https://push.example/send/3"]
    assert '"url": "https://x.example/app?view=approvals"' in calls[0][1]


def test_only_the_chosen_kinds_push(push):
    push.subscribe(_sub(1))
    calls = []
    out = push.send("budget", "Budget warning", "80%", sender=lambda **kw: calls.append(kw))
    assert out["sent"] == 0 and calls == []


def test_real_pywebpush_signs_and_encrypts(push, tmp_path, monkeypatch):
    pytest.importorskip("pywebpush")
    monkeypatch.chdir(tmp_path)   # curl=True writes encrypted.data to the working folder
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from pywebpush import webpush
    rk = ec.generate_private_key(ec.SECP256R1())
    p256 = base64.urlsafe_b64encode(rk.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).decode().rstrip("=")
    sub = {"endpoint": "https://push.example/send/real", "keys": {"p256dh": p256, "auth": base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")}}
    cmd = webpush(subscription_info=sub, data="{}", vapid_private_key=push.vapid_keys()["private"],
                  vapid_claims={"sub": push.CONTACT}, curl=True, ttl=60)
    assert "vapid t=" in cmd.lower() and "aes128gcm" in cmd


def test_daemon_notify_pushes_only_with_a_kind(push, monkeypatch):
    import orchestrator.daemon as daemon
    sent = []
    monkeypatch.setattr(push, "send", lambda kind, title, body, url="": sent.append((kind, title, url)) or {})

    async def fake_notify(title, body):
        return {}

    import sys
    import types
    monkeypatch.setitem(sys.modules, "notifications.notifier", types.SimpleNamespace(notify=fake_notify))
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://wijerco.example")
    asyncio.run(daemon._notify("Budget warning", "80%"))
    asyncio.run(daemon._notify("Waiting on you", "a draft", kind="approval", view="approvals"))
    assert sent == [("approval", "Waiting on you", "https://wijerco.example/app/command_centre.html?view=approvals")]


def test_due_push_once_a_day_and_only_when_due(push, monkeypatch, tmp_path):
    import orchestrator.daemon as daemon
    import orchestrator.records as records
    monkeypatch.setattr(daemon, "PUSH_DUE_HOUR", 0)
    monkeypatch.setattr(daemon, "save_state", lambda s: None)
    notes = []

    async def fake(title, body, kind=None, view="overview"):
        notes.append((title, body, kind))

    monkeypatch.setattr(daemon, "_notify", fake)
    state: dict = {}
    asyncio.run(daemon._maybe_due_push(state))
    assert notes == [] and state["due_push_date"] == datetime.now().strftime("%Y-%m-%d")   # nothing due: silent
    records.upsert_record("commitment", "c1", title="Send the agenda", data_class="personal", owner="me",
                          due_at=datetime.now().strftime("%Y-%m-%d"))
    asyncio.run(daemon._maybe_due_push(state))
    assert notes == []                                    # already ran today
    state.clear()
    asyncio.run(daemon._maybe_due_push(state))
    assert notes == [("1 due today", "- Send the agenda", "due")]
