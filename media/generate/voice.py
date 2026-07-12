"""Voice generation worker."""

from __future__ import annotations

import subprocess

from .common import derived_path, prompt_from, register_asset, select_tool, write_plan
from .contracts import GenerationJob, GenerationResult


def generate(job: GenerationJob) -> GenerationResult:
    tool, blocked = select_tool(job)
    if blocked:
        return blocked
    assert tool is not None

    text = prompt_from(job)
    if not text:
        return GenerationResult.blocked(job, tool=tool["name"], reason="voice generation needs text")

    if tool["name"] == "elevenlabs":
        from media.providers import elevenlabs

        out_path = derived_path(job, str(job.brief.get("filename") or f"voice-{job.job_id}.mp3"))
        try:
            result = elevenlabs.synthesize(
                text,
                out_path,
                voice_id=job.brief.get("voice_id"),
                timeout=float(job.meta.get("timeout", 120)),
            )
        except Exception as exc:
            return GenerationResult.failed(job, tool=tool["name"], error=str(exc)[:1000])
        asset_id = register_asset(
            job,
            path=result["path"],
            type_="audio",
            tool=tool,
            prompt=text,
            meta={"clone": True, "voice_id": result.get("voice_id"), "provider": "elevenlabs"},
        )
        return GenerationResult.completed(
            job,
            tool=tool["name"],
            asset_id=asset_id,
            path=result["path"],
            license_status="pending_review",
            prompt=text,
            meta={"clone": True, "voice_id": result.get("voice_id")},
        )

    if tool["kind"] == "http":
        import httpx

        endpoint = str(tool.get("endpoint") or "").rstrip("/")
        out_name = str(job.brief.get("filename") or f"voice-{job.job_id}.wav")
        payload = {
            "text": text,
            "filename": out_name,
            "brief": job.brief,
            "job_id": job.job_id,
            "production_id": job.production_id,
        }
        try:
            response = httpx.post(f"{endpoint}/generate", json=payload,
                                  timeout=float(job.meta.get("timeout", 600)))
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            return GenerationResult.failed(job, tool=tool["name"], error=str(exc)[:1000])
        path = data.get("path")
        if not path:
            return GenerationResult.failed(job, tool=tool["name"],
                                           error=f"voice worker returned no path: {str(data)[:300]}")
        is_clone = bool(data.get("clone", True))
        asset_id = register_asset(
            job,
            path=path,
            type_="audio",
            tool=tool,
            prompt=text,
            meta={"clone": is_clone, "provider": tool["name"], "engine": data.get("engine")},
        )
        return GenerationResult.completed(
            job,
            tool=tool["name"],
            asset_id=asset_id,
            path=path,
            license_status="pending_review",
            prompt=text,
            meta={"clone": is_clone, "engine": data.get("engine")},
        )

    if tool["kind"] != "cli" or tool["name"] != "piper":
        plan_path = write_plan(job, tool, {"text": text, "brief": job.brief})
        return GenerationResult.completed(
            job,
            tool=tool["name"],
            path=str(plan_path),
            license_status="pending_review",
            prompt=text,
            meta={"prepared": True},
        )

    model = job.brief.get("voice_model") or job.meta.get("voice_model")
    if not model:
        return GenerationResult.blocked(job, tool=tool["name"], reason="Piper voice generation needs voice_model")

    out_path = derived_path(job, str(job.brief.get("filename") or f"voice-{job.job_id}.wav"))
    result = subprocess.run(
        [str(tool["command"]), "--model", str(model), "--output_file", str(out_path)],
        input=text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(job.meta.get("timeout", 120)),
    )
    if result.returncode != 0:
        return GenerationResult.failed(job, tool=tool["name"], error=(result.stderr or "")[-1000:])

    asset_id = register_asset(job, path=out_path, type_="audio", tool=tool, prompt=text)
    return GenerationResult.completed(
        job,
        tool=tool["name"],
        asset_id=asset_id,
        path=str(out_path),
        license_status="approved" if tool.get("commercial_safe") else "pending_review",
        prompt=text,
    )
