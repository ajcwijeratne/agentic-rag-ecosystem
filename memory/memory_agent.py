"""
Memory Agent
============
Two async functions used around every WijerCo agent call:

  extract_and_store(department, query, response)
      After a WijerCo response, uses a local LLM (Ollama/llama3) to extract
      memorable facts (client names, project details, decisions) and stores them.

  recall(query, department)
      Before a WijerCo response, retrieves semantically relevant memories and
      returns them as a formatted text block ready for system prompt injection.

Extraction prompt keeps it lightweight — runs Tier-0 local model only.
"""

from __future__ import annotations

import logging
import os

from .memory_store import store

logger = logging.getLogger(__name__)

_EXTRACT_SYSTEM = """\
You are a memory extraction assistant. Given a conversation exchange, extract
facts worth remembering long-term: client names, project names, decisions made,
stated preferences, key dates, or commitments. Output ONLY a JSON array of
objects with keys "entity" and "fact". If nothing memorable, output [].

Example: [{"entity": "Swinburne", "fact": "Requires TEQSA-compliant unit outlines."}]
"""


async def extract_and_store(
    department: str,
    query:      str,
    response:   str,
) -> int:
    """
    Extract memorable facts from the query+response pair and persist them.
    Returns the number of memories stored. Runs silently on failure.
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

    count = 0
    for item in facts:
        if isinstance(item, dict) and "entity" in item and "fact" in item:
            try:
                await store.add(
                    entity  = str(item["entity"]),
                    content = str(item["fact"]),
                    source  = department,
                )
                count += 1
            except Exception as exc:
                logger.debug(f"[memory_agent] Store failed: {exc}")

    if count:
        logger.info(f"[memory_agent] Stored {count} memories from {department} agent")

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
