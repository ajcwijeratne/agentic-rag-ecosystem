"""
Memory Agent
============
Two async functions used around every WijerCo agent call:

  extract_and_record_evidence(department, query, response)
      After a WijerCo response, uses a local LLM (Ollama/llama3) to extract
      candidate facts (client names, project details, decisions) and records
      them as UNVERIFIED EVIDENCE. They are not recallable. Promotion into the
      semantic store happens only in memory.consolidation.verify_candidates(),
      and only when the knowledge base corroborates the claim. Generated text
      is evidence, never knowledge.

  recall(query, department)
      Before a WijerCo response, retrieves semantically relevant memories and
      returns them as a formatted text block ready for system prompt injection.

Extraction prompt keeps it lightweight — runs Tier-0 local model only.
"""

from __future__ import annotations

import logging
import os
import re

from .memory_store import store

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = """\
You are a memory extraction assistant. Given a conversation exchange, extract
facts worth remembering long-term: client names, project names, decisions made,
stated preferences, key dates, or commitments. Output ONLY a JSON array of
objects with keys "entity" and "fact". If nothing memorable, output [].

Example: [{"entity": "Swinburne", "fact": "Requires TEQSA-compliant unit outlines."}]
"""


# Tag on candidate entries in project memory. memory/consolidation.py reads
# this to find things awaiting verification, and skips them in the ordinary
# recurrence-based promotion pass, where repetition alone would be enough.
CANDIDATE_KIND = "agent_candidate"

_PLACEHOLDER = re.compile(r"\[[^\]]{2,60}\]")
_ERRORISH = re.compile(r"\b(error|traceback|exception|failed to|no response)\b", re.I)
_FILLER = {"n/a", "none", "unknown", "not specified", "not provided", "tbd"}


def _is_storable(entity: str, fact: str) -> bool:
    """Reject things that are not durable facts about the world.

    Facts are extracted from the ASSISTANT'S OWN REPLY, so whatever it wrote
    can come back later as though it were established truth. On 4 Sep 2026 a
    drafted email containing "[Link to LMS password reset page]" was stored as
    a fact about the entity "LMS", and then recalled into an unrelated question
    about a portal outage, which the agent answered by telling the user to
    reset their password. Template scaffolding is not knowledge, and neither is
    an error string.

    This filters the unambiguous junk. It does not address the wider question
    of whether generated replies should seed long-term memory at all.
    """
    e, f = entity.strip(), fact.strip()
    if len(f) < 8 or len(e) < 2:
        return False
    if f.lower() in _FILLER or e.lower() in _FILLER:
        return False
    # Unresolved placeholders: "[Student Name]", "[Link to ...]", "[Your Name]"
    if _PLACEHOLDER.search(f) or _PLACEHOLDER.search(e):
        return False
    if _ERRORISH.search(f) or _ERRORISH.search(e):
        return False
    return True


async def extract_and_record_evidence(
    department: str,
    query:      str,
    response:   str,
) -> int:
    """Extract candidate facts from a query+response pair as EVIDENCE ONLY.

    These facts come from the assistant's own reply, which is generated text,
    not established truth. Nothing here is recallable. Candidates land in the
    project-memory tier tagged CANDIDATE_KIND, and only reach the semantic
    store that recall() reads if verify_candidates() finds the knowledge base
    corroborates them.

    Until 4 Sep 2026 this wrote straight into the semantic store, so the system
    treated whatever it had just said as a fact about the world. A drafted
    email became a "fact" about the LMS and was recalled into an unrelated
    question days later. Returns the number of candidates recorded.
    """
    import json
    from orchestrator.multi_llm import call_model

    extraction_prompt = (
        f"User asked ({department} agent):\n{query}\n\n"
        f"Assistant replied:\n{response[:1200]}"
    )

    try:
        result = await call_model(
            user_message    = extraction_prompt,
            system_prompt   = _EXTRACT_SYSTEM,
            force_model_key = "ollama/llama3",  # always use local for memory extraction
        )
        raw = result.content.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        facts = json.loads(raw)
    except Exception as exc:
        logger.debug(f"[memory_agent] Extraction failed: {exc}")
        return 0

    if not isinstance(facts, list):
        return 0

    from orchestrator.operating import add_project_memory

    count = 0
    for item in facts:
        if isinstance(item, dict) and "entity" in item and "fact" in item:
            entity, fact = str(item["entity"]), str(item["fact"])
            if not _is_storable(entity, fact):
                logger.debug("[memory_agent] Rejected non-fact: %r / %r", entity, fact)
                continue
            try:
                # Evidence, not knowledge. This goes to the candidate tier and
                # is NOT recallable. memory.consolidation.verify_candidates()
                # promotes it into the semantic store only if the knowledge
                # base corroborates it.
                add_project_memory(
                    project = department,
                    content = f"{entity}: {fact}",
                    source  = "agent-extraction",
                    meta    = {
                        "kind":       CANDIDATE_KIND,
                        "entity":     entity,
                        "fact":       fact,
                        "department": department,
                        "query":      query[:300],
                        "verified":   False,
                    },
                )
                count += 1
            except Exception as exc:
                logger.debug(f"[memory_agent] Candidate record failed: {exc}")

    if count:
        logger.info(
            "[memory_agent] Recorded %d unverified candidate(s) from %s agent",
            count, department,
        )

    return count


async def recall(
    query:      str,
    department: str | None = None,
    top_k:      int = 4,
) -> str:
    """
    Layered recall across the memory tiers, composed into one prompt block:
      • Semantic  — durable entity/client facts
      • Episodic  — summaries of relevant past conversations

    (The working tier — the live session — is supplied separately as chat
    history, so it is not duplicated here.)

    Returns empty string if nothing relevant or on any error.
    """
    blocks: list[str] = []

    # Semantic tier
    try:
        memories = await store.recall(query=query, top_k=top_k)
        sem = store.format_for_prompt(memories)
        if sem:
            blocks.append(sem)
    except Exception as exc:
        logger.debug(f"[memory_agent] Semantic recall failed: {exc}")

    # Episodic tier
    try:
        from .episodic import recall_episodes
        episodes = await recall_episodes(query, top_k=3)
        if episodes:
            lines = ["[Relevant past conversations:]"]
            for e in episodes:
                lines.append(f"• {e.summary}")
            blocks.append("\n".join(lines))
    except Exception as exc:
        logger.debug(f"[memory_agent] Episodic recall failed: {exc}")

    return "\n\n".join(blocks)
