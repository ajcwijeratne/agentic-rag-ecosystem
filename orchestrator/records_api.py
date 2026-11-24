"""
HTTP surface for the records store, the Google link and Google actions.

  GET  /google/status            connection state (operator)
  POST /google/sync              run one Calendar + Gmail sync now (admin)
  GET  /records                  ?kind=&status=&limit= (operator)
  GET  /records/today            the Today view's data (operator)
  GET  /records/counts           (operator)
  POST /records/{id}/status      {"status": "done"|"dismissed"|"open"} (admin)
  GET  /google/actions           ?status= (operator)
  POST /google/actions/draft     propose a Gmail draft (admin); approval creates it
  POST /google/actions/hold      propose a tentative hold (admin); approval creates it
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from common.rbac import require_role
from common.security import require_admin

from . import google_actions, google_link, records

router = APIRouter()
_operator = [Depends(require_role("operator"))]


@router.get("/google/status", dependencies=_operator)
async def google_status() -> dict[str, Any]:
    out = await asyncio.to_thread(google_link.status)
    out["last_run"] = records.get_kv("last_run")
    return out


@router.post("/google/sync", dependencies=[Depends(require_admin)])
async def google_sync_now() -> dict[str, Any]:
    from . import google_sync
    if not google_link.TOKEN_PATH.exists():
        raise HTTPException(status_code=409, detail="Google is not connected on this machine")
    return await google_sync.run_once(drive=True)


@router.get("/records", dependencies=_operator)
async def list_records(kind: str | None = None, status: str | None = None, limit: int = 100) -> dict[str, Any]:
    if kind and kind not in records.KINDS:
        raise HTTPException(status_code=422, detail=f"kind must be one of {', '.join(records.KINDS)}")
    return {"items": records.list_records(kind=kind, status=status, limit=limit)}


@router.get("/records/today", dependencies=_operator)
async def records_today() -> dict[str, Any]:
    return records.today()


@router.get("/records/counts", dependencies=_operator)
async def records_counts() -> dict[str, Any]:
    return records.counts()


class StatusIn(BaseModel):
    status: str


@router.post("/records/{record_id}/status", dependencies=[Depends(require_admin)])
async def record_status(record_id: str, req: StatusIn) -> dict[str, Any]:
    try:
        ok = records.set_status(record_id, req.status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if not ok:
        raise HTTPException(status_code=404, detail="record not found")
    return {"record_id": record_id, "status": req.status}


@router.get("/google/actions", dependencies=_operator)
async def actions(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    return {"items": google_actions.list_actions(status=status, limit=limit)}


class DraftIn(BaseModel):
    to: str
    subject: str
    body: str
    cc: str | None = None
    reply_to_item: str | None = None
    reason: str = ""


@router.post("/google/actions/draft", dependencies=[Depends(require_admin)])
async def propose_draft(req: DraftIn) -> dict[str, Any]:
    try:
        return google_actions.propose_draft(to=req.to, subject=req.subject, body=req.body, cc=req.cc,
                                            reply_to_item=req.reply_to_item, proposed_by="command-centre",
                                            reason=req.reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


class HoldIn(BaseModel):
    title: str
    start: str
    end: str
    note: str = ""
    reason: str = ""


@router.post("/google/actions/hold", dependencies=[Depends(require_admin)])
async def propose_hold(req: HoldIn) -> dict[str, Any]:
    try:
        return google_actions.propose_hold(title=req.title, start=req.start, end=req.end, note=req.note,
                                           proposed_by="command-centre", reason=req.reason)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
