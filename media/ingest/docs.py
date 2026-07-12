"""
Document ingestion worker.

Extracts text from .pdf, .docx, .txt, and .md files and writes it to the
registry so it indexes into media_text. Reuses the orchestrator's existing
text-extraction helper rather than duplicating PDF and DOCX parsing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import registry


def enrich(asset_id: str, path: str) -> dict[str, Any]:
    doc_path = Path(path)
    if not doc_path.exists():
        registry.set_status(asset_id, "failed")
        return {"status": "failed", "detail": "file not found"}

    try:
        from orchestrator.uploads import extract_text
        data = doc_path.read_bytes()
        text = extract_text(doc_path.name, data)
    except Exception as exc:
        registry.set_status(asset_id, "failed")
        return {"status": "failed", "detail": f"extraction failed: {exc}"}

    notes: list[str] = []
    if text.strip():
        registry.add_transcript(asset_id, language=None, segments=[], text=text)
    else:
        notes.append("no text extracted")

    registry.update_asset(asset_id, status="ready", meta={"chars": len(text)})
    return {"status": "ready", "chars": len(text), "notes": notes}
