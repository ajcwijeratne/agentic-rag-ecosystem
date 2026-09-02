"""
Screen awareness — description
==============================
Turns a captured image into an answer. Kept deliberately separate from
`orchestrator.multi_llm`: adding an image parameter would mean threading it
through all five provider functions that every text call also uses, and this
capability is not worth that risk to the main chat path.

Provider order is local-first, which is the whole point. If Ollama is serving a
vision model the screenshot never leaves the machine, matching the rest of the
voice stack. Only if no local model exists does it fall back to a cloud
provider — and that is a real privacy change, so it is reported in the result
and written to the audit log rather than happening quietly.

To keep it entirely local:

    ollama pull llama3.2-vision        # or llava, qwen2.5vl, moondream
    VISION_PROVIDER=ollama
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass

import httpx

OLLAMA_URL: str = os.getenv("OLLAMA_BASE_URL", os.getenv("OLLAMA_URL", "http://localhost:11434"))
VISION_PROVIDER: str = os.getenv("VISION_PROVIDER", "auto").lower()
VISION_TIMEOUT: float = float(os.getenv("VISION_TIMEOUT_S", "120"))

# Ollama tags that can actually see. Checked as prefixes against installed models.
LOCAL_VISION_MODELS: tuple = (
    "llama3.2-vision", "llava", "bakllava", "moondream", "qwen2.5vl", "minicpm-v", "gemma3",
)

CLOUD_MODELS: dict = {
    "anthropic": os.getenv("VISION_MODEL_ANTHROPIC", "claude-sonnet-5"),
    "openai":    os.getenv("VISION_MODEL_OPENAI", "gpt-4o-mini"),
    "google":    os.getenv("VISION_MODEL_GOOGLE", "gemini-2.0-flash"),
}

DEFAULT_QUESTION = "Describe what is on this screen."


@dataclass
class VisionResult:
    text:      str
    provider:  str
    model:     str
    local:     bool          # did the image stay on this machine
    elapsed_s: float
    error:     str = ""

    def to_dict(self) -> dict:
        return {
            "text": self.text, "provider": self.provider, "model": self.model,
            "local": self.local, "elapsed_s": round(self.elapsed_s, 2),
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def local_vision_model() -> str | None:
    """Name of an installed Ollama model that can see, if any."""
    try:
        r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=3.0)
        if r.status_code != 200:
            return None
        for m in r.json().get("models", []):
            name = m.get("name", "")
            if any(name.startswith(v) for v in LOCAL_VISION_MODELS):
                return name
    except Exception:
        pass
    return None


def _cloud_provider() -> tuple[str, str] | None:
    """First cloud provider with a key configured, as (provider, model)."""
    for provider, env in (
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("google", "GOOGLE_API_KEY"),
    ):
        if os.getenv(env, "").strip():
            return provider, CLOUD_MODELS[provider]
    return None


def status() -> dict:
    """What the vision path can do right now — surfaced by /screen/status."""
    local = local_vision_model()
    cloud = _cloud_provider()
    return {
        "available":     bool(local or cloud),
        "local_model":   local,
        "cloud_provider": cloud[0] if cloud else None,
        "preferred":     "ollama" if local else (cloud[0] if cloud else None),
        "stays_local":   bool(local),
        "configured":    VISION_PROVIDER,
        "hint": None if local else (
            "No local vision model. `ollama pull llama3.2-vision` keeps screenshots "
            "on this machine; otherwise they are sent to a cloud provider."
        ),
    }


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

async def _ask_ollama(model: str, image: bytes, question: str) -> str:
    payload = {
        "model": model,
        "prompt": question,
        "images": [base64.b64encode(image).decode()],
        "stream": False,
    }
    async with httpx.AsyncClient(timeout=VISION_TIMEOUT) as client:
        r = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
        r.raise_for_status()
        return (r.json().get("response") or "").strip()


async def _ask_anthropic(model: str, image: bytes, question: str) -> str:
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
    msg = await client.messages.create(
        model=model,
        max_tokens=600,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg",
                "data": base64.b64encode(image).decode()}},
            {"type": "text", "text": question},
        ]}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


async def _ask_openai(model: str, image: bytes, question: str) -> str:
    import openai

    client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))
    resp = await client.chat.completions.create(
        model=model,
        max_tokens=600,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{base64.b64encode(image).decode()}"}},
        ]}],
    )
    return (resp.choices[0].message.content or "").strip()


async def _ask_google(model: str, image: bytes, question: str) -> str:
    import google.generativeai as genai

    genai.configure(api_key=os.getenv("GOOGLE_API_KEY", ""))
    gm = genai.GenerativeModel(model)
    resp = await gm.generate_content_async(
        [{"mime_type": "image/jpeg", "data": image}, question]
    )
    return (resp.text or "").strip()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def describe(
    image: bytes,
    question: str = DEFAULT_QUESTION,
    provider: str | None = None,
    spoken: bool = False,
) -> VisionResult:
    """
    Describe an image. Prefers a local model so the screenshot stays put.

    `spoken` asks for an answer short enough to listen to, the same constraint
    the voice persona applies to text answers.
    """
    started = time.perf_counter()
    provider = (provider or VISION_PROVIDER or "auto").lower()

    if spoken:
        question = (
            f"{question}\n\nAnswer in at most two short sentences of plain spoken "
            "English. No markdown or lists — this will be read aloud."
        )

    local = local_vision_model()
    if provider in ("auto", "ollama") and local:
        chosen, model, is_local = "ollama", local, True
    elif provider in ("auto", "ollama"):
        cloud = _cloud_provider()
        if not cloud:
            return VisionResult("", "none", "", False, time.perf_counter() - started,
                                error="No vision model available. Pull a local one with "
                                      "`ollama pull llama3.2-vision`, or set an API key.")
        chosen, model, is_local = cloud[0], cloud[1], False
    else:
        if provider not in CLOUD_MODELS:
            return VisionResult("", provider, "", False, time.perf_counter() - started,
                                error=f"Unknown vision provider {provider!r}")
        chosen, model, is_local = provider, CLOUD_MODELS[provider], False

    if not is_local:
        # Sending a screenshot off the machine is a privacy event, not an
        # implementation detail. Record it.
        try:
            from common.security import audit_log

            audit_log("vision.cloud_upload",
                      {"provider": chosen, "model": model, "bytes": len(image)})
        except Exception:
            pass

    fn = {"ollama": _ask_ollama, "anthropic": _ask_anthropic,
          "openai": _ask_openai, "google": _ask_google}[chosen]
    try:
        text = await fn(model, image, question)
    except Exception as exc:  # noqa: BLE001 — reported, never raised at the caller
        return VisionResult("", chosen, model, is_local, time.perf_counter() - started,
                            error=f"{type(exc).__name__}: {exc}"[:300])

    return VisionResult(text, chosen, model, is_local, time.perf_counter() - started)
