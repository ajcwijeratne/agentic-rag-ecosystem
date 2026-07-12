"""Video generation worker."""

from __future__ import annotations

from typing import Any

from media import render as render_service

from .common import select_tool
from .contracts import GenerationJob, GenerationResult


def generate(job: GenerationJob) -> GenerationResult:
    tool, blocked = select_tool(job)
    if blocked:
        return blocked
    assert tool is not None
    if not job.production_id:
        return GenerationResult.blocked(job, tool=tool["name"], reason="video generation needs production_id")

    template = str(job.brief.get("template") or job.brief.get("format") or "")
    if not template:
        return GenerationResult.blocked(job, tool=tool["name"], reason="video generation needs template or format")

    props: dict[str, Any] = dict(job.brief.get("props") or {})
    props.setdefault("linked_assets", job.source_assets)
    result = render_service.render(job.production_id, template, props)
    return GenerationResult.completed(
        job,
        tool=tool["name"],
        asset_id=result.get("asset_id"),
        path=result.get("path"),
        license_status="approved" if tool.get("commercial_safe") else "pending_review",
        meta=result,
    )
