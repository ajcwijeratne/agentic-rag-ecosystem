"""
Web push: notifications from the Command Centre to Aaron's phone.

Decided 26 Nov 2026: push for three kinds only. Telegram alerts carry on.
  approval  something is waiting for his yes or no
  failure   a sync, reindex or agent task failed
  due       commitments due today (one morning notice)

How it works:
  * The phone's Chrome subscribes through the service worker (/app/sw.js)
    with this server's VAPID public key. The subscription is stored in the
    records database (table push_subs).
  * send() posts an encrypted message to each subscription with pywebpush.
    A subscription the push service reports gone (404/410) is removed.
  * VAPID keys are made on first use and kept in data/push_vapid.json, which
    the nightly backup covers.

pywebpush is imported lazily, so the orchestrator still starts without it;
status() reports whether it is installed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import records

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
VAPID_PATH = Path(os.getenv("PUSH_VAPID_PATH", str(_ROOT / "data" / "push_vapid.json")))
# VAPID needs a contact for the push service operator. No personal address is
# kept in code; set PUSH_CONTACT in .env to use another.
CONTACT = os.getenv("PUSH_CONTACT", "mailto:alerts@wijerco.invalid")
KINDS = ("approval", "failure", "due")


def library_installed() -> bool:
    try:
        import pywebpush  # noqa: F401
        return True
    except Exception:
        return False


def _ensure() -> None:
    with records._db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS push_subs (
                endpoint   TEXT PRIMARY KEY,
                keys       TEXT NOT NULL,
                label      TEXT,
                created_at TEXT NOT NULL,
                last_ok    TEXT,
                last_error TEXT
            )
        """)


def vapid_keys() -> dict[str, str]:
    """{"public": base64url raw P-256 point, "private": base64url PKCS8 DER}; made once.
    Both are the forms pywebpush and the browser's applicationServerKey take."""
    if VAPID_PATH.exists():
        return json.loads(VAPID_PATH.read_text(encoding="utf-8"))
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    b64 = lambda b: base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")  # noqa: E731
    keys = {"public": b64(raw), "private": b64(der)}
    VAPID_PATH.parent.mkdir(parents=True, exist_ok=True)
    VAPID_PATH.write_text(json.dumps(keys), encoding="utf-8")
    return keys


def subscribe(sub: dict[str, Any], label: str = "") -> dict[str, Any]:
    endpoint = str(sub.get("endpoint") or "")
    keys = sub.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("not a push subscription (needs an https endpoint and p256dh/auth keys)")
    _ensure()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with records._db() as conn:
        conn.execute("""INSERT INTO push_subs (endpoint, keys, label, created_at) VALUES (?,?,?,?)
                        ON CONFLICT(endpoint) DO UPDATE SET keys=excluded.keys, label=excluded.label""",
                     (endpoint, json.dumps({"p256dh": keys["p256dh"], "auth": keys["auth"]}), label[:80], now))
    return {"subscribed": True, "devices": len(list_subs())}


def unsubscribe(endpoint: str) -> bool:
    _ensure()
    with records._db() as conn:
        return conn.execute("DELETE FROM push_subs WHERE endpoint=?", (endpoint,)).rowcount > 0


def list_subs() -> list[dict[str, Any]]:
    _ensure()
    with records._db() as conn:
        rows = conn.execute("SELECT endpoint, keys, label, created_at, last_ok, last_error FROM push_subs").fetchall()
    return [{**dict(r), "keys": json.loads(r["keys"])} for r in rows]


def status() -> dict[str, Any]:
    subs = list_subs()
    return {"library": library_installed(), "kinds": list(KINDS), "devices": len(subs),
            "public_key": vapid_keys()["public"] if library_installed() else None,
            "last_ok": max((s["last_ok"] or "" for s in subs), default="") or None}


def _mark(endpoint: str, *, ok: bool, error: str | None = None) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with records._db() as conn:
        if ok:
            conn.execute("UPDATE push_subs SET last_ok=?, last_error=NULL WHERE endpoint=?", (now, endpoint))
        else:
            conn.execute("UPDATE push_subs SET last_error=? WHERE endpoint=?", (f"{now} {error}"[:300], endpoint))


def send(kind: str, title: str, body: str, *, url: str = "", tag: str | None = None, sender: Any = None) -> dict[str, int]:
    """Push one notification to every subscribed device. Never raises.

    sender(subscription_info, data, vapid_private_key, vapid_claims) defaults
    to pywebpush.webpush; tests pass a fake."""
    out = {"sent": 0, "failed": 0, "removed": 0}
    if kind not in KINDS:
        return out
    subs = list_subs()
    if not subs:
        return out
    if sender is None:
        if not library_installed():
            logger.warning("push skipped: pywebpush is not installed")
            return out
        from pywebpush import webpush as sender
    data = json.dumps({"title": title[:120], "body": body[:400], "url": url or "/app/command_centre.html",
                       "tag": tag or kind, "kind": kind})
    private_key = vapid_keys()["private"]
    for s in subs:
        info = {"endpoint": s["endpoint"], "keys": s["keys"]}
        try:
            sender(subscription_info=info, data=data, vapid_private_key=private_key,
                   vapid_claims={"sub": CONTACT}, ttl=6 * 3600)
            _mark(s["endpoint"], ok=True)
            out["sent"] += 1
        except Exception as exc:  # noqa: BLE001 - one device failing must not stop the others
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code in (404, 410):
                unsubscribe(s["endpoint"])
                out["removed"] += 1
            else:
                _mark(s["endpoint"], ok=False, error=str(exc)[:200])
                out["failed"] += 1
    return out


def app_url(view: str) -> str:
    """Link a notification opens: the Command Centre on the right view."""
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/app/command_centre.html?view={view}"
