"""
Visual Embedder — CLIP, self-hosted
===================================
Embeds images and text into one shared vector space so a text query can find an
image by its content. Backs the `media_visual` Qdrant collection and the visual
leg of media search.

Model: open_clip ViT-B-32 by default (512-dim, cosine). Override with
VISUAL_CLIP_MODEL / VISUAL_CLIP_PRETRAINED. The model loads lazily on first use,
following the reranker's pattern, and every entry point degrades to None if
open_clip or torch is not installed, so the rest of the system keeps working and
visual indexing is simply skipped until the dependency is present.

Vectors are L2-normalised, so a dot product equals cosine similarity, which is
what the Qdrant collections are configured for.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

CLIP_MODEL:      str = os.getenv("VISUAL_CLIP_MODEL", "ViT-B-32")
CLIP_PRETRAINED: str = os.getenv("VISUAL_CLIP_PRETRAINED", "laion2b_s34b_b79k")
VISUAL_DIM:      int = int(os.getenv("VISUAL_VECTOR_DIM", "512"))

_model = None
_preprocess = None
_tokenizer = None
_loaded = False


def _load():
    """Load the CLIP model once. Sets the module globals; returns the model or None."""
    global _model, _preprocess, _tokenizer, _loaded
    if _loaded:
        return _model
    _loaded = True
    try:
        import open_clip
        import torch  # noqa: F401  (imported so a missing torch fails here, cleanly)
        _model, _, _preprocess = open_clip.create_model_and_transforms(
            CLIP_MODEL, pretrained=CLIP_PRETRAINED
        )
        _model.eval()
        _tokenizer = open_clip.get_tokenizer(CLIP_MODEL)
        logger.info(f"[visual_embedder] Loaded {CLIP_MODEL}/{CLIP_PRETRAINED}")
    except Exception as exc:
        logger.warning(
            f"[visual_embedder] CLIP unavailable: {exc}. "
            "Visual indexing and visual search are disabled until open_clip + torch are installed."
        )
        _model = None
    return _model


def available() -> bool:
    """True if the CLIP backend loaded. Cheap after the first call."""
    return _load() is not None


def _normalise(vec) -> list[float]:
    import torch
    vec = vec / vec.norm(dim=-1, keepdim=True)
    return vec.squeeze(0).tolist()


def embed_image(path: str | Path) -> list[float] | None:
    """Embed an image file. Returns a unit vector, or None if CLIP is unavailable
    or the image cannot be read."""
    model = _load()
    if model is None:
        return None
    try:
        import torch
        from PIL import Image
        with Image.open(path) as im:
            tensor = _preprocess(im.convert("RGB")).unsqueeze(0)
        with torch.no_grad():
            feats = model.encode_image(tensor)
        return _normalise(feats)
    except Exception as exc:
        logger.warning(f"[visual_embedder] could not embed image {path}: {exc}")
        return None


def embed_text(text: str) -> list[float] | None:
    """Embed a text query into the image space (CLIP text encoder). Returns a unit
    vector, or None if CLIP is unavailable."""
    model = _load()
    if model is None:
        return None
    try:
        import torch
        tokens = _tokenizer([text])
        with torch.no_grad():
            feats = model.encode_text(tokens)
        return _normalise(feats)
    except Exception as exc:
        logger.warning(f"[visual_embedder] could not embed text: {exc}")
        return None
