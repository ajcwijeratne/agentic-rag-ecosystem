"""Generation orchestration for Content Studio productions."""

from __future__ import annotations

from typing import Any

from media.generate import generate_dict
from media.generate.plan import extract_generation_jobs

from . import production as production_store


def _link_result(production: dict[str, Any], result: dict[str, Any], actor: str) -> None:
    asset_id = result.get("asset_id")
    if not asset_id:
        return
    production_id = production["production_id"]
    linked = list(production.get("linked_assets") or [])
    if asset_id not in linked:
        linked.append(asset_id)
        production_store.update_production(production_id, linked_assets=linked)
        production["linked_assets"] = linked
    production_store.record_event(
        production_id,
        production.get("state"),
        production.get("state"),
        actor,
        f"generated {result.get('capability')}: {result.get('status')}",
    )


def generate_one_for_production(
    production_id: str,
    *,
    capability: str,
    brief: dict[str, Any] | None = None,
    tool: str | None = None,
    source_assets: list[str] | None = None,
    rights: str = "owned",
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    production = production_store.get_production(production_id)
    if not production:
        raise KeyError("production not found")

    job_brief = dict(brief or {})
    sources = source_assets if source_assets is not None else list(production.get("linked_assets") or [])
    if capability == "video":
        job_brief.setdefault("template", production.get("format"))
        job_brief.setdefault("props", {
            "title": production.get("title"),
            "format": production.get("format"),
            "script": production.get("script") or {},
            "asset_plan": production.get("asset_plan") or {},
            "edit_plan": production.get("edit_plan") or {},
            "linked_assets": sources,
        })

    job_meta = {"project": production.get("project"), **(meta or {})}
    result = generate_dict({
        "capability": capability,
        "brief": job_brief,
        "production_id": production_id,
        "tool": tool,
        "source_assets": sources,
        "rights": rights,
        "meta": job_meta,
    })
    _link_result(production, result, str(job_meta.get("actor") or "operator"))
    return {"generation": result, "production": production_store.get_production(production_id)}


def generate_plan_for_production(
    production_id: str,
    *,
    capabilities: list[str] | None = None,
    include_video: bool = False,
    max_jobs: int = 20,
    dry_run: bool = False,
    actor: str = "operator",
) -> dict[str, Any]:
    production = production_store.get_production(production_id)
    if not production:
        raise KeyError("production not found")

    jobs = extract_generation_jobs(production, capabilities=capabilities, include_video=include_video)
    jobs = jobs[: max(0, max_jobs)]
    if dry_run:
        return {"jobs": jobs, "results": [], "production": production}

    results = []
    for job in jobs:
        job.setdefault("meta", {})
        job["meta"] = {"project": production.get("project"), **job["meta"], "actor": actor}
        result = generate_dict(job)
        results.append(result)
        _link_result(production, result, actor)

    return {
        "jobs": jobs,
        "results": results,
        "production": production_store.get_production(production_id),
    }
