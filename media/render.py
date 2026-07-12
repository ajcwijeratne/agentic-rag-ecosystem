"""Render productions with Remotion and register derived assets."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from media import registry

MEDIA_DERIVED_ROOT = Path(os.getenv("MEDIA_DERIVED_ROOT", "media_derived"))
REMOTION_DIR = Path(os.getenv("REMOTION_DIR", "my-video"))
TEMPLATES = (
    "linkedin_short",
    "explainer_carousel",
    "talking_head_clip",
    "policy_briefing",
    "course_teaser",
    "proposal_walkthrough",
)


def _flatten_text(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_text(item))
        return out
    if isinstance(value, dict):
        out = []
        for item in value.values():
            out.extend(_flatten_text(item))
        return out
    return [str(value).strip()] if str(value).strip() else []


def _scenes(asset_plan: dict[str, Any]) -> list[dict[str, Any]]:
    raw = asset_plan.get("scenes") if isinstance(asset_plan, dict) else []
    if not isinstance(raw, list):
        return []
    scenes = []
    for i, scene in enumerate(raw, start=1):
        if not isinstance(scene, dict):
            scenes.append({"scene_id": f"scene-{i}", "title": str(scene), "text": str(scene)})
            continue
        scenes.append({
            "scene_id": scene.get("scene_id") or scene.get("id") or f"scene-{i}",
            "title": scene.get("title") or scene.get("heading") or f"Scene {i}",
            "text": scene.get("text") or scene.get("description") or scene.get("beat") or "",
            "asset_id": scene.get("asset_id") or scene.get("source_asset_id") or "",
            "thumbnail_path": scene.get("thumbnail_path") or scene.get("image_path") or "",
            "needs_generation": bool(scene.get("needs_generation")),
        })
    return scenes


def build_props(production: dict[str, Any]) -> dict[str, Any]:
    """Build the stable Remotion props contract from a production record."""
    script = production.get("script") or {}
    asset_plan = production.get("asset_plan") or {}
    edit_plan = production.get("edit_plan") or {}
    lines = _flatten_text(script)
    captions = edit_plan.get("captions") if isinstance(edit_plan, dict) else []
    if not isinstance(captions, list):
        captions = _flatten_text(captions)
    scene_list = _scenes(asset_plan if isinstance(asset_plan, dict) else {})
    return {
        "production_id": production.get("production_id"),
        "title": production.get("title"),
        "project": production.get("project"),
        "format": production.get("format"),
        "owner": production.get("owner"),
        "script": script,
        "asset_plan": asset_plan,
        "edit_plan": edit_plan,
        "lines": lines[:8],
        "captions": captions[:10],
        "scenes": scene_list[:12],
        "linked_assets": production.get("linked_assets") or [],
        "brand": {
            "name": "WijerCo",
            "pine": "#1f4d3f",
            "ink": "#14231f",
            "gold": "#b88724",
            "paper": "#f7f4ee",
        },
    }


def _write_props(production_id: str, props: dict[str, Any]) -> Path:
    target = MEDIA_DERIVED_ROOT / production_id / "props.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(props, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def _register(production_id: str, path: Path, template: str, meta: dict[str, Any]) -> str:
    asset_id = registry.add_asset(
        "video",
        str(path),
        "derived",
        rights="owned",
        status="ready",
        project=meta.get("project"),
        tags=["production", template],
        meta={"production_id": production_id, "template": template, **meta},
    )
    for source_id in meta.get("source_assets") or []:
        try:
            registry.add_link(asset_id, source_id, "derived_from")
        except Exception:
            pass
    return asset_id


def render(production_id: str, template: str, props: dict[str, Any]) -> dict[str, Any]:
    """Render a production, or create a prepared placeholder when Remotion is absent."""
    if template not in TEMPLATES:
        raise ValueError(f"template must be one of {TEMPLATES}")
    MEDIA_DERIVED_ROOT.mkdir(parents=True, exist_ok=True)
    props_path = _write_props(production_id, props)
    out_path = MEDIA_DERIVED_ROOT / production_id / f"{template}.mp4"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    node = shutil.which("npx")
    if node and (REMOTION_DIR / "package.json").is_file():
        cmd = [
            node,
            "remotion",
            "render",
            "src/index.ts",
            template,
            str(out_path.resolve()),
            "--props",
            str(props_path.resolve()),
        ]
        result = subprocess.run(
            cmd,
            cwd=str(REMOTION_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if result.returncode == 0 and out_path.exists():
            asset_id = _register(
                production_id,
                out_path,
                template,
                {"source_assets": props.get("linked_assets") or [], "rendered": True},
            )
            return {"status": "rendered", "asset_id": asset_id, "path": str(out_path)}
        meta = {"stderr": (result.stderr or "")[-2000:], "stdout": (result.stdout or "")[-1000:]}
    else:
        meta = {"reason": "Remotion or npx not available"}

    placeholder = MEDIA_DERIVED_ROOT / production_id / f"{template}.render-plan.json"
    placeholder.write_text(json.dumps({"props": props, "meta": meta}, ensure_ascii=False, indent=2), encoding="utf-8")
    asset_id = _register(
        production_id,
        placeholder,
        template,
        {"source_assets": props.get("linked_assets") or [], "rendered": False, **meta},
    )
    return {"status": "prepared", "asset_id": asset_id, "path": str(placeholder), **meta}
