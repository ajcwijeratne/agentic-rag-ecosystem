"""
Notification Engine — Apprise
===============================
Routes structured JSON payloads to Telegram, email, and desktop
notifications via a single Apprise client.

Configure channels in .env:
  APPRISE_TELEGRAM_TOKEN    — bot token
  APPRISE_TELEGRAM_CHAT_ID  — your chat/group ID
  APPRISE_EMAIL_HOST        — SMTP host
  APPRISE_EMAIL_PORT        — SMTP port
  APPRISE_EMAIL_USER        — sender address
  APPRISE_EMAIL_PASS        — SMTP password
  APPRISE_EMAIL_TO          — recipient address(es), comma-separated
  APPRISE_DESKTOP           — true/false, enable desktop notifications

REST endpoint:
  POST /notify              — accepts {title, body, tags[]}

Usage:
  python -m notifications.notifier --serve
  python -m notifications.notifier --test
"""

from __future__ import annotations

import os
from typing import Any

import apprise
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

PORT: int = int(os.getenv("NOTIFIER_PORT", "8004"))


# ---------------------------------------------------------------------------
# Apprise client builder
# ---------------------------------------------------------------------------

def _build_apprise() -> apprise.Apprise:
    ap = apprise.Apprise()

    # Telegram
    tg_token   = os.getenv("APPRISE_TELEGRAM_TOKEN", "")
    tg_chat_id = os.getenv("APPRISE_TELEGRAM_CHAT_ID", "")
    if tg_token and tg_chat_id:
        ap.add(f"tgram://{tg_token}/{tg_chat_id}/")

    # Email (SMTP)
    email_host = os.getenv("APPRISE_EMAIL_HOST", "")
    email_port = os.getenv("APPRISE_EMAIL_PORT", "587")
    email_user = os.getenv("APPRISE_EMAIL_USER", "")
    email_pass = os.getenv("APPRISE_EMAIL_PASS", "")
    email_to   = os.getenv("APPRISE_EMAIL_TO", "")
    if email_host and email_user and email_pass and email_to:
        for recipient in email_to.split(","):
            recipient = recipient.strip()
            ap.add(
                f"mailtos://{email_user}:{email_pass}@{email_host}:{email_port}"
                f"?to={recipient}"
            )

    # Desktop (OS notification)
    if os.getenv("APPRISE_DESKTOP", "false").lower() == "true":
        ap.add("pover://")  # Placeholder — replace with dbus:// (Linux) or gntp:// (macOS Growl)

    # Custom Apprise URLs from env (advanced)
    extra_urls = os.getenv("APPRISE_EXTRA_URLS", "")
    for url in extra_urls.split(","):
        url = url.strip()
        if url:
            ap.add(url)

    return ap


# ---------------------------------------------------------------------------
# Notification functions
# ---------------------------------------------------------------------------

async def notify(
    title: str,
    body: str,
    tags: list[str] | None = None,
    notify_type: str = apprise.NotifyType.INFO,
) -> dict[str, Any]:
    """
    Send a notification to all configured channels.
    Returns a status dict.
    """
    ap = _build_apprise()

    if len(ap) == 0:
        return {
            "status":  "warning",
            "message": "No notification channels configured. Set APPRISE_* env vars.",
            "sent":    0,
        }

    success = await ap.async_notify(
        title=title,
        body=body,
        notify_type=notify_type,
    )

    return {
        "status":   "ok" if success else "error",
        "channels": len(ap),
        "title":    title,
        "tags":     tags or [],
    }


def notify_sync(title: str, body: str, tags: list[str] | None = None) -> dict[str, Any]:
    """Synchronous wrapper for use outside async contexts."""
    import asyncio
    return asyncio.run(notify(title, body, tags))


# ---------------------------------------------------------------------------
# FastAPI service
# ---------------------------------------------------------------------------

from fastapi import Depends
from fastapi.middleware.cors import CORSMiddleware
from common.security import require_api_key, cors_kwargs, bind_host
app = FastAPI(title="Apprise Notification Engine", dependencies=[Depends(require_api_key)])
app.add_middleware(CORSMiddleware, **cors_kwargs())


class NotifyRequest(BaseModel):
    title: str
    body:  str
    tags:  list[str] = []
    notify_type: str = apprise.NotifyType.INFO


@app.post("/notify")
async def notify_endpoint(req: NotifyRequest):
    result = await notify(req.title, req.body, req.tags, req.notify_type)
    return result


@app.get("/health")
def health():
    ap = _build_apprise()
    return {
        "status":   "ok",
        "service":  "notifier",
        "channels": len(ap),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Apprise Notification Engine")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--test",  action="store_true", help="Send a test notification")
    args = parser.parse_args()

    if args.serve:
        uvicorn.run("notifications.notifier:app", host=bind_host(), port=PORT, reload=False)
    elif args.test:
        result = notify_sync(
            title="Agentic RAG — Test Notification",
            body="If you see this, your Apprise channels are configured correctly.",
            tags=["test"],
        )
        print(result)
