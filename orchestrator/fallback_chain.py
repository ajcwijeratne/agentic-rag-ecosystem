"""
Provider Fallback Chain
=======================
Wraps call_model() with automatic retry logic.

On any provider error (rate-limit 429, server error 5xx, timeout, auth failure),
the chain marks that provider as temporarily unavailable and immediately retries
with the next cheapest capable model from a higher tier.

Failure tracking is per-instance (resets on server restart). For persistent
failure tracking across restarts, swap the set for a Redis key with a TTL.

Usage:
    from .fallback_chain import call_with_fallback

    response = await call_with_fallback(
        user_message="...",
        system_prompt="...",
        max_retries=4,
    )
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator

from .llm_registry import MODELS, available_models
from .multi_llm import LLMResponse, call_model
from .token_optimizer import pick_model, classify_task, rough_token_count

logger = logging.getLogger(__name__)

# Providers that have errored: {provider: failed_until_epoch}. Persisted to disk
# so a cooldown survives a server restart; the in-memory dict is a write-through
# cache.
import json as _json
import os as _os
from pathlib import Path as _Path

_FAILURE_COOLDOWN: float = float(_os.getenv("PROVIDER_FAILURE_COOLDOWN", "120"))
_COOLDOWN_PATH = _Path(_os.getenv(
    "PROVIDER_COOLDOWN_PATH",
    str(_Path(__file__).parent.parent / "logs" / "provider_cooldowns.json"),
))


def _load_cooldowns() -> dict[str, float]:
    try:
        if _COOLDOWN_PATH.exists():
            with _COOLDOWN_PATH.open("r", encoding="utf-8") as fh:
                data = _json.load(fh)
            now = time.time()
            return {p: u for p, u in data.items() if u > now}
    except Exception:
        pass
    return {}


def _save_cooldowns() -> None:
    try:
        _COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _COOLDOWN_PATH.open("w", encoding="utf-8") as fh:
            _json.dump(_FAILED_PROVIDERS, fh)
    except Exception:
        pass


_FAILED_PROVIDERS: dict[str, float] = _load_cooldowns()


def _is_retryable(exc: Exception) -> bool:
    """
    True if the error means 'try a different provider'.

    Covers transient errors (rate limits, 5xx, timeouts) AND account-level
    failures (insufficient balance, billing, expired key) — in all these cases
    the same request will succeed on a different provider, so we fall through.
    """
    msg = str(exc).lower()
    return any(k in msg for k in [
        "429", "rate limit", "quota", "overloaded",
        "500", "502", "503", "504", "timeout", "connection",
        "service unavailable", "internal server error",
        # Account / billing failures — skip this provider, try the next
        "402", "insufficient balance", "insufficient_quota",
        "billing", "payment", "exceeded your current quota",
        # Bad / expired key — skip this provider
        "401", "403", "invalid api key", "authentication",
        # Model not available on this backend (e.g. not pulled in Ollama)
        "404", "not found", "model not found",
    ])


def _mark_failed(provider: str) -> None:
    _FAILED_PROVIDERS[provider] = time.time() + _FAILURE_COOLDOWN
    _save_cooldowns()
    logger.warning(f"[fallback] Provider '{provider}' marked unavailable for {_FAILURE_COOLDOWN}s")


def _is_available(provider: str) -> bool:
    until = _FAILED_PROVIDERS.get(provider, 0)
    if time.time() > until:
        if provider in _FAILED_PROVIDERS:
            _FAILED_PROVIDERS.pop(provider, None)
            _save_cooldowns()
        return True
    return False


def _candidate_models(
    query: str,
    system_prompt: str,
    exclude_providers: set[str],
    max_tier: int = 3,
    force_task_type: str | None = None,
) -> list[str]:
    """
    Return model keys ordered by ascending cost, excluding failed providers
    and any providers in exclude_providers.
    """
    input_tokens = rough_token_count(query + system_prompt)
    task_type    = force_task_type or classify_task(query, input_tokens)

    candidates = []
    for key, spec in available_models().items():
        if spec.provider in exclude_providers:
            continue
        if not _is_available(spec.provider):
            continue
        if spec.tier > max_tier:
            continue
        if not spec.can_handle(input_tokens):
            continue
        if task_type not in spec.capabilities:
            continue
        cost = spec.estimated_cost_usd(input_tokens, 600)
        candidates.append((key, cost))

    candidates.sort(key=lambda x: x[1])
    return [k for k, _ in candidates]


async def call_with_fallback(
    user_message:    str,
    system_prompt:   str = "",
    history:         list[dict] | None = None,
    force_model_key: str | None = None,
    force_task_type: str | None = None,
    max_tier:        int = 3,
    max_retries:     int = 4,
    trace:           "object | None" = None,
) -> LLMResponse:
    """
    Call the optimal model with automatic provider fallback.

    Tries up to max_retries different models before raising.
    On retryable error: marks provider failed, tries next cheapest candidate.
    On non-retryable error (e.g. bad request): raises immediately.
    """
    # If a model is forced, try it directly without fallback
    if force_model_key:
        return await call_model(
            user_message    = user_message,
            system_prompt   = system_prompt,
            history         = history,
            force_model_key = force_model_key,
            force_task_type = force_task_type,
            max_tier        = max_tier,
        )

    tried_providers: set[str] = set()
    last_exc: Exception | None = None

    for attempt in range(max_retries):
        candidates = _candidate_models(
            user_message,
            system_prompt,
            tried_providers,
            max_tier,
            force_task_type,
        )
        if not candidates:
            break

        model_key = candidates[0]
        spec      = MODELS[model_key]

        logger.info(f"[fallback] Attempt {attempt+1}: {spec.label} ({spec.provider})")

        try:
            response = await call_model(
                user_message    = user_message,
                system_prompt   = system_prompt,
                history         = history,
                force_model_key = model_key,
                force_task_type = force_task_type,
                max_tier        = max_tier,
            )

            if response.error and _is_retryable(Exception(response.error)):
                raise RuntimeError(response.error)

            if attempt > 0:
                logger.info(f"[fallback] Succeeded on attempt {attempt+1} with {spec.label}")

            return response

        except Exception as exc:
            last_exc = exc
            if _is_retryable(exc):
                _mark_failed(spec.provider)
                tried_providers.add(spec.provider)
                logger.warning(f"[fallback] {spec.provider} failed: {exc}. Trying next.")
                if trace is not None:
                    try:
                        trace.add_event("model_fallback", attempt=attempt + 1,
                                        provider=spec.provider, model_key=model_key,
                                        error=str(exc)[:200])
                    except Exception:
                        pass
                await asyncio.sleep(0.5 * (attempt + 1))   # brief back-off
            else:
                # Non-retryable (bad API key, malformed request) — fail fast
                raise

    # Degraded last resort: force the local Ollama model regardless of tier
    # or capability filters. It is free and always available if Ollama is up.
    logger.warning("[fallback] All cloud candidates failed; trying local ollama/llama3 as last resort.")
    try:
        return await call_model(
            user_message    = user_message,
            system_prompt   = system_prompt,
            history         = history,
            force_model_key = "ollama/llama3",
            force_task_type = force_task_type,
        )
    except Exception as exc:
        last_exc = exc

    logger.error(f"[fallback] All providers exhausted, including local. Last error: {last_exc}")
    return LLMResponse(
        content       = (
            "[No model could be reached. Cloud providers failed "
            f"({last_exc}); local Ollama is also unavailable. "
            "Check your API keys/balance in .env, or confirm Ollama is running "
            "with `ollama list`.]"
        ),
        model_key     = "none",
        model_label   = "None",
        provider      = "none",
        input_tokens  = 0,
        output_tokens = 0,
        cost_usd      = 0.0,
        latency_ms    = 0,
        task_type     = "unknown",
        error         = str(last_exc),
    )


# ── Streaming variant ──────────────────────────────────────────────────────

async def stream_with_fallback(
    user_message:    str,
    system_prompt:   str = "",
    history:         list[dict] | None = None,
    force_model_key: str | None = None,
    force_task_type: str | None = None,
    max_tier:        int = 3,
) -> AsyncGenerator[dict, None]:
    """
    Streaming version. Yields dicts:
      {"token": "...", "done": False, "model_key": "..."}
      {"token": "",    "done": True,  "model_key": "...", "cost_usd": ..., "provider": ...}

    Falls back to non-streaming call_with_fallback on providers that don't
    support streaming, emitting the full response as a single token.
    """
    from .multi_llm import stream_model

    tried_providers: set[str] = set()

    for attempt in range(4):
        candidates = _candidate_models(
            user_message,
            system_prompt,
            tried_providers,
            max_tier,
            force_task_type,
        ) if not force_model_key else [force_model_key]

        if not candidates:
            break

        model_key = candidates[0]
        spec      = MODELS[model_key]

        try:
            full_content = ""
            async for chunk in stream_model(
                user_message  = user_message,
                system_prompt = system_prompt,
                history       = history or [],
                model_key     = model_key,
            ):
                full_content += chunk
                yield {"token": chunk, "done": False, "model_key": model_key}

            from .token_optimizer import rough_token_count
            out_tok = rough_token_count(full_content)
            in_tok  = rough_token_count(user_message + system_prompt)

            yield {
                "token":     "",
                "done":      True,
                "model_key": model_key,
                "model_label": spec.label,
                "provider":  spec.provider,
                "cost_usd":  spec.estimated_cost_usd(in_tok, out_tok),
                "input_tokens":  in_tok,
                "output_tokens": out_tok,
                "task_type": force_task_type or classify_task(user_message),
            }
            return

        except Exception as exc:
            if _is_retryable(exc):
                _mark_failed(spec.provider)
                tried_providers.add(spec.provider)
                await asyncio.sleep(0.3)
            else:
                # Non-retryable (bad key, model not pulled, etc.) — surface the
                # real error in the chat instead of resetting the connection.
                yield {
                    "token":       f"[{spec.provider}/{spec.model_id} error: {exc}]",
                    "done":        True,
                    "model_key":   model_key,
                    "model_label": spec.label,
                    "provider":    spec.provider,
                    "cost_usd":    0.0,
                }
                return

    # Degraded last resort: stream from local Ollama regardless of filters.
    try:
        from .multi_llm import stream_model
        from .token_optimizer import rough_token_count
        full = ""
        async for chunk in stream_model(
            user_message  = user_message,
            system_prompt = system_prompt,
            history       = history or [],
            model_key     = "ollama/llama3",
        ):
            full += chunk
            yield {"token": chunk, "done": False, "model_key": "ollama/llama3"}
        yield {
            "token":         "",
            "done":          True,
            "model_key":     "ollama/llama3",
            "model_label":   "Llama 3 8B (local)",
            "provider":      "ollama",
            "cost_usd":      0.0,
            "input_tokens":  rough_token_count(user_message + system_prompt),
            "output_tokens": rough_token_count(full),
        }
        return
    except Exception as exc:
        yield {
            "token": (
                "[No model could be reached. Cloud providers failed and local "
                f"Ollama is also unavailable ({exc}). Check API keys/balance in "
                ".env, or run `ollama list` to confirm llama3 is pulled.]"
            ),
            "done": True, "model_key": "none", "cost_usd": 0.0,
        }
