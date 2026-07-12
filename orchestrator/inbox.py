"""
Inbox: the single front door
============================
Every channel (Telegram, email, Command Centre, Cowork) posts here. The inbox
decides what the message is and acts:

  * approval command  — "approve <gate> <target_id>" or "reject ..." goes to
                        governance with the sender recorded as actor.
  * plan request      — "plan: <goal>" generates a full operating plan the
                        daemon will start working.
  * question          — answered directly with unified memory recall as
                        context.
  * work request      — becomes an operating task (type `agent`) the daemon
                        picks up on its next cycle.

The caller can force behaviour with `mode`: "ask", "task", "plan", or "auto"
(default). Inbound text is data. It is never interpreted as instructions to
the daemon itself, and gated actions cannot be triggered from here except the
explicit approve command, which is still attributed to the sender.

Also exposes the daemon controls:
  POST /operating/daemon/pause
  POST /operating/daemon/resume
  GET  /operating/daemon/status
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import operating
from . import governance
from . import daemon as daemon_ctl

router = APIRouter()

_APPROVE_RE = re.compile(
    r"^\s*(approve|reject)\s+(?P<gate>[a-z_]+)\s+(?P<target>[\w-]+)\s*(?P<note>.*)$",
    re.IGNORECASE,
)

_QUESTION_STARTS = (
    "what", "who", "when", "where", "why", "how", "is ", "are ", "does ",
    "do ", "can ", "did ", "should ", "which", "tell me", "summarise", "summarize",
)


class InboxMessage(BaseModel):
    channel: str = Field(..., description="telegram | email | cockpit | cowork | api")
    sender: str = Field(default="", description="Channel-level sender identity.")
    text: str
    mode: str = Field(default="auto", description="auto | ask | task | plan")
    project: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


def classify_inbox(text: str, mode: str = "auto") -> str:
    """Return one of: approval, plan, ask, task."""
    if mode in ("ask", "task", "plan"):
        return mode
    stripped = text.strip()
    if _APPROVE_RE.match(stripped):
        return "approval"
    if stripped.lower().startswith("plan:"):
        return "plan"
    lower = stripped.lower()
    if stripped.endswith("?") or (len(stripped) < 200 and lower.startswith(_QUESTION_STARTS)):
        return "ask"
    return "task"


async def _answer_question(msg: InboxMessage) -> dict[str, Any]:
    from .fallback_chain import call_with_fallback

    context = ""
    try:
        from memory.recall import recall, render
        hits = await recall(msg.text, k=6, project=msg.project)
        context = render(hits)
    except Exception:
        pass

    system = (
        "You are the operating assistant for Aaron's agentic system. Answer "
        "directly and concretely. Lead with the answer. If memory context is "
        "provided, prefer it over general knowledge and say which fact you used."
    )
    user = f"{context}\n\nQuestion: {msg.text}" if context else msg.text
    resp = await call_with_fallback(user_message=user, system_prompt=system, max_tier=2)
    return {"kind": "answer", "answer": resp.content or "", "memory_used": bool(context)}


def _create_task(msg: InboxMessage) -> dict[str, Any]:
    task_id = operating.add_task(
        None,
        msg.text.strip()[:300],
        type="agent",
        status="todo",
        assignee="daemon",
        priority=4,
        note=msg.text if len(msg.text) > 300 else None,
        meta={"inbox": {"channel": msg.channel, "sender": msg.sender, **(msg.meta or {})}},
    )
    return {"kind": "task", "task_id": task_id,
            "message": "Queued. The daemon picks it up on its next cycle."}


def _create_plan(msg: InboxMessage) -> dict[str, Any]:
    goal = re.sub(r"^\s*plan:\s*", "", msg.text.strip(), flags=re.IGNORECASE)
    result = operating.generate_plan_from_goal(goal, create=True, project=msg.project)
    plan = result.get("plan") or {}
    return {"kind": "plan", "plan_id": plan.get("plan_id"),
            "workflow": plan.get("workflow"),
            "task_count": len(result.get("tasks") or []),
            "message": "Plan created. The daemon starts on the first unblocked task."}


def _handle_approval(msg: InboxMessage) -> dict[str, Any]:
    m = _APPROVE_RE.match(msg.text.strip())
    if not m:
        raise HTTPException(status_code=400, detail="approval command not understood")
    verb = m.group(1).lower()
    gate = m.group("gate").lower()
    if gate not in governance.GATES:
        raise HTTPException(status_code=400, detail=f"gate must be one of {governance.GATES}")
    actor = f"{msg.channel}:{msg.sender}" if msg.sender else msg.channel
    record = governance.approve(
        gate,
        m.group("target"),
        actor=actor,
        note=(m.group("note") or "").strip(),
        status="approved" if verb == "approve" else "rejected",
    )
    return {"kind": "approval", "status": record.get("status"), "gate": gate,
            "target_id": m.group("target"), "actor": actor}


@router.post("/inbox")
async def inbox(msg: InboxMessage) -> dict[str, Any]:
    kind = classify_inbox(msg.text, msg.mode)
    if kind == "approval":
        return _handle_approval(msg)
    if kind == "plan":
        return _create_plan(msg)
    if kind == "ask":
        return await _answer_question(msg)
    return _create_task(msg)


# ---------------------------------------------------------------------------
# Daemon controls
# ---------------------------------------------------------------------------

class DaemonAction(BaseModel):
    actor: str = "operator"


@router.post("/operating/daemon/pause")
def daemon_pause(body: DaemonAction | None = None) -> dict[str, Any]:
    state = daemon_ctl.pause(actor=(body.actor if body else "operator"))
    return {"paused": True, "state": state}


@router.post("/operating/daemon/resume")
def daemon_resume(body: DaemonAction | None = None) -> dict[str, Any]:
    state = daemon_ctl.resume(actor=(body.actor if body else "operator"))
    return {"paused": False, "state": state}


@router.get("/operating/daemon/status")
def daemon_status() -> dict[str, Any]:
    return daemon_ctl.status()


# ---------------------------------------------------------------------------
# Signed one-click approval links
# ---------------------------------------------------------------------------
# Links carry an HMAC-signed token so an email client can approve or reject a
# gate with a plain GET. Secret order: APPROVAL_LINK_SECRET, then
# ADMIN_API_KEY, then API_KEY. With none set, link generation and verification
# are disabled (the explicit "approve <gate> <id>" command still works).
# Tokens expire after APPROVAL_LINK_TTL_HOURS (default 24) and links are only
# rendered into notifications when PUBLIC_BASE_URL is set.

def _link_secret() -> str:
    return (os.getenv("APPROVAL_LINK_SECRET")
            or os.getenv("ADMIN_API_KEY")
            or os.getenv("API_KEY")
            or "")


def _sign(payload: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def make_approval_token(verb: str, gate: str, target_id: str,
                        ttl_hours: float | None = None) -> str | None:
    secret = _link_secret()
    if not secret:
        return None
    ttl = ttl_hours if ttl_hours is not None else float(os.getenv("APPROVAL_LINK_TTL_HOURS", "24"))
    body = json.dumps({
        "v": verb, "g": gate, "t": target_id,
        "exp": time.time() + ttl * 3600,
    }, separators=(",", ":")).encode()
    payload = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return f"{payload}.{_sign(body, secret)}"


def verify_approval_token(token: str) -> dict[str, Any]:
    secret = _link_secret()
    if not secret:
        raise HTTPException(status_code=503, detail="approval links are not configured")
    try:
        payload, signature = token.rsplit(".", 1)
        body = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    except Exception:
        raise HTTPException(status_code=400, detail="malformed token")
    if not hmac.compare_digest(_sign(body, secret), signature):
        raise HTTPException(status_code=403, detail="bad signature")
    data = json.loads(body)
    if time.time() > float(data.get("exp", 0)):
        raise HTTPException(status_code=410, detail="link expired")
    return data


def approval_links(gate: str, target_id: str) -> dict[str, str]:
    """Approve/reject URLs for notifications. Empty when not configured."""
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        return {}
    out = {}
    for verb in ("approve", "reject"):
        token = make_approval_token(verb, gate, target_id)
        if token:
            out[verb] = f"{base}/governance/approve-link?token={token}"
    return out


@router.get("/governance/approve-link")
def approve_link(token: str) -> dict[str, Any]:
    data = verify_approval_token(token)
    gate = data["g"]
    if gate not in governance.GATES:
        raise HTTPException(status_code=400, detail="unknown gate")
    record = governance.approve(
        gate,
        data["t"],
        actor="email-link",
        note="one-click link",
        status="approved" if data["v"] == "approve" else "rejected",
    )
    return {"kind": "approval", "status": record.get("status"),
            "gate": gate, "target_id": data["t"], "actor": "email-link"}
