"""Image generation worker."""

from __future__ import annotations

import httpx

from .common import prompt_from, register_asset, select_tool, write_plan
from .contracts import GenerationJob, GenerationResult


def _output_path(payload: dict) -> str | None:
    for key in ("path", "output_path", "file", "image_path"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def generate(job: GenerationJob) -> GenerationResult:
    tool, blocked = select_tool(job)
    if blocked:
        return blocked
    assert tool is not None

    prompt = prompt_from(job)
    if not prompt:
        return GenerationResult.blocked(job, tool=tool["name"], reason="image generation needs a prompt")

    payload = {
        "prompt": prompt,
        "brief": job.brief,
        "job_id": job.job_id,
        "production_id": job.production_id,
    }
    try:
        endpoint = str(tool.get("endpoint") or "").rstrip("/")
        route = str(job.brief.get("route") or "/prompt")
        response = httpx.post(f"{endpoint}{route}", json=payload, timeout=float(job.meta.get("timeout", 30)))
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        write_plan(job, tool, {"request": payload, "blocked_reason": str(exc)})
        return GenerationResult.blocked(
            job,
            tool=tool["name"],
            reason=f"image worker did not return an output: {exc}",
        )

    path = _output_path(data)
    if not path:
        plan_path = write_plan(job, tool, {"request": payload, "response": data})
        return GenerationResult.completed(
            job,
            tool=tool["name"],
            path=str(plan_path),
            license_status="pending_review",
            prompt=prompt,
            meta={"queued": True, "response": data},
        )

    asset_id = register_asset(job, path=path, type_="image", tool=tool, prompt=prompt, meta={"response": data})
    return GenerationResult.completed(
        job,
        tool=tool["name"],
        asset_id=asset_id,
        path=path,
        license_status="approved" if tool.get("commercial_safe") else "pending_review",
        prompt=prompt,
        meta={"response": data},
    )
