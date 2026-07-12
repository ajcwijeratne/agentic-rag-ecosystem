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
    loaded = GenerationJob.from_dict(job) if isinstance(job, dict) else job
    worker = _WORKERS[loaded.capability]
    return worker(loaded)


def generate_dict(job: GenerationJob | dict) -> dict:
    return generate(job).to_dict()
