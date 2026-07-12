"""Extract generation jobs from Content Studio production plans."""

from __future__ import annotations

from typing import Any

CAPABILITY_ALIASES = {
    "visual": "image",
    "image": "image",
    "images": "image",
    "voice": "voice",
    "audio": "voice",
    "narration": "voice",
    "avatar": "avatar",
    "talking_head": "avatar",
    "animation": "animation",
    "motion": "animation",
    "video": "video",
}


def _normalise_capability(value: Any) -> str | None:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return CAPABILITY_ALIASES.get(key)


def _prompt_from(item: dict[str, Any]) -> str:
    for key in ("prompt", "visual_prompt", "image_prompt", "brief", "description", "text", "script"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _coerce_brief(item: dict[str, Any], capability: str, scene: dict[str, Any] | None = None) -> dict[str, Any]:
    brief = dict(item.get("brief") if isinstance(item.get("brief"), dict) else item)
    prompt = _prompt_from(brief) or (_prompt_from(scene) if scene else "")
    if prompt:
        brief.setdefault("prompt" if capability != "voice" else "text", prompt)
    if scene:
        for key in ("scene_id", "scene", "shot", "duration", "aspect_ratio"):
            if key in scene and key not in brief:
                brief[key] = scene[key]
    return brief


def _job(
    production: dict[str, Any],
    capability: str,
    brief: dict[str, Any],
    *,
    tool: str | None = None,
    rights: str = "owned",
) -> dict[str, Any]:
    return {
        "capability": capability,
        "brief": brief,
        "production_id": production.get("production_id"),
        "tool": tool,
        "source_assets": list(production.get("linked_assets") or []),
        "rights": rights,
        "meta": {"project": production.get("project")},
    }


def _items(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _from_explicit_lists(production: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for key, capability in (
        ("generation_briefs", None),
        ("generated_assets", None),
        ("images", "image"),
        ("visuals", "image"),
        ("voice", "voice"),
        ("narration", "voice"),
        ("avatars", "avatar"),
        ("animations", "animation"),
        ("videos", "video"),
    ):
        for item in _items(plan.get(key)):
            cap = capability or _normalise_capability(item.get("capability") or item.get("type"))
            if not cap:
                continue
            jobs.append(_job(
                production,
                cap,
                _coerce_brief(item, cap),
                tool=item.get("tool"),
                rights=item.get("rights") or "owned",
            ))
    return jobs


def _from_scenes(production: dict[str, Any], plan: dict[str, Any]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for scene in _items(plan.get("scenes") or plan.get("storyboard") or plan.get("shots")):
        if scene.get("asset_id") or scene.get("use_asset_id"):
            continue
        scene_needs_generation = bool(scene.get("needs_generation") or scene.get("generate"))

        for key, capability in (
            ("generation_brief", None),
            ("visual_brief", "image"),
            ("image_brief", "image"),
            ("voice_brief", "voice"),
            ("avatar_brief", "avatar"),
            ("animation_brief", "animation"),
            ("video_brief", "video"),
        ):
            value = scene.get(key)
            if not isinstance(value, dict):
                continue
            cap = capability or _normalise_capability(value.get("capability") or value.get("type")) or "image"
            jobs.append(_job(
                production,
                cap,
                _coerce_brief(value, cap, scene),
                tool=value.get("tool") or scene.get("tool"),
                rights=value.get("rights") or scene.get("rights") or "owned",
            ))

        if scene_needs_generation and not any(
            key in scene for key in ("generation_brief", "visual_brief", "image_brief", "voice_brief", "avatar_brief", "animation_brief", "video_brief")
        ):
            cap = _normalise_capability(scene.get("capability") or scene.get("type")) or "image"
            jobs.append(_job(
                production,
                cap,
                _coerce_brief(scene, cap, scene),
                tool=scene.get("tool"),
                rights=scene.get("rights") or "owned",
            ))
    return jobs


def extract_generation_jobs(
    production: dict[str, Any],
    *,
    capabilities: list[str] | None = None,
    include_video: bool = False,
) -> list[dict[str, Any]]:
    """Return generation jobs implied by a production's asset plan."""
    plan = production.get("asset_plan") or {}
    if not isinstance(plan, dict):
        return []

    allowed = {_normalise_capability(item) for item in capabilities or []}
    allowed.discard(None)
    jobs = [*_from_explicit_lists(production, plan), *_from_scenes(production, plan)]
    if include_video:
        jobs.append(_job(
            production,
            "video",
            {
                "template": production.get("format"),
                "props": {
                    "title": production.get("title"),
                    "format": production.get("format"),
                    "script": production.get("script") or {},
                    "asset_plan": plan,
                    "edit_plan": production.get("edit_plan") or {},
                    "linked_assets": list(production.get("linked_assets") or []),
                },
            },
        ))
    if allowed:
        jobs = [job for job in jobs if job["capability"] in allowed]
    return jobs
