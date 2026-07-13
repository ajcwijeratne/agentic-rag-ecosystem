"""Persist governed publication handoffs into the Obsidian writing pipeline."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common.security import backup_file

READY_STAGE = "03_Ready"
PUBLISHED_STAGE = "04_Published"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _vault() -> Path:
    raw = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    if not raw:
        raise RuntimeError("OBSIDIAN_VAULT_PATH is not configured")
    vault = Path(raw).expanduser().resolve()
    if not vault.is_dir():
        raise RuntimeError("OBSIDIAN_VAULT_PATH does not exist")
    return vault


def _pipeline(vault: Path) -> Path:
    raw = os.getenv("WRITING_PIPELINE_PATH", "09_Writing Pipline").strip()
    pipeline = (vault / (raw or "09_Writing Pipline")).resolve()
    try:
        pipeline.relative_to(vault)
    except ValueError as exc:
        raise RuntimeError("WRITING_PIPELINE_PATH must stay inside OBSIDIAN_VAULT_PATH") from exc
    return pipeline


def _slug(title: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip().rstrip(".")
    value = re.sub(r"\s+", " ", value)
    return (value or "Untitled publication")[:140]


def _yaml(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value or ""), ensure_ascii=False)


def _frontmatter(fields: dict[str, Any]) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {_yaml(value)}" for key, value in fields.items() if value not in (None, ""))
    lines.append("---")
    return "\n".join(lines)


def _copy(production: dict[str, Any], publication: dict[str, Any]) -> str:
    meta = publication.get("meta") or {}
    copy = str(meta.get("copy") or "").strip()
    if copy:
        return copy
    if publication.get("channel") == "linkedin":
        from .linkedin import _text

        return _text(production.get("script")).strip()
    return ""


def _sources(production: dict[str, Any]) -> list[tuple[str, str]]:
    research = production.get("research") or {}
    items = research.get("sources") if isinstance(research, dict) else []
    out: list[tuple[str, str]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("file") or "Source").strip()
        url = str(item.get("url") or "").strip()
        if title or url:
            out.append((title or url, url))
    return out


def _find_note(pipeline: Path, production_id: str) -> Path | None:
    needles = (f'production_id: "{production_id}"', f"production_id: {production_id}")
    for stage in (READY_STAGE, PUBLISHED_STAGE):
        folder = pipeline / stage
        if not folder.is_dir():
            continue
        for path in folder.glob("*.md"):
            try:
                head = path.read_text(encoding="utf-8")[:5000]
            except Exception:
                continue
            if any(needle in head for needle in needles):
                return path
    return None


def _target_path(pipeline: Path, stage: str, production: dict[str, Any], existing: Path | None) -> Path:
    folder = pipeline / stage
    folder.mkdir(parents=True, exist_ok=True)
    if existing:
        candidate = folder / existing.name
    else:
        candidate = folder / f"{_slug(str(production.get('title') or 'Untitled publication'))}.md"
    if candidate.exists() and (not existing or candidate.resolve() != existing.resolve()):
        short_id = str(production.get("production_id") or "production")[:8]
        candidate = folder / f"{candidate.stem}--{short_id}.md"
    return candidate


def _render(production: dict[str, Any], publication: dict[str, Any], stage: str) -> str:
    title = str(production.get("title") or "Untitled publication")
    status = "Published" if stage == PUBLISHED_STAGE else "Ready"
    fields = {
        "t": title,
        "p": str(publication.get("channel") or "").title(),
        "stage": stage,
        "status": status,
        "production_id": production.get("production_id"),
        "publication_id": publication.get("publication_id"),
        "publication_status": publication.get("status"),
        "project": production.get("project"),
        "format": production.get("format"),
        "owner": production.get("owner"),
        "public_url": publication.get("url"),
        "source": "Production pipeline",
        "updated_at": _now(),
    }
    parts = [_frontmatter(fields), "", f"# {title}"]
    copy = _copy(production, publication)
    if copy:
        parts.extend(["", "## Final Copy", "", copy])
    sources = _sources(production)
    if sources:
        parts.extend(["", "## Sources", ""])
        for source_title, url in sources:
            parts.append(f"- [{source_title}]({url})" if url else f"- {source_title}")
    parts.extend([
        "",
        "## Production Record",
        "",
        f"- Production ID: `{production.get('production_id')}`",
        f"- Publication ID: `{publication.get('publication_id')}`",
        f"- Channel: {publication.get('channel')}",
        f"- Status: {publication.get('status')}",
    ])
    if publication.get("url"):
        parts.append(f"- Public URL: {publication.get('url')}")
    return "\n".join(parts).rstrip() + "\n"


def _sync(production: dict[str, Any], publication: dict[str, Any], stage: str) -> dict[str, Any]:
    vault = _vault()
    pipeline = _pipeline(vault)
    production_id = str(production.get("production_id") or "").strip()
    if not production_id:
        raise ValueError("production has no production_id")
    existing = _find_note(pipeline, production_id)
    if existing and existing.parent.name == PUBLISHED_STAGE and stage == READY_STAGE:
        return {"status": "published", "path": existing.relative_to(vault).as_posix()}
    target = _target_path(pipeline, stage, production, existing)
    text = _render(production, publication, stage)
    if existing and existing.exists():
        backup_file(existing)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, target)
    if existing and existing.resolve() != target.resolve() and existing.exists():
        existing.unlink()
    return {"status": "published" if stage == PUBLISHED_STAGE else "ready", "path": target.relative_to(vault).as_posix()}


def sync_ready(production: dict[str, Any], publication: dict[str, Any]) -> dict[str, Any]:
    """Create or refresh the production note only after a handoff is ready."""
    if publication.get("status") not in {"handoff_ready", "published"}:
        raise ValueError("publication is not ready for Obsidian sync")
    stage = PUBLISHED_STAGE if publication.get("status") == "published" else READY_STAGE
    return _sync(production, publication, stage)


def sync_published(production: dict[str, Any], publication: dict[str, Any]) -> dict[str, Any]:
    """Move the ready note to Published after the public URL is confirmed."""
    if publication.get("status") != "published" or not publication.get("url"):
        raise ValueError("published Obsidian sync requires a confirmed public URL")
    return _sync(production, publication, PUBLISHED_STAGE)
