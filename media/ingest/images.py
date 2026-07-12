"""
Image ingestion worker.

Phase 1: record dimensions and OCR text, then mark the asset `ready`. OCR text
is stored in the registry's transcripts table (one extracted-text record per
asset), so Phase 2 indexing can pull text uniformly from audio and images.

Caption generation and the visual embedding are Phase 2 and run through the
adapter interface (self-hosted CLIP by default). Both are left out here so
Phase 1 pulls no model into memory. Pillow and pytesseract are optional; a
missing one degrades gracefully rather than failing the asset.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import registry


def _dimensions(image_path: Path) -> str | None:
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(image_path) as im:
            return f"{im.width}x{im.height}"
    except Exception:
        return None


def _ocr(image_path: Path) -> str:
    try:
        import pytesseract
        from PIL import Image
    except Exception:
        return ""
    try:
        with Image.open(image_path) as im:
            return pytesseract.image_to_string(im).strip()
    except Exception:
        return ""


def enrich(asset_id: str, path: str) -> dict[str, Any]:
    image_path = Path(path)
    if not image_path.exists():
        registry.set_status(asset_id, "failed")
        return {"status": "failed", "detail": "file not found"}

    dims = _dimensions(image_path)
    ocr_text = _ocr(image_path)

    if ocr_text:
        registry.add_transcript(asset_id, language=None, segments=[], text=ocr_text)

    fields: dict[str, Any] = {"status": "ready"}
    if dims:
        fields["dimensions"] = dims
    registry.update_asset(asset_id, **fields)

    return {
        "status":     "ready",
        "dimensions": dims,
        "ocr_chars":  len(ocr_text),
        "note":       "caption + visual embedding deferred to Phase 2",
    }
