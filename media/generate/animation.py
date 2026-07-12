"""Animation generation worker for Manim and Blender handoffs."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .common import derived_path, prompt_from, register_asset, select_tool, write_plan
from .contracts import GenerationJob, GenerationResult


def _run_manim(job: GenerationJob, tool: dict) -> GenerationResult:
    scene_file = job.brief.get("scene_file")
    scene_name = job.brief.get("scene_name")
    if not scene_file or not scene_name:
        plan_path = write_plan(job, tool, {"brief": job.brief, "prompt": prompt_from(job)})
        return GenerationResult.completed(
            job,
            tool=tool["name"],
            path=str(plan_path),
            license_status="approved",
            prompt=prompt_from(job),
            meta={"prepared": True, "reason": "Manim needs scene_file and scene_name to render"},
        )

    out_name = str(job.brief.get("filename") or f"animation-{job.job_id}.mp4")
    out_path = derived_path(job, out_name)
    result = subprocess.run(
        [str(tool["command"]), "-ql", str(scene_file), str(scene_name), "-o", out_path.name],
        cwd=str(Path(out_path).parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(job.meta.get("timeout", 180)),
    )
    if result.returncode != 0:
        return GenerationResult.failed(job, tool=tool["name"], error=(result.stderr or "")[-1000:])
    asset_id = register_asset(job, path=out_path, type_="video", tool=tool, prompt=prompt_from(job))
    return GenerationResult.completed(job, tool=tool["name"], asset_id=asset_id, path=str(out_path), license_status="approved")


def _run_blender(job: GenerationJob, tool: dict) -> GenerationResult:
    scene_file = job.brief.get("scene_file")
    if not scene_file:
        plan_path = write_plan(job, tool, {"brief": job.brief, "prompt": prompt_from(job)})
        return GenerationResult.completed(
            job,
            tool=tool["name"],
            path=str(plan_path),
            license_status="approved",
            prompt=prompt_from(job),
            meta={"prepared": True, "reason": "Blender needs scene_file to render"},
        )

    out_path = derived_path(job, str(job.brief.get("filename") or f"animation-{job.job_id}.mp4"))
    result = subprocess.run(
        [str(tool["command"]), "--background", "--python", str(scene_file), "--", str(out_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(job.meta.get("timeout", 300)),
    )
    if result.returncode != 0:
        return GenerationResult.failed(job, tool=tool["name"], error=(result.stderr or "")[-1000:])
    asset_id = register_asset(job, path=out_path, type_="video", tool=tool, prompt=prompt_from(job))
    return GenerationResult.completed(job, tool=tool["name"], asset_id=asset_id, path=str(out_path), license_status="approved")


def generate(job: GenerationJob) -> GenerationResult:
    tool, blocked = select_tool(job)
    if blocked:
        return blocked
    assert tool is not None
    if tool["name"] == "blender":
        return _run_blender(job, tool)
    return _run_manim(job, tool)
