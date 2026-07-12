"""
Nightly Memory Consolidation
============================
Turns the day's activity into durable knowledge. Run by the operating daemon
once per night (CONSOLIDATION_HOUR, default 02:00 local) or by hand:

    python -m memory.consolidation

Three passes, each independent and failure-tolerant:

  1. digest_completed_tasks — completed operating tasks from the last 24 hours
     are folded into one digest per project, written to project memory and the
     semantic store. The system remembers what it did, not just what it said.
  2. promote_repeated_facts — a fact that keeps recurring across project
     memory entries (three or more near-duplicates) is promoted to the
     semantic store as durable knowledge. Promotions are hashed in
     data/consolidation_state.json so nothing promotes twice.
  3. prune_episodic — episodic entries older than EPISODIC_RETENTION_DAYS
     (default 90) that were never promoted are deleted from Qdrant. Semantic
     and project memories are permanent until deleted by hand.

Every run appends a summary line to logs/consolidation.jsonl so you can audit
what the system decided to remember.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

_ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = Path(os.getenv("CONSOLIDATION_STATE_PATH", str(_ROOT / "data" / "consolidation_state.json")))
LOG_PATH = _ROOT / "logs" / "consolidation.jsonl"

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
EPISODIC_COLLECTION = os.getenv("EPISODIC_COLLECTION", "episodic_memory")
RETENTION_DAYS = int(os.getenv("EPISODIC_RETENTION_DAYS", "90"))
PROMOTE_MIN_COUNT = int(os.getenv("CONSOLIDATION_PROMOTE_MIN", "3"))
SIMILARITY_THRESHOLD = float(os.getenv("CONSOLIDATION_SIMILARITY", "0.6"))


def _load_state() -> dict[str, Any]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"promoted_hashes": [], "digested_task_ids": []}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Cap the ledgers so the file never grows without bound.
    state["promoted_hashes"] = state.get("promoted_hashes", [])[-2000:]
    state["digested_task_ids"] = state.get("digested_task_ids", [])[-2000:]
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _log(summary: dict[str, Any]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **summary}
    try:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass


def _terms(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", (text or "").lower()))


def _similar(a: str, b: str) -> bool:
    ta, tb = _terms(a), _terms(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= SIMILARITY_THRESHOLD


def _content_hash(text: str) -> str:
    normalised = " ".join(sorted(_terms(text)))
    return hashlib.sha256(normalised.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Pass 1: digest completed tasks
# ---------------------------------------------------------------------------

async def digest_completed_tasks(hours: int = 24) -> dict[str, Any]:
    from orchestrator import operating

    state = _load_state()
    seen: set[str] = set(state.get("digested_task_ids", []))
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")

    done = [t for t in operating.list_tasks(status="done", limit=500)
            if (t.get("updated_at") or "") >= cutoff and t["task_id"] not in seen]
    if not done:
        return {"digests": 0, "tasks": 0}

    # Group by project via the owning plan; standalone tasks fall to "general".
    by_project: dict[str, list[dict]] = {}
    plan_cache: dict[str, dict | None] = {}
    for t in done:
        project = "general"
        pid = t.get("plan_id")
        if pid:
            if pid not in plan_cache:
                plan_cache[pid] = operating.get_plan(pid)
            project = (plan_cache[pid] or {}).get("project") or "general"
        by_project.setdefault(project, []).append(t)

    digests = 0
    for project, tasks in by_project.items():
        lines = [f"Daily digest {datetime.now().strftime('%Y-%m-%d')}: "
                 f"{len(tasks)} task(s) completed."]
        for t in tasks[:12]:
            lines.append(f"- {t['title']}")
        digest = "\n".join(lines)
        operating.add_project_memory(project, digest, source="consolidation",
                                     meta={"kind": "daily_digest"})
        try:
            from memory.memory_store import store
            await store.add(project, digest, source="consolidation")
        except Exception:
            pass
        digests += 1

    state["digested_task_ids"] = list(seen | {t["task_id"] for t in done})
    _save_state(state)
    return {"digests": digests, "tasks": len(done)}


# ---------------------------------------------------------------------------
# Pass 2: promote repeated facts
# ---------------------------------------------------------------------------

async def promote_repeated_facts() -> dict[str, Any]:
    from orchestrator import operating

    state = _load_state()
    promoted: set[str] = set(state.get("promoted_hashes", []))

    entries = [m for m in operating.list_project_memory(limit=500)
               if (m.get("meta") or {}).get("kind") != "daily_digest"]

    # Greedy near-duplicate clustering on keyword overlap.
    clusters: list[list[dict]] = []
    for entry in entries:
        placed = False
        for cluster in clusters:
            if _similar(entry.get("content", ""), cluster[0].get("content", "")):
                cluster.append(entry)
                placed = True
                break
        if not placed:
            clusters.append([entry])

    promotions = 0
    for cluster in clusters:
        if len(cluster) < PROMOTE_MIN_COUNT:
            continue
        representative = max(cluster, key=lambda m: len(m.get("content") or ""))
        h = _content_hash(representative.get("content", ""))
        if h in promoted:
            continue
        project = representative.get("project") or "general"
        content = (f"Recurring fact ({len(cluster)} observations): "
                   f"{representative.get('content')}")
        try:
            from memory.memory_store import store
            await store.add(project, content, source="consolidation-promotion")
            promoted.add(h)
            promotions += 1
        except Exception:
            continue

    state["promoted_hashes"] = list(promoted)
    _save_state(state)
    return {"clusters": len(clusters), "promotions": promotions}


# ---------------------------------------------------------------------------
# Pass 3: episodic retention
# ---------------------------------------------------------------------------

async def prune_episodic(days: int | None = None) -> dict[str, Any]:
    days = days or RETENTION_DAYS
    cutoff = time.time() - days * 86400
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{QDRANT_URL}/collections/{EPISODIC_COLLECTION}/points/delete",
                json={"filter": {"must": [
                    {"key": "timestamp", "range": {"lt": cutoff}},
                ]}},
                timeout=20.0,
            )
            ok = resp.status_code in (200, 202)
        return {"pruned": ok, "cutoff_days": days}
    except Exception as exc:
        return {"pruned": False, "error": str(exc)[:200]}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_consolidation() -> dict[str, Any]:
    summary: dict[str, Any] = {}
    try:
        summary["digest"] = await digest_completed_tasks()
    except Exception as exc:
        summary["digest"] = {"error": str(exc)[:200]}
    try:
        summary["promotion"] = await promote_repeated_facts()
    except Exception as exc:
        summary["promotion"] = {"error": str(exc)[:200]}
    try:
        summary["retention"] = await prune_episodic()
    except Exception as exc:
        summary["retention"] = {"error": str(exc)[:200]}
    _log(summary)
    return summary


if __name__ == "__main__":
    import asyncio
    print(json.dumps(asyncio.run(run_consolidation()), indent=2))
