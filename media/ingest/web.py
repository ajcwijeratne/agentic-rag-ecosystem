"""
Web-page ingestion worker.

Fetches a URL, strips boilerplate, and stores the cleaned main text (which then
indexes into media_text) plus the title and fetch time in asset.meta. A full-page
screenshot is optional: it runs only when MEDIA_WEB_SCREENSHOTS=1 and Playwright
is installed, and registers as a linked image asset. Default is text-first, so a
missing browser never blocks ingestion.

httpx and BeautifulSoup are project dependencies; Playwright is not, and its
absence is handled gracefully.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import registry

DERIVED_ROOT = Path(os.getenv("MEDIA_DERIVED_ROOT", "./media_derived"))
SCREENSHOTS = os.getenv("MEDIA_WEB_SCREENSHOTS", "0").lower() in ("1", "true", "yes")
_STRIP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "noscript", "form")


def clean_html(html: str) -> tuple[str, str]:
    """Return (title, main_text) from raw HTML. Pure, so it is unit-testable
    without a network call."""
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return "", ""
    soup = BeautifulSoup(html, "html.parser")
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    for tag in soup(list(_STRIP_TAGS)):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned = "\n".join(ln for ln in lines if ln)
    return title, cleaned


def _screenshot(url: str, out_png: Path) -> bool:
    if not SCREENSHOTS:
        return False
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        out_png.parent.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.screenshot(path=str(out_png), full_page=True)
            browser.close()
        return out_png.exists()
    except Exception:
        return False


def enrich(asset_id: str, url: str) -> dict[str, Any]:
    try:
        import httpx
        resp = httpx.get(url, timeout=30.0, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (agentic-rag media ingest)"})
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        registry.set_status(asset_id, "failed")
        return {"status": "failed", "detail": f"fetch failed: {exc}"}

    title, text = clean_html(html)
    notes: list[str] = []
    if text:
        registry.add_transcript(asset_id, language=None, segments=[], text=text)
        registry.add_moment(
            asset_id,
            kind="page",
            label=title or url,
            text=text,
            meta={"url": url},
        )
    else:
        notes.append("no main text extracted (bs4 missing or empty page)")

    children: list[str] = []
    shot = DERIVED_ROOT / f"web_{asset_id}.png"
    if _screenshot(url, shot):
        asset = registry.get_asset(asset_id, with_relations=False) or {}
        kid = registry.add_asset(
            "image", path=str(shot), source="derived",
            rights=asset.get("rights", "unknown"),
            status="ready", project=asset.get("project"),
            meta={"parent_asset_id": asset_id, "url": url, "capture": "full_page"},
        )
        registry.add_link(kid, asset_id, "thumbnail_of")
        registry.add_moment(
            asset_id,
            kind="screenshot",
            label="Full-page screenshot",
            thumbnail_path=str(shot),
            child_asset_id=kid,
            meta={"url": url, "capture": "full_page"},
        )
        children.append(kid)
    else:
        notes.append("screenshot skipped (disabled or Playwright missing)")

    registry.update_asset(
        asset_id,
        status="ready",
        meta={"url": url, "title": title,
              "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    )
    return {"status": "ready", "title": title, "chars": len(text),
            "children": children, "notes": notes}
