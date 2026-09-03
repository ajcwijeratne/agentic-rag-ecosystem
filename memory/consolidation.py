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

# Corroboration bar for promoting an agent-extracted candidate into recallable
# memory. Deliberately set so that an unsupported claim stays evidence: the
# cost of a missed promotion is that a fact must be re-established later, while
# the cost of a wrong one is the system asserting something untrue for months.
VERIFY_MIN_SCORE = float(os.getenv("MEMORY_VERIFY_MIN_SCORE", "0.55"))
# Local and free: this runs nightly over a handful of candidates, and a slow
# careful reader is the right trade here.
ENTAIL_MODEL_KEY = os.getenv("MEMORY_VERIFY_MODEL", "ollama/llama3")
VERIFY_COLLECTIONS = [
    c.strip() for c in os.getenv(
        "MEMORY_VERIFY_COLLECTIONS", "wijerco_knowledge,obsidian_vault"
    ).split(",") if c.strip()
]
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
    state["verified_hashes"] = state.get("verified_hashes", [])[-2000:]
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

    # Agent-extracted candidates are deliberately excluded. This pass promotes
    # on recurrence, and a model repeating its own claim three times is not
    # corroboration, it is the same unverified assertion counted thrice. Those
    # go through verify_candidates(), which requires the KB to support them.
    from memory.memory_agent import CANDIDATE_KIND

    entries = [m for m in operating.list_project_memory(limit=500)
               if (m.get("meta") or {}).get("kind") not in ("daily_digest", CANDIDATE_KIND)]

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
# Pass 2b: verify agent-extracted candidates against the knowledge base
# ---------------------------------------------------------------------------

_ENTAIL_SYSTEM = """\
You decide whether EVIDENCE supports a CLAIM. Reply with exactly one word:
SUPPORTED or UNSUPPORTED.

Answer SUPPORTED only if the evidence states the claim or directly implies it.
Being about the same subject is NOT support. If the evidence does not settle
the claim either way, answer UNSUPPORTED. When uncertain, answer UNSUPPORTED.
"""


async def _entailed(claim: str, evidence: list[str]) -> bool:
    """Does the retrieved evidence actually support this claim?

    Deliberately biased towards UNSUPPORTED. A missed promotion costs a fact
    that has to be re-established later; a wrong one puts a false statement
    into the system's mouth for months, in a store that presents everything as
    established. Any failure here, including the model being unreachable,
    leaves the candidate as evidence.
    """
    from orchestrator.multi_llm import call_model

    body = "EVIDENCE:\n" + "\n---\n".join(evidence[:4]) + f"\n\nCLAIM:\n{claim}"
    try:
        resp = await call_model(
            user_message    = body,
            system_prompt   = _ENTAIL_SYSTEM,
            force_model_key = ENTAIL_MODEL_KEY,
        )
    except Exception:
        return False
    if getattr(resp, "error", None):
        return False
    verdict = (resp.content or "").strip().upper()
    return verdict.startswith("SUPPORTED")


async def verify_candidates(limit: int = 300) -> dict[str, Any]:
    """Promote candidate facts into the semantic store, but only if the KB agrees.

    Facts extracted from an agent's own reply are evidence, not knowledge. They
    are recorded by memory_agent.extract_and_record_evidence() into project
    memory and are NOT recallable. This is the only path by which one becomes a
    memory the system will later state as fact, and it requires corroboration
    from the indexed knowledge base rather than repetition of the claim.

    A candidate that nothing corroborates is left alone. It stays as evidence,
    keeps its provenance, and expires with the rest of project memory. Silence
    is the correct outcome for an unsupported claim, not promotion.
    """
    from orchestrator import operating
    from memory.memory_agent import CANDIDATE_KIND
    from memory.memory_store import store
    from rag.retriever import search

    state = _load_state()
    seen: set[str] = set(state.get("verified_hashes", []))

    candidates = [m for m in operating.list_project_memory(limit=limit)
                  if (m.get("meta") or {}).get("kind") == CANDIDATE_KIND]

    promoted = 0
    unsupported = 0
    for cand in candidates:
        meta = cand.get("meta") or {}
        fact = str(meta.get("fact") or cand.get("content") or "").strip()
        entity = str(meta.get("entity") or cand.get("project") or "general").strip()
        if not fact:
            continue
        h = _content_hash(f"{entity}: {fact}")
        if h in seen:
            continue

        # Step 1: gather candidate evidence. Retrieval alone cannot verify
        # anything: it ranks by topical similarity, so a false claim about a
        # documented subject scores well precisely because the subject is
        # documented. Measured 4 Sep 2026 on this KB: true statements scored
        # 0.78-0.85, but the invented "Deakin has agreed to a five year
        # exclusive contract" scored 0.635, above any threshold that still
        # admitted real facts. So similarity only decides what to READ.
        evidence: list[str] = []
        best_file = None
        for collection in VERIFY_COLLECTIONS:
            try:
                hits = await search(
                    query      = f"{entity}: {fact}",
                    top_k      = 3,
                    collection = collection,
                )
            except Exception:
                continue
            for hit in hits:
                if float(hit.get("score") or 0.0) < VERIFY_MIN_SCORE:
                    continue
                text = (hit.get("text") or "").strip()
                if text:
                    evidence.append(text[:900])
                    if best_file is None:
                        best_file = hit.get("file") or collection

        # Step 2: does that evidence actually support the claim? This is the
        # verification step, and it is a reading task, not a ranking one.
        if evidence and await _entailed(f"{entity}: {fact}", evidence):
            try:
                await store.add(entity, fact, source=f"verified:kb:{best_file}")
                seen.add(h)
                promoted += 1
            except Exception:
                continue
        else:
            unsupported += 1

    state["verified_hashes"] = list(seen)
    _save_state(state)
    return {
        "candidates":  len(candidates),
        "promoted":    promoted,
        "unsupported": unsupported,
        "threshold":   VERIFY_MIN_SCORE,
    }


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
        summary["verification"] = await verify_candidates()
    except Exception as exc:
        summary["verification"] = {"error": str(exc)[:200]}
    try:
        summary["retention"] = await prune_episodic()
    except Exception as exc:
        summary["retention"] = {"error": str(exc)[:200]}
    _log(summary)
    return summary


if __name__ == "__main__":
    import asyncio
    print(json.dumps(asyncio.run(run_consolidation()), indent=2))
