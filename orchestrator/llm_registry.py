"""
LLM Registry — complete catalog of available models.

Each model entry defines:
  • provider          : API backend to call
  • model_id          : exact model string for the API
  • tier              : 0 (local/free) → 3 (premium)
  • cost_input_per_m  : USD per 1M input tokens
  • cost_output_per_m : USD per 1M output tokens
  • context_window    : max input tokens
  • max_output        : max response tokens
  • capabilities      : set of task tags this model handles well
  • requires_env      : env var that must be non-empty for this model to be available

Task tags (used by the optimizer):
  classification  — intent detection, routing decisions, yes/no
  retrieval       — summarising retrieved chunks
  summary         — condensing long text
  advisory        — strategic, consultative, business writing
  reasoning       — multi-step logic, analysis, synthesis
  code            — generating or reviewing code
  creative        — proposals, articles, LinkedIn posts
  long_context    — processing docs > 32k tokens
  fast            — latency-sensitive, needs quick response
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

Provider = Literal["ollama", "openai", "anthropic", "google", "deepseek"]

ALL_TASK_TAGS = {
    "classification", "retrieval", "summary", "advisory",
    "reasoning", "code", "creative", "long_context", "fast",
}


@dataclass
class ModelSpec:
    model_id:           str
    provider:           Provider
    tier:               int                        # 0 = free/local, 1 = budget, 2 = mid, 3 = premium
    label:              str                        # human-readable name
    cost_input_per_m:   float                      # USD per 1M input tokens
    cost_output_per_m:  float                      # USD per 1M output tokens
    context_window:     int                        # max input tokens
    max_output:         int                        # max generation tokens
    capabilities:       set[str] = field(default_factory=set)
    requires_env:       str | None = None          # env var key that must be set
    supports_tools:     bool = True                # reliable function/tool calling

    def is_available(self) -> bool:
        if self.requires_env is None:
            return True
        val = os.getenv(self.requires_env, "").strip()
        if not val:
            return False
        # Reject unedited placeholder values from .env.example so a model
        # is not treated as available just because the key var is non-empty.
        low = val.lower()
        if any(m in low for m in ("your_", "_here", "changeme", "replace", "xxxx", "<", "...")):
            return False
        if len(val) < 12:
            return False
        return True

    def estimated_cost_usd(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens  / 1_000_000 * self.cost_input_per_m +
            output_tokens / 1_000_000 * self.cost_output_per_m
        )

    def can_handle(self, input_tokens: int) -> bool:
        """True if the input fits within this model's context window."""
        return input_tokens < self.context_window * 0.90   # 10% safety margin


