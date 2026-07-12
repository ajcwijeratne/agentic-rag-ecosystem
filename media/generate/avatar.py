"""Avatar and lip-sync generation worker."""

from __future__ import annotations

import httpx

from .common import prompt_from, register_asset, select_tool, write_plan
from .contracts import GenerationJob, GenerationResult


def _output_path(payload: dict) -> str | None:
    for key in ("path", "output_path", "file", "video_path"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def generate(job: GenerationJob) -> GenerationResult:
    tool, blocked = select_tool(job)
    if blocked:
        return blocked
    assert tool is not None

    portrait = job.brief.get("portrait_path")
    audio = job.brief.get("audio_path")
    text = prompt_from(job)

    if tool["name"] == "heygen":
        from .common import derived_path
        from media.providers import heygen

        if not audio and not text:
            return GenerationResult.blocked(job, tool=tool["name"], reason="heygen avatar needs audio_path or text")
        out_path = derived_path(job, str(job.brief.get("filename") or f"avatar-{job.job_id}.mp4"))
        try:
            result = heygen.generate_and_download(
                out_path,
                avatar_id=job.brief.get("avatar_id"),
                audio_path=audio,
                text=None if audio else text,
                poll_timeout=float(job.meta.get("timeout", 900)),
            )
        except Exception as exc:
            return GenerationResult.failed(job, tool=tool["name"], error=str(exc)[:1000])
        asset_id = register_asset(
            job,
            path=result["path"],
            type_="video",
            tool=tool,
            prompt=text,
            meta={"clone": True, "avatar_id": result.get("avatar_id"),
                  "provider": "heygen", "heygen_video_id": result.get("video_id")},
        )
        return GenerationResult.completed(
            job,
            tool=tool["name"],
            asset_id=asset_id,
            path=result["path"],
            license_status="pending_review",
            prompt=text,
            meta={"clone": True, "avatar_id": result.get("avatar_id")},
        )

    if not portrait:
        return GenerationResult.blocked(job, tool=tool["name"], reason="avatar generation needs portrait_path")
    if not audio and not text:
        return GenerationResult.blocked(job, tool=tool["name"], reason="avatar generation needs audio_path or text")

    payload = {
        "portrait_path": portrait,
        "audio_path": audio,
        "text": text,
        "brief": job.brief,
        "job_id": job.job_id,
        "production_id": job.production_id,
    }
    try:
        endpoint = str(tool.get("endpoint") or "").rstrip("/")
        route = str(job.brief.get("route") or "/generate")
        response = httpx.post(f"{endpoint}{route}", json=payload, timeout=float(job.meta.get("timeout", 60)))
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        write_plan(job, tool, {"request": payload, "blocked_reason": str(exc)})
        return GenerationResult.blocked(job, tool=tool["name"], reason=f"avatar worker did not return an output: {exc}")

    path = _output_path(data)
    if not path:
        plan_path = write_plan(job, tool, {"request": payload, "response": data})
        return GenerationResult.completed(
            job,
            tool=tool["name"],
            path=str(plan_path),
            license_status="pending_review",
            prompt=text,
            meta={"queued": True, "response": data},
        )

    is_clone = bool(data.get("clone", True))
    asset_id = register_asset(
        job, path=path, type_="video", tool=tool, prompt=text,
        meta={"response": data, "clone": is_clone, "provider": tool["name"]},
    )
    return GenerationResult.completed(
        job,
        tool=tool["name"],
        asset_id=asset_id,
        path=path,
        license_status="pending_review",
        prompt=text,
        meta={"response": data},
    )
