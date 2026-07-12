"""
Email Channel
=============
IMAP poller that turns email from Aaron into inbox messages and replies with
the result. The slow channel: five-minute poll, plain text, no buttons.

Security: only senders in EMAIL_ALLOWED_SENDERS are processed. Everything
else is left unread and logged. Approval by email works with the same
explicit command the inbox understands: a message whose body (or subject)
starts with "approve <gate> <target_id>" or "reject ...".

Env:
  EMAIL_IMAP_HOST           e.g. imap.gmail.com
  EMAIL_IMAP_PORT           default 993
  EMAIL_USER                mailbox to watch (also the From for replies)
  EMAIL_PASS                app password
  EMAIL_ALLOWED_SENDERS     comma-separated addresses allowed to task the system
  EMAIL_FOLDER              default INBOX
  EMAIL_POLL_SEC            default 300
  EMAIL_SMTP_HOST           default smtp.gmail.com
  EMAIL_SMTP_PORT           default 465 (SSL)
  ORCHESTRATOR_URL          default http://localhost:8000
  ORCH_API_KEY              sent as X-API-Key when set

Run: python -m channels.email_poller
"""

from __future__ import annotations

import email
import email.header
import imaplib
import logging
import os
import smtplib
import time
from email.mime.text import MIMEText

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("email_poller")

IMAP_HOST = os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("EMAIL_IMAP_PORT", "993"))
SMTP_HOST = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("EMAIL_SMTP_PORT", "465"))
USER = os.getenv("EMAIL_USER", os.getenv("APPRISE_EMAIL_USER", ""))
PASSWORD = os.getenv("EMAIL_PASS", os.getenv("APPRISE_EMAIL_PASS", ""))
FOLDER = os.getenv("EMAIL_FOLDER", "INBOX")
POLL_SEC = int(os.getenv("EMAIL_POLL_SEC", "300"))
ALLOWED = [a.strip().lower() for a in os.getenv("EMAIL_ALLOWED_SENDERS", "").split(",") if a.strip()]
ORCH_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.getenv("ORCH_API_KEY", os.getenv("API_KEY", ""))


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _body_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        return ""
    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload() or "")


def _sender_address(msg: email.message.Message) -> str:
    raw = _decode(msg.get("From", ""))
    if "<" in raw and ">" in raw:
        return raw.split("<", 1)[1].split(">", 1)[0].strip().lower()
    return raw.strip().lower()


def _post_inbox(text: str, sender: str) -> dict:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    r = httpx.post(f"{ORCH_URL}/inbox",
                   json={"channel": "email", "sender": sender, "text": text},
                   headers=headers, timeout=180)
    r.raise_for_status()
    return r.json()


def _reply(to_addr: str, subject: str, body: str) -> None:
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"Re: {subject}" if subject and not subject.lower().startswith("re:") else (subject or "Agent reply")
    msg["From"] = USER
    msg["To"] = to_addr
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(USER, PASSWORD)
            smtp.send_message(msg)
    except Exception:
        logger.exception("reply to %s failed", to_addr)


def _result_to_text(result: dict) -> str:
    kind = result.get("kind")
    if kind == "answer":
        return result.get("answer") or "(no answer)"
    if kind == "task":
        return f"Queued as task {result.get('task_id')}. The daemon picks it up within a minute."
    if kind == "plan":
        return (f"Plan {result.get('plan_id')} created "
                f"({result.get('workflow')}, {result.get('task_count')} tasks). "
                "The daemon starts on the first unblocked task.")
    if kind == "approval":
        return f"{result.get('status')}: {result.get('gate')} for {result.get('target_id')}"
    return str(result)[:1000]


def poll_once() -> int:
    """One IMAP pass. Returns the number of messages processed."""
    processed = 0
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        imap.login(USER, PASSWORD)
        imap.select(FOLDER)
        _, data = imap.search(None, "UNSEEN")
        ids = data[0].split() if data and data[0] else []
        for msg_id in ids:
            _, msg_data = imap.fetch(msg_id, "(RFC822)")
            raw = msg_data[0][1] if msg_data and msg_data[0] else None
            if not raw:
                continue
            msg = email.message_from_bytes(raw)
            sender = _sender_address(msg)
            if sender not in ALLOWED:
                logger.warning("unauthorised sender %s left unread", sender)
                imap.store(msg_id, "-FLAGS", "\\Seen")
                continue
            subject = _decode(msg.get("Subject", ""))
            body = _body_text(msg).strip()
            text = body or subject
            # An approval command in the subject wins over a long body.
            low_subject = subject.strip().lower()
            if low_subject.startswith(("approve ", "reject ")):
                text = subject.strip()
            if not text:
                continue
            logger.info("processing mail from %s: %s", sender, subject[:80])
            try:
                result = _post_inbox(text, sender)
                _reply(sender, subject, _result_to_text(result))
                processed += 1
            except Exception:
                logger.exception("inbox post failed for mail from %s", sender)
                imap.store(msg_id, "-FLAGS", "\\Seen")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return processed


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not USER or not PASSWORD:
        raise SystemExit("EMAIL_USER and EMAIL_PASS are required")
    if not ALLOWED:
        raise SystemExit("EMAIL_ALLOWED_SENDERS is required; refusing to run an open mailbox")
    logger.info("email poller starting; watching %s for %s", FOLDER, ALLOWED)
    while True:
        try:
            n = poll_once()
            if n:
                logger.info("processed %d message(s)", n)
        except Exception:
            logger.exception("poll failed")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    main()
