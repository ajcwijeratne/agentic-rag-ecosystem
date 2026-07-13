"""Prepare a LinkedIn post for deliberate human publication."""

from __future__ import annotations

from typing import Any

from notifications.notifier import notify


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("linkedin_post", "post", "copy", "text", "content", "_raw"):
            found = _text(value.get(key))
            if found:
                return found
        section_order = value.get("section_order")
        section_map = value.get("script")
        if isinstance(section_order, list) and isinstance(section_map, dict):
            parts = [_text(section_map.get(str(section))) for section in section_order]
            joined = "\n\n".join(part for part in parts if part)
            if joined:
                return joined
        for key in ("draft", "script"):
            found = _text(value.get(key))
            if found:
                return found
        for found_value in value.values():
            found = _text(found_value)
            if found:
                return found
    if isinstance(value, list):
        parts = [_text(item) for item in value]
        return "\n\n".join(part for part in parts if part)
    return ""


async def prepare_handoff(production: dict, options: dict[str, Any] | None = None) -> dict:
    options = options or {}
    copy = str(options.get("copy") or _text(production.get("script")) or "").strip()
    if not copy:
        raise ValueError("production has no LinkedIn copy to hand off")
    assets = list(production.get("linked_assets") or [])
    body = (
        f"Ready to post on LinkedIn\n\n{copy}\n\n"
        f"Production: {production.get('production_id')}\n"
        f"Assets: {', '.join(assets) if assets else 'none'}\n\n"
        "Paste the copy into LinkedIn, attach the listed media, then confirm the public URL in Command Centre."
    )
    notification = await notify(
        title=f"LinkedIn handoff: {production.get('title')}",
        body=body[:4000],
        tags=["publication", "linkedin", str(production.get("production_id"))],
    )
    if notification.get("status") not in {"ok", "warning"}:
        raise RuntimeError("LinkedIn handoff notification failed")
    return {
        "channel": "linkedin",
        "status": "handoff_ready",
        "copy": copy,
        "asset_ids": assets,
        "notification": notification,
    }
