"""Dispatch multimedia generation jobs to local workers."""

from __future__ import annotations

from . import animation, avatar, image, video, voice
from .contracts import GenerationJob, GenerationResult

_WORKERS = {
    "video": video.generate,
    "image": image.generate,
    "voice": voice.generate,
    "avatar": avatar.generate,
    "animation": animation.generate,
}


def generate(job: GenerationJob | dict) -> GenerationResult:
    import time

    loaded = GenerationJob.from_dict(job) if isinstance(job, dict) else job
    worker = _WORKERS[loaded.capability]
    started = time.time()
    result = worker(loaded)
    elapsed = time.time() - started
    _record_cost(loaded, result, elapsed)
    return result


def _record_cost(job: GenerationJob, result: GenerationResult, elapsed: float) -> None:
    """Cost every completed media job into the shared ledger. Never raises."""
    if getattr(result, "status", None) != "completed":
        return
    try:
        from orchestrator import cost_tracker

        meta = result.meta or {}
        response = meta.get("response") if isinstance(meta.get("response"), dict) else {}
        reported = meta.get("cost_usd") or response.get("cost_usd")
        cost_tracker.record_media(
            capability=job.capability,
            provider=result.provider,
            tool=result.tool,
            seconds=elapsed,
            cost_usd=float(reported) if reported is not None else None,
            query=str(job.brief.get("text") or job.production_id or job.capability),
        )
    except Exception:
        pass


def generate_dict(job: GenerationJob | dict) -> dict:
    return generate(job).to_dict()
