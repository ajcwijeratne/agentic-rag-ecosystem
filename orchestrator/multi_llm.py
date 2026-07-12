"""
Unified Multi-LLM Caller
=========================
Single async function `call_model()` that routes to any provider:
  • Ollama   (local)
  • Anthropic (Claude)
  • OpenAI   (GPT)
  • Google   (Gemini)
  • DeepSeek (OpenAI-compatible endpoint)

Also exposes `stream_model()` — an async generator that yields string chunks
from any provider for use with SSE streaming endpoints.

Returns a standardised LLMResponse regardless of provider.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

import httpx

from .llm_registry import ModelSpec, MODELS
from .token_optimizer import OptimizationResult, pick_model, rough_token_count


# ─────────────────────────────────────────────────────────────────────────────
# Response schema
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class LLMResponse:
    content:        str
    model_key:      str
    model_label:    str
    provider:       str
    input_tokens:   int
    output_tokens:  int
    cost_usd:       float
    latency_ms:     int
    task_type:      str
    error:          str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Provider callers
# ─────────────────────────────────────────────────────────────────────────────

async def _call_ollama(model_id: str, system: str, user: str, history: list[dict]) -> tuple[str, int, int]:
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(history)
    messages.append({"role": "user", "content": user})

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{base}/api/chat",
            json={"model": model_id, "messages": messages, "stream": False},
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()

    content      = data["message"]["content"]
    input_tok    = data.get("prompt_eval_count", rough_token_count(user + system))
    output_tok   = data.get("eval_count", rough_token_count(content))
    return content, input_tok, output_tok


async def _call_anthropic(model_id: str, system: str, user: str, history: list[dict], max_tokens: int) -> tuple[str, int, int]:
    import anthropic
    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    messages = list(history) + [{"role": "user", "content": user}]
    resp = await client.messages.create(
        model      = model_id,
        max_tokens = max_tokens,
        system     = system or anthropic.NOT_GIVEN,
        messages   = messages,
    )
    content    = resp.content[0].text
    input_tok  = resp.usage.input_tokens
    output_tok = resp.usage.output_tokens
    return content, input_tok, output_tok


async def _call_openai(model_id: str, system: str, user: str, history: list[dict], max_tokens: int) -> tuple[str, int, int]:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(history)
    messages.append({"role": "user", "content": user})
    resp = await client.chat.completions.create(
        model      = model_id,
        messages   = messages,
        max_tokens = max_tokens,
    )
    content    = resp.choices[0].message.content or ""
    input_tok  = resp.usage.prompt_tokens
    output_tok = resp.usage.completion_tokens
    return content, input_tok, output_tok


GOOGLE_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"


async def _call_google(model_id: str, system: str, user: str, history: list[dict], max_tokens: int) -> tuple[str, int, int]:
    # Route Gemini through its OpenAI-compatible endpoint. The native
    # google.generativeai package is end-of-life; this uses the openai client
    # we already depend on and works with current Gemini 2.5 / 3.x models.
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key  = os.getenv("GOOGLE_API_KEY", ""),
        base_url = os.getenv("GOOGLE_OPENAI_BASE", GOOGLE_OPENAI_BASE),
    )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(history)
    messages.append({"role": "user", "content": user})
    resp = await client.chat.completions.create(
        model      = model_id,
        messages   = messages,
        max_tokens = max_tokens,
    )
    content    = resp.choices[0].message.content or ""
    input_tok  = resp.usage.prompt_tokens
    output_tok = resp.usage.completion_tokens
    return content, input_tok, output_tok


async def _call_deepseek(model_id: str, system: str, user: str, history: list[dict], max_tokens: int) -> tuple[str, int, int]:
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key  = os.getenv("DEEPSEEK_API_KEY", ""),
        base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
    )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(history)
    messages.append({"role": "user", "content": user})
    resp = await client.chat.completions.create(
        model      = model_id,
        messages   = messages,
        max_tokens = max_tokens,
    )
    content    = resp.choices[0].message.content or ""
    input_tok  = resp.usage.prompt_tokens
    output_tok = resp.usage.completion_tokens
    return content, input_tok, output_tok


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

async def call_model(
    user_message:    str,
    system_prompt:   str = "",
    history:         list[dict] | None = None,
    force_model_key: str | None = None,
    force_task_type: str | None = None,
    max_tier:        int = 3,
) -> LLMResponse:
    """
    Call the optimal model for this task and return a standardised LLMResponse.

    Args:
        user_message:    The query / instruction.
        system_prompt:   Optional system context.
        history:         Prior conversation turns [{role, content}].
        force_model_key: Pin to a specific model key from llm_registry.
        force_task_type: Override auto-detected task classification.
        max_tier:        Cap tier (0=local, 1=budget, 2=mid, 3=all).
    """
    history = history or []
    opt = pick_model(
        query           = user_message,
        system_prompt   = system_prompt,
        force_model_key = force_model_key,
        force_task_type = force_task_type,
        max_tier        = max_tier,
    )
    spec  = opt.model
    t0    = time.monotonic()
    error = None

    try:
        provider_fn = {
            "ollama":    _call_ollama,
            "anthropic": _call_anthropic,
            "openai":    _call_openai,
            "google":    _call_google,
            "deepseek":  _call_deepseek,
        }.get(spec.provider)

        if provider_fn is None:
            raise ValueError(f"Unknown provider: {spec.provider}")

        if spec.provider == "ollama":
            content, in_tok, out_tok = await provider_fn(
                spec.model_id, system_prompt, user_message, history
            )
        else:
            content, in_tok, out_tok = await provider_fn(
                spec.model_id, system_prompt, user_message, history, spec.max_output
            )

    except Exception as exc:
        content  = f"[{spec.provider}/{spec.model_id} error: {exc}]"
        in_tok   = opt.input_tokens
        out_tok  = 0
        error    = str(exc)

    latency_ms = int((time.monotonic() - t0) * 1000)
    cost_usd   = spec.estimated_cost_usd(in_tok, out_tok)

    return LLMResponse(
        content      = content,
        model_key    = opt.model_key,
        model_label  = spec.label,
        provider     = spec.provider,
        input_tokens = in_tok,
        output_tokens= out_tok,
        cost_usd     = cost_usd,
        latency_ms   = latency_ms,
        task_type    = opt.task_type,
        error        = error,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Streaming entry point
# ─────────────────────────────────────────────────────────────────────────────

async def stream_model(
    user_message:  str,
    system_prompt: str = "",
    history:       list[dict] | None = None,
    model_key:     str | None = None,
) -> AsyncGenerator[str, None]:
    """
    Yield string chunks (tokens) from a provider as they arrive.

    Uses streaming APIs where available:
      • Ollama    — native streaming (/api/chat stream=True)
      • Anthropic — stream_message()
      • OpenAI    — stream=True
      • DeepSeek  — stream=True (OpenAI-compat)
      • Google    — stream=True (generate_content_async stream)

    Raises on connection or auth errors so fallback_chain can catch them.
    """
    history   = history or []
    opt       = pick_model(user_message, system_prompt, force_model_key=model_key)
    spec      = opt.model

    if spec.provider == "ollama":
        async for chunk in _stream_ollama(spec.model_id, system_prompt, user_message, history):
            yield chunk

    elif spec.provider == "anthropic":
        async for chunk in _stream_anthropic(spec.model_id, system_prompt, user_message, history, spec.max_output):
            yield chunk

    elif spec.provider in ("openai", "deepseek", "google"):
        async for chunk in _stream_openai_compat(spec, system_prompt, user_message, history):
            yield chunk

    else:
        # Fallback: non-streaming call, emit as single chunk
        resp = await call_model(user_message, system_prompt, history, force_model_key=model_key)
        yield resp.content


# ── Per-provider streaming helpers ───────────────────────────────────────────

async def _stream_ollama(model_id: str, system: str, user: str, history: list[dict]) -> AsyncGenerator[str, None]:
    base = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(history)
    messages.append({"role": "user", "content": user})

    import json
    async with httpx.AsyncClient() as client:
        async with client.stream(
            "POST",
            f"{base}/api/chat",
            json={"model": model_id, "messages": messages, "stream": True},
            timeout=120.0,
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line:
                    continue
                data = json.loads(line)
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    yield chunk
                if data.get("done"):
                    break


async def _stream_anthropic(model_id: str, system: str, user: str, history: list[dict], max_tokens: int) -> AsyncGenerator[str, None]:
    import anthropic
    client   = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    messages = list(history) + [{"role": "user", "content": user}]

    async with client.messages.stream(
        model      = model_id,
        max_tokens = max_tokens,
        system     = system or anthropic.NOT_GIVEN,
        messages   = messages,
    ) as stream:
        async for text in stream.text_stream:
            yield text


async def _stream_openai_compat(spec: "ModelSpec", system: str, user: str, history: list[dict]) -> AsyncGenerator[str, None]:
    from openai import AsyncOpenAI
    if spec.provider == "deepseek":
        client = AsyncOpenAI(
            api_key  = os.getenv("DEEPSEEK_API_KEY", ""),
            base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"),
        )
    elif spec.provider == "google":
        client = AsyncOpenAI(
            api_key  = os.getenv("GOOGLE_API_KEY", ""),
            base_url = os.getenv("GOOGLE_OPENAI_BASE", GOOGLE_OPENAI_BASE),
        )
    else:
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(history)
    messages.append({"role": "user", "content": user})

    stream = await client.chat.completions.create(
        model      = spec.model_id,
        messages   = messages,
        max_tokens = spec.max_output,
        stream     = True,
    )
    async for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            yield delta


# _stream_google removed — Gemini now streams via the OpenAI-compatible path
# in _stream_openai_compat (provider == "google"). The end-of-life
# google.generativeai package is no longer imported anywhere.