# ─────────────────────────────────────────────────────────────────────────────
# MODEL CATALOG
# ─────────────────────────────────────────────────────────────────────────────
#
# Pricing source: each provider's public pricing page (as of mid-2025).
# Prices are USD per 1M tokens. Update when providers change rates.
#
MODELS: dict[str, ModelSpec] = {

    # ── Tier 0 — Local / Free ─────────────────────────────────────────────
    "ollama/llama3": ModelSpec(
        model_id          = "llama3",
        provider          = "ollama",
        tier              = 0,
        label             = "Llama 3 8B (local)",
        cost_input_per_m  = 0.0,
        cost_output_per_m = 0.0,
        context_window    = 8_192,
        max_output        = 2_048,
        capabilities      = {"classification", "retrieval", "summary", "fast"},
    ),
    # NOTE: phi3 and llama3:70b were removed from the default catalog because
    # they are not pulled by setup.ps1 (only llama3 + nomic-embed-text are).
    # Selecting an un-pulled model causes a 404 from Ollama. If you pull them
    # (`ollama pull phi3`, `ollama pull llama3:70b`), re-add their entries here.

    # ── Tier 1 — Budget Cloud ─────────────────────────────────────────────
    "deepseek/deepseek-chat": ModelSpec(
        model_id          = "deepseek-chat",
        provider          = "deepseek",
        tier              = 1,
        label             = "DeepSeek Chat",
        cost_input_per_m  = 0.07,
        cost_output_per_m = 0.28,
        context_window    = 64_000,
        max_output        = 8_192,
        capabilities      = {"classification", "retrieval", "summary", "advisory", "code", "creative", "fast"},
        requires_env      = "DEEPSEEK_API_KEY",
    ),
    "google/gemini-2.5-flash-lite": ModelSpec(
        model_id          = "gemini-2.5-flash-lite",
        provider          = "google",
        tier              = 1,
        label             = "Gemini 2.5 Flash-Lite",
        cost_input_per_m  = 0.10,
        cost_output_per_m = 0.40,
        context_window    = 1_000_000,
        max_output        = 8_192,
        capabilities      = {"classification", "retrieval", "summary", "advisory", "creative", "reasoning", "long_context", "fast"},
        requires_env      = "GOOGLE_API_KEY",
    ),
    "google/gemini-2.5-flash": ModelSpec(
        model_id          = "gemini-2.5-flash",
        provider          = "google",
        tier              = 1,
        label             = "Gemini 2.5 Flash",
        cost_input_per_m  = 0.30,
        cost_output_per_m = 2.50,
        context_window    = 1_000_000,
        max_output        = 8_192,
        capabilities      = {"classification", "retrieval", "summary", "advisory", "reasoning", "code", "creative", "long_context", "fast"},
        requires_env      = "GOOGLE_API_KEY",
    ),
    "openai/gpt-4o-mini": ModelSpec(
        model_id          = "gpt-4o-mini",
        provider          = "openai",
        tier              = 1,
        label             = "GPT-4o Mini",
        cost_input_per_m  = 0.15,
        cost_output_per_m = 0.60,
        context_window    = 128_000,
        max_output        = 16_384,
        capabilities      = {"classification", "retrieval", "summary", "advisory", "code", "creative", "fast"},
        requires_env      = "OPENAI_API_KEY",
    ),
    "anthropic/claude-haiku-4-5": ModelSpec(
        model_id          = "claude-haiku-4-5-20251001",
        provider          = "anthropic",
        tier              = 1,
        label             = "Claude Haiku 4.5",
        cost_input_per_m  = 0.25,
        cost_output_per_m = 1.25,
        context_window    = 200_000,
        max_output        = 8_192,
        capabilities      = {"classification", "retrieval", "summary", "advisory", "creative", "fast", "long_context"},
        requires_env      = "ANTHROPIC_API_KEY",
    ),

    # ── Tier 2 — Mid ─────────────────────────────────────────────────────
    "deepseek/deepseek-reasoner": ModelSpec(
        model_id          = "deepseek-reasoner",
        provider          = "deepseek",
        tier              = 2,
        label             = "DeepSeek-R1",
        cost_input_per_m  = 0.55,
        cost_output_per_m = 2.19,
        context_window    = 64_000,
        max_output        = 32_768,
        capabilities      = {"reasoning", "code", "advisory", "summary", "classification"},
        requires_env      = "DEEPSEEK_API_KEY",
        supports_tools    = False,   # deepseek-reasoner (R1) has no function calling
    ),
    "google/gemini-2.5-pro": ModelSpec(
        model_id          = "gemini-2.5-pro",
        provider          = "google",
        tier              = 2,
        label             = "Gemini 2.5 Pro",
        cost_input_per_m  = 1.25,
        cost_output_per_m = 10.00,
        context_window    = 1_000_000,
        max_output        = 8_192,
        capabilities      = {"classification", "retrieval", "summary", "advisory", "reasoning", "code", "creative", "long_context"},
        requires_env      = "GOOGLE_API_KEY",
    ),
    "anthropic/claude-sonnet-4-6": ModelSpec(
        model_id          = "claude-sonnet-4-6",
        provider          = "anthropic",
        tier              = 2,
        label             = "Claude Sonnet 4.6",
        cost_input_per_m  = 3.00,
        cost_output_per_m = 15.00,
        context_window    = 200_000,
        max_output        = 8_192,
        capabilities      = {"classification", "retrieval", "summary", "advisory", "reasoning", "code", "creative", "long_context"},
        requires_env      = "ANTHROPIC_API_KEY",
    ),
    "openai/gpt-4o": ModelSpec(
        model_id          = "gpt-4o",
        provider          = "openai",
        tier              = 2,
        label             = "GPT-4o",
        cost_input_per_m  = 2.50,
        cost_output_per_m = 10.00,
        context_window    = 128_000,
        max_output        = 16_384,
        capabilities      = {"classification", "retrieval", "summary", "advisory", "reasoning", "code", "creative", "long_context"},
        requires_env      = "OPENAI_API_KEY",
    ),

    # ── Tier 3 — Premium ─────────────────────────────────────────────────
    "anthropic/claude-opus-4-8": ModelSpec(
        model_id          = "claude-opus-4-8",
        provider          = "anthropic",
        tier              = 3,
        label             = "Claude Opus 4.8",
        cost_input_per_m  = 15.00,
        cost_output_per_m = 75.00,
        context_window    = 200_000,
        max_output        = 8_192,
        capabilities      = {"classification", "retrieval", "summary", "advisory", "reasoning", "code", "creative", "long_context"},
        requires_env      = "ANTHROPIC_API_KEY",
    ),
    "anthropic/claude-fable-5": ModelSpec(
        model_id          = "claude-fable-5",
        provider          = "anthropic",
        tier              = 3,
        label             = "Claude Fable 5",
        cost_input_per_m  = 15.00,
        cost_output_per_m = 75.00,
        context_window    = 200_000,
        max_output        = 8_192,
        capabilities      = {"classification", "retrieval", "summary", "advisory", "reasoning", "code", "creative", "long_context"},
        requires_env      = "ANTHROPIC_API_KEY",
    ),
    "openai/gpt-4-turbo": ModelSpec(
        model_id          = "gpt-4-turbo",
        provider          = "openai",
        tier              = 3,
        label             = "GPT-4 Turbo",
        cost_input_per_m  = 10.00,
        cost_output_per_m = 30.00,
        context_window    = 128_000,
        max_output        = 4_096,
        capabilities      = {"classification", "retrieval", "summary", "advisory", "reasoning", "code", "creative", "long_context"},
        requires_env      = "OPENAI_API_KEY",
    ),
    "openai/o1": ModelSpec(
        model_id          = "o1",
        provider          = "openai",
        tier              = 3,
        label             = "OpenAI o1",
        cost_input_per_m  = 15.00,
        cost_output_per_m = 60.00,
        context_window    = 200_000,
        max_output        = 100_000,
        capabilities      = {"reasoning", "code", "advisory"},
        requires_env      = "OPENAI_API_KEY",
        supports_tools    = False,   # o1 tool/function calling is unreliable for this loop
    ),
}


def available_models() -> dict[str, ModelSpec]:
    """Return only models whose required env var is set."""
    return {k: v for k, v in MODELS.items() if v.is_available()}


def models_by_tier(max_tier: int = 3) -> dict[str, ModelSpec]:
    """Return available models up to and including max_tier."""
    return {k: v for k, v in available_models().items() if v.tier <= max_tier}
