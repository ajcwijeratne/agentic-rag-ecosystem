"""Shared helpers for multimedia generation workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from media import registry, tool_registry

from .contracts import GenerationJob, GenerationResult

MEDIA_DERIVED_ROOT = Path(os.getenv("MEDIA_DERIVED_ROOT", "media_derived"))


def select_tool(job: GenerationJob) -> tuple[dict[str, Any] | None, GenerationResult | None]:
    try:
        tool = tool_registry.require_tool(
            job.capability,
            tool_name=job.tool,
            require_commercial_safe=job.meta.get("require_commercial_safe", False),
        )
    except Exception as exc:
        return None, GenerationResult.blocked(job, tool=job.tool or job.capability, reason=str(exc))
    if not tool.get("available"):
        return None, GenerationResult.blocked(
            job,
            tool=tool["name"],
            reason=f"media tool {tool['name']!r} is enabled but not available on this machine",
        )
    return tool, None


def derived_path(job: GenerationJob, filename: str) -> Path:
    root = MEDIA_DERIVED_ROOT / (job.production_id or job.job_id)
    root.mkdir(parents=True, exist_ok=True)
    return root / filename


def write_plan(job: GenerationJob, tool: dict[str, Any], payload: dict[str, Any]) -> Path:
    path = derived_path(job, f"{job.capability}-{job.job_id}.plan.json")
    path.write_text(
        json.dumps({"job": job.to_dict(), "tool": tool, "payload": payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def register_asset(
    job: GenerationJob,
    *,
    path: str | Path,
    type_: str,
    tool: dict[str, Any],
    prompt: str | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    governance_meta = {
        "generated": True,
        "generation_capability": job.capability,
        "review_status": "pending",
        "gate": "generated_image",
    }
    asset_id = registry.add_asset(
        type_,
        str(path),
        "derived",
        rights=job.rights,
        status="ready",
        project=job.meta.get("project"),
        tags=["generated", job.capability, tool["name"]],
        meta={
            "job_id": job.job_id,
            "production_id": job.production_id,
            "tool": tool["name"],
            "license": tool.get("license"),
            "commercial_safe": tool.get("commercial_safe"),
            "prompt": prompt,
            "governance": governance_meta,
            **(meta or {}),
        },
    )
    for source_id in job.source_assets:
        try:
            registry.add_link(asset_id, source_id, "derived_from")
        except Exception:
            pass
    return asset_id


def prompt_from(job: GenerationJob) -> str:
    return str(job.brief.get("prompt") or job.brief.get("text") or job.brief.get("script") or "")
