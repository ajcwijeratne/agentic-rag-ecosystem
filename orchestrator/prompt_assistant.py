"""
Prompt Assistant — rewrites a draft prompt to be clearer and more specific.

Runs on a cheap model (forced to the budget tier) so improving a prompt costs
a fraction of a cent. Returns the improved prompt text only — no preamble.
"""

from __future__ import annotations

_IMPROVE_SYSTEM = """\
You are a prompt engineer. Rewrite the user's draft prompt so an AI assistant
will produce a sharper, more useful answer. Apply these rules:

- Keep the user's intent and subject exactly. Do not answer the prompt.
- Make the ask specific: state the deliverable, format, length, and audience
  where they can be reasonably inferred.
- Add any obviously useful constraints or context the draft implies.
- Remove vagueness and filler. Prefer concrete verbs and named specifics.
- Keep it concise. One tight paragraph, or a short instruction plus 2-4 bullet
  constraints if that serves the task better.

Output ONLY the improved prompt. No explanation, no quotes, no preamble.
"""


async def improve_prompt(draft: str, max_tier: int = 1) -> dict:
    """
    Return an improved version of a draft prompt.

    Uses the fallback chain at the budget tier (cheap models first), so it is
    near-free and resilient to a provider being down.
    """
    from .fallback_chain import call_with_fallback

    draft = (draft or "").strip()
    if not draft:
        return {"improved": "", "original": "", "model": "", "cost_usd": 0.0}

    resp = await call_with_fallback(
        user_message    = f"Draft prompt to improve:\n\n{draft}",
        system_prompt   = _IMPROVE_SYSTEM,
        force_task_type = "fast",     # cheap, low-latency tier
        max_tier        = max_tier,
    )

    improved = (resp.content or "").strip()
    # Strip wrapping quotes if the model added them
    if len(improved) >= 2 and improved[0] in "\"'" and improved[-1] in "\"'":
        improved = improved[1:-1].strip()

    return {
        "improved":  improved or draft,
        "original":  draft,
        "model":     resp.model_label,
        "provider":  resp.provider,
        "cost_usd":  resp.cost_usd,
        "error":     resp.error,
    }
