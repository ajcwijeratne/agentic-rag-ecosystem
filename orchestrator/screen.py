"""
Screen awareness endpoints
==========================
  GET  /screen/status     what the capability can do, and whether it stays local
  GET  /screen/window     the foreground window title and app — text only, no pixels
  POST /screen/describe   capture the screen and answer a question about it

Read-only throughout: this can look, never act. There is no endpoint here that
clicks, types, or launches anything.

`/screen/describe` needs the operator role. It is the most sensitive route in
the service — it photographs the desktop and, without a local vision model,
sends that image to a third party — so it sits behind the same gate as the other
actions that spend money or leave the machine.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from common.rbac import require_role

router = APIRouter(prefix="/screen", tags=["screen"])


class DescribeRequest(BaseModel):
    question: str = Field(default="Describe what is on this screen.", max_length=2000)
    region:   str = Field(default="screen", description="screen | window")
    provider: str | None = None
    spoken:   bool = False


@router.get("/status")
async def screen_status() -> dict:
    """Capability report. The UI uses this to show whether images stay local."""
    from media import screen, vision

    vis = vision.status()
    return {
        "capture_enabled": screen.SCREEN_ENABLED,
        "blocklist":       screen.SCREEN_BLOCKLIST,
        "max_edge":        screen.SCREEN_MAX_EDGE,
        "vision":          vis,
        "ready":           screen.SCREEN_ENABLED and vis["available"],
        "privacy": (
            "Screenshots are described locally and never leave this machine."
            if vis.get("stays_local")
            else "No local vision model: screenshots are sent to "
                 f"{vis.get('cloud_provider') or 'a cloud provider'} to be described."
        ),
    }


@router.get("/window")
async def screen_window() -> dict:
    """
    What is in the foreground, by title and process only.

    Cheap, captures no pixels, and often enough on its own to know what someone
    is working on — so it is deliberately not behind the capture gate.
    """
    from media import screen

    info = screen.active_window()
    return {**info, "blocked": bool(screen.is_blocked(info.get("title", "")))}


@router.post("/describe", dependencies=[Depends(require_role("operator"))])
async def screen_describe(req: DescribeRequest) -> dict:
    """Capture the screen and answer a question about it."""
    from media import screen, vision

    try:
        shot = screen.capture(region=req.region, reason=req.question[:200])
    except screen.ScreenCaptureError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    result = await vision.describe(
        shot.image_bytes, req.question, provider=req.provider, spoken=req.spoken
    )
    if result.error:
        raise HTTPException(status_code=502, detail=result.error)

    return {
        "answer":  result.text,
        "capture": shot.meta(),
        "vision":  result.to_dict(),
    }


# ---------------------------------------------------------------------------
# Voice integration
# ---------------------------------------------------------------------------

async def answer_about_screen(question: str, spoken: bool = True) -> dict | None:
    """
    Answer a spoken question about the screen, or return None to fall through
    to normal retrieval.

    Returning None rather than raising matters: if screen awareness is off or
    unavailable, "what's on my screen?" should still get a spoken explanation of
    why, not a stack trace or silence.
    """
    from media import screen, vision

    if not screen.wants_screen(question):
        return None

    try:
        shot = screen.capture(region=screen.region_for(question), reason=question[:200])
    except screen.ScreenCaptureError as exc:
        return {"answer": str(exc), "route": "screen", "model": "", "cost_usd": 0.0,
                "screen": {"captured": False}}

    result = await vision.describe(shot.image_bytes, question, spoken=spoken)
    if result.error:
        return {"answer": f"I could not read the screen. {result.error}",
                "route": "screen", "model": result.model, "cost_usd": 0.0,
                "screen": {"captured": True, **shot.meta()}}

    return {
        "answer":   result.text,
        "route":    "screen",
        "model":    result.model,
        "cost_usd": 0.0,
        "screen":   {"captured": True, "local": result.local, **shot.meta()},
    }
