"""Obsidian Projects integration for operating plans.

SQLite remains the operational source of truth. Obsidian receives a readable
project note that links back to the planner plan_id and can be reviewed by a
human without opening the Command Centre.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.security import audit_log, backup_file

from . import operating


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def vault_path() -> Path | None:
    raw = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def projects_root() -> Path | None:
    vault = vault_path()
    if not vault:
        return None
    rel = os.getenv("OBSIDIAN_PROJECTS_PATH", "Projects").strip() or "Projects"
    root = (vault / rel).resolve()
    try:
        root.relative_to(vault)
    except ValueError:
        raise ValueError("OBSIDIAN_PROJECTS_PATH must stay inside OBSIDIAN_VAULT_PATH")
    return root


def status() -> dict[str, Any]:
    vault = vault_path()
    root = projects_root() if vault else None
    return {
        "configured": bool(vault),
        "vault_path": str(vault) if vault else None,
        "vault_exists": vault.is_dir() if vault else False,
        "projects_path": str(root) if root else None,
        "projects_exists": root.is_dir() if root else False,
    }


def sync_plan(plan_id: str, *, overwrite: bool = True) -> dict[str, Any]:
    root = projects_root()
    if not root:
        raise ValueError("OBSIDIAN_VAULT_PATH is not configured")
    plan = operating.get_plan(plan_id)
    if not plan:
        raise KeyError("plan not found")
    root.mkdir(parents=True, exist_ok=True)
    path = _path_for_plan(root, plan)
    existed = path.exists()
    if existed and not overwrite:
        return {"status": "exists", "path": str(path), "plan_id": plan_id}
    if existed:
        backup_file(path)

    markdown = render_plan_markdown(plan)
    path.write_text(markdown, encoding="utf-8")
    _record_obsidian_link(plan, path)
    audit_log("obsidian.plan_sync", {"plan_id": plan_id, "path": str(path), "overwrite": overwrite})
    return {
        "status": "ok",
        "path": str(path),
        "plan_id": plan_id,
        "created": not existed,
        "updated_at": _now(),
    }


def import_project_notes(project: str, *, limit: int = 20) -> dict[str, Any]:
    root = projects_root()
    if not root:
        raise ValueError("OBSIDIAN_VAULT_PATH is not configured")
    if not root.is_dir():
        return {"items": [], "imported": 0, "projects_path": str(root)}
    imported = []
    for path in sorted(root.rglob("*.md"))[: max(1, limit)]:
        text = path.read_text(encoding="utf-8", errors="ignore")
        frontmatter = _frontmatter(text)
        if project and project.lower() not in {
            str(frontmatter.get("project", "")).lower(),
            path.stem.lower(),
            path.parent.name.lower(),
        }:
            continue
        content = _extract_memory_content(text)
        if not content:
            continue
        memory_id = operating.add_project_memory(
            project or str(frontmatter.get("project") or path.stem),
            content,
            source="obsidian",
            meta={"obsidian": {"path": str(path), "imported_at": _now()}},
        )
        imported.append({"memory_id": memory_id, "path": str(path)})
    audit_log("obsidian.project_import", {"project": project, "imported": len(imported)})
    return {"items": imported, "imported": len(imported), "projects_path": str(root)}


def render_plan_markdown(plan: dict[str, Any]) -> str:
    meta = plan.get("meta") or {}
    planner = meta.get("planner") or {}
    next_action = operating.recommend_next_action(plan_id=plan["plan_id"]).get("task")
    lines = [
        "---",
        "type: project",
        f"planner_plan_id: {plan['plan_id']}",
        f"project: {_yaml_value(plan.get('project') or 'General')}",
        f"workflow: {_yaml_value(planner.get('workflow') or 'general')}",
        f"status: {_yaml_value(plan.get('status') or 'active')}",
        f"owner: {_yaml_value(plan.get('owner') or '')}",
        f"next_action: {_yaml_value((next_action or {}).get('title') or '')}",
        f"updated: {_now()}",
        "---",
        "",
        f"# {plan.get('title') or 'Operating Plan'}",
        "",
        f"Goal: {plan.get('goal') or plan.get('title') or ''}",
        "",
        "## Planner",
        "",
        f"- Workflow: {planner.get('workflow') or 'general'}",
        f"- Confidence: {planner.get('confidence', '')}",
        f"- Rationale: {planner.get('reason', '')}",
        f"- Risk flags: {', '.join(planner.get('risk_flags') or []) or 'none'}",
        "",
        "## Next Action",
        "",
        _task_line(next_action) if next_action else "No unblocked next action.",
        "",
        "## Tasks",
        "",
    ]
    for task in plan.get("tasks") or []:
        lines.append(_task_line(task))
        criteria = ((task.get("meta") or {}).get("planner") or {}).get("success_criteria") or []
        for item in criteria:
            lines.append(f"  - success: {item}")
    lines.extend([
        "",
        "## Context",
        "",
        "",
        "## Decisions",
        "",
        "",
        "## Risks",
        "",
    ])
    return "\n".join(lines).rstrip() + "\n"


def _record_obsidian_link(plan: dict[str, Any], path: Path) -> None:
    meta = plan.get("meta") or {}
    meta["obsidian"] = {"path": str(path), "synced_at": _now()}
    operating.update_plan(plan["plan_id"], meta=meta)


def _path_for_plan(root: Path, plan: dict[str, Any]) -> Path:
    project = _slug(plan.get("project") or "General")
    title = _slug(plan.get("title") or plan["plan_id"])
    path = (root / project / f"{title}.md").resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("computed Obsidian path escaped Projects root") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _slug(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._ -]+", "", str(value)).strip()
    clean = re.sub(r"\s+", " ", clean)
    return clean[:96] or "Untitled"


def _task_line(task: dict[str, Any] | None) -> str:
    if not task:
        return ""
    checked = "x" if task.get("status") == "done" else " "
    planner = (task.get("meta") or {}).get("planner") or {}
    seq = planner.get("sequence")
    prefix = f"{seq}. " if seq else ""
    return f"- [{checked}] {prefix}{task.get('title')} `{task.get('status')}` priority:{task.get('priority')}"


def _yaml_value(value: Any) -> str:
    text = str(value or "").replace('"', '\\"')
    return f'"{text}"'


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    data = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def _extract_memory_content(text: str) -> str:
    headings = ("## Context", "## Decisions", "## Risks", "## Client Preferences")
    chunks = []
    for heading in headings:
        pattern = re.compile(rf"{re.escape(heading)}\s*\n(.*?)(?=\n## |\Z)", re.S)
        match = pattern.search(text)
        if match:
            body = match.group(1).strip()
            if body:
                chunks.append(f"{heading}\n{body}")
    return "\n\n".join(chunks)[:4000]
