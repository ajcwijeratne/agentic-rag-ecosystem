"""Governance gates for production moves and paid adapter calls."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from media import registry

try:
    from common.security import audit_log
except Exception:
    def audit_log(event: str, detail: dict | None = None) -> None:
        return None

GATES = (
    "public_claim",
    "generated_image",
    "client_sensitive",
    "paid_job",
    "external_publish",
    "clone_output",
)

_DB_PATH = Path(os.getenv("MEDIA_DB_PATH", "data/media.db"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gate_approvals (
            id        TEXT PRIMARY KEY,
            gate      TEXT NOT NULL,
            target_id TEXT NOT NULL,
            status    TEXT NOT NULL,
            actor     TEXT,
            at        TEXT NOT NULL,
            note      TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS ix_gate_target ON gate_approvals(gate,target_id,status)")
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _approval(gate: str, target_id: str) -> dict | None:
    with _db() as conn:
        row = conn.execute(
            "SELECT * FROM gate_approvals WHERE gate=? AND target_id=? "
            "ORDER BY at DESC LIMIT 1",
            (gate, target_id),
        ).fetchone()
    return dict(row) if row else None


def check(gate: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return whether a named gate is open for the target in context."""
    if gate not in GATES:
        raise ValueError(f"unknown gate: {gate}")
    context = context or {}
    target_id = context.get("target_id") or context.get("production_id") or context.get("job_id")
    if not target_id:
        return {"ok": False, "reason": "missing gate target"}
    approval = _approval(gate, str(target_id))
    if approval and approval.get("status") == "approved":
        return {"ok": True, "reason": "approved", "approval": approval}
    if approval and approval.get("status") == "rejected":
        return {"ok": False, "reason": "rejected", "approval": approval}
    return {"ok": False, "reason": f"{gate} approval required"}


def approve(gate: str, target_id: str, actor: str = "operator", note: str = "", status: str = "approved") -> dict:
    if gate not in GATES:
        raise ValueError(f"unknown gate: {gate}")
    if status not in ("approved", "rejected"):
        raise ValueError("status must be approved or rejected")
    row = {
        "id": str(uuid.uuid4()),
        "gate": gate,
        "target_id": target_id,
        "status": status,
        "actor": actor,
        "at": _now(),
        "note": note,
    }
    with _db() as conn:
        conn.execute(
            "INSERT INTO gate_approvals (id,gate,target_id,status,actor,at,note) "
            "VALUES (:id,:gate,:target_id,:status,:actor,:at,:note)",
            row,
        )
    if gate == "generated_image" and status == "approved":
        _mark_generated_assets_reviewed(target_id, actor, note)
    audit_log("governance.approve", row)
    return row


def _has_client_confidential_asset(production: dict[str, Any]) -> bool:
    for asset_id in production.get("linked_assets") or []:
        asset = registry.get_asset(asset_id, with_relations=False)
        if asset and asset.get("rights") == "client_confidential":
            return True
    return False


def _clone_assets(production: dict[str, Any]) -> list[dict[str, Any]]:
    """Assets produced with Aaron's cloned voice or avatar."""
    items = []
    for asset_id in production.get("linked_assets") or []:
        asset = registry.get_asset(asset_id, with_relations=False)
        if not asset:
            continue
        meta = asset.get("meta") or {}
        if bool(meta.get("clone") or (meta.get("governance") or {}).get("clone")):
            items.append({
                "asset_id": asset_id,
                "type": asset.get("type"),
                "provider": meta.get("provider"),
                "path": asset.get("path"),
            })
    return items


def _generated_assets_requiring_review(production: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for asset_id in production.get("linked_assets") or []:
        asset = registry.get_asset(asset_id, with_relations=False)
        if not asset:
            continue
        meta = asset.get("meta") or {}
        tags = asset.get("tags") or []
        governance = meta.get("governance") or {}
        capability = governance.get("generation_capability") or meta.get("generation_capability")
        is_generated = bool(governance.get("generated") or "generated" in tags)
        needs_review = governance.get("review_status") != "approved"
        reviewable = asset.get("type") in {"image", "video"} or capability in {"image", "avatar", "animation", "video"}
        if is_generated and needs_review and reviewable:
            items.append({
                "asset_id": asset_id,
                "type": asset.get("type"),
                "capability": capability,
                "path": asset.get("path"),
            })
    return items


def _mark_generated_assets_reviewed(production_id: str, actor: str, note: str) -> None:
    try:
        from orchestrator import production

        prod = production.get_production(production_id)
    except Exception:
        prod = None
    if not prod:
        return
    for item in _generated_assets_requiring_review(prod):
        asset = registry.get_asset(item["asset_id"], with_relations=False)
        if not asset:
            continue
        meta = asset.get("meta") or {}
        governance = meta.get("governance") or {}
        governance.update({
            "review_status": "approved",
            "reviewed_by": actor,
            "review_note": note,
            "reviewed_at": _now(),
        })
        meta["governance"] = governance
        registry.update_asset(item["asset_id"], meta=meta)


def required_for_transition(production: dict[str, Any], to_state: str) -> list[str]:
    required: list[str] = []
    if production.get("state") == "review" and to_state == "publish":
        required.extend(["public_claim", "external_publish"])
        if _has_client_confidential_asset(production):
            required.append("client_sensitive")
        if _generated_assets_requiring_review(production):
            required.append("generated_image")
    if to_state == "render":
        plan = production.get("asset_plan") or {}
        raw = json.dumps(plan).lower()
        if "generated_image" in raw or "needs_generation" in raw:
            required.append("generated_image")
    # Clone rule: any output made with Aaron's cloned voice or avatar stops at
    # a gate before moving past render. Applies to review, publish, and
    # measure, so clone output never leaves the system unapproved.
    if to_state in ("review", "publish", "measure") and _clone_assets(production):
        required.append("clone_output")
    return required


def pending_gates(production: dict[str, Any], to_state: str | None = None) -> list[dict[str, Any]]:
    target_id = production.get("production_id")
    if not target_id:
        return []
    if to_state is None:
        states = ("idea", "brief", "research", "outline", "draft", "asset_plan",
                  "render", "review", "publish", "measure")
        state = production.get("state")
        try:
            idx = states.index(state)
            to_state = states[idx + 1] if idx + 1 < len(states) else state
        except ValueError:
            to_state = state
    pending = []
    for gate in required_for_transition(production, to_state or ""):
        result = check(gate, {"target_id": target_id})
        if not result["ok"]:
            item: dict[str, Any] = {"gate": gate, "target_id": target_id, "reason": result["reason"]}
            if gate == "generated_image":
                item["assets"] = _generated_assets_requiring_review(production)
            if gate == "clone_output":
                item["assets"] = _clone_assets(production)
            pending.append(item)
    return pending


def list_approvals(target_id: str | None = None, limit: int = 200) -> list[dict]:
    params: list[Any] = []
    where = ""
    if target_id:
        where = "WHERE target_id=?"
        params.append(target_id)
    params.append(limit)
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM gate_approvals {where} ORDER BY at DESC LIMIT ?",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def pending() -> dict[str, list]:
    from orchestrator import production

    items = []
    for prod in production.list_productions(limit=500):
        for gate in pending_gates(prod):
            items.append({
                "production_id": prod["production_id"],
                "title": prod["title"],
                "state": prod["state"],
                **gate,
            })
    return {"items": items}
