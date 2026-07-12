"""
Chunk schema — one provenance-carrying record that travels from index time
through retrieval, context assembly, and the final answer's citations.

Every field is optional with a safe default, so chunks read from older Qdrant
payloads (written before provenance fields existed) still load cleanly. Re-index
to populate `section`, `modified_at`, and `chunk_id` on existing collections.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


# Retrieval modes a chunk can be tagged with as it moves through the pipeline.
RETRIEVAL_MODES = ("dense", "bm25", "rrf", "rerank")


@dataclass
class Chunk:
    text:           str = ""
    collection:     str = ""
    file:           str = ""
    section:        str = ""           # nearest preceding Markdown heading
    modified_at:    str = ""           # ISO 8601, source file mtime
    chunk_id:       str = ""           # deterministic id (matches the Qdrant point id)
    score:          float = 0.0        # dense cosine or BM25 score
    rerank_score:   float | None = None
    source:         str = ""           # vault | wijerco | upload | web | cloud | media
    source_agent:   str = ""           # local_data | search | cloud
    retrieval_mode: str = ""           # one of RETRIEVAL_MODES
    # Multimodal fields. Populated for chunks that come from a media asset;
    # left at defaults for vault/wijerco/upload text chunks.
    asset_id:       str = ""           # FK into the media registry
    media_type:     str = ""           # audio | video | image | slide_deck | web_page
    t_start:        float | None = None  # transcript chunk start, seconds
    t_end:          float | None = None  # transcript chunk end, seconds
    speaker:        str = ""           # diarised speaker, when known
    thumbnail_path: str = ""           # preview image for a visual/keyframe hit
    extra:          dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ #

    @classmethod
    def from_qdrant_payload(
        cls,
        payload: dict[str, Any],
        *,
        score: float = 0.0,
        collection: str = "",
        chunk_id: str = "",
        retrieval_mode: str = "dense",
        source_agent: str = "",
    ) -> "Chunk":
        """Build a Chunk from a Qdrant point payload, filling defaults."""
        payload = payload or {}
        return cls(
            text=payload.get("text", ""),
            collection=collection or payload.get("collection", ""),
            file=payload.get("file", ""),
            section=payload.get("section", ""),
            modified_at=payload.get("modified_at", ""),
            chunk_id=chunk_id or payload.get("chunk_id", ""),
            score=round(float(score), 6),
            source=payload.get("source", ""),
            source_agent=source_agent,
            retrieval_mode=retrieval_mode,
            asset_id=payload.get("asset_id", ""),
            media_type=payload.get("media_type", ""),
            t_start=payload.get("t_start"),
            t_end=payload.get("t_end"),
            speaker=payload.get("speaker") or "",
            thumbnail_path=payload.get("thumbnail_path", ""),
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Chunk":
        """Build a Chunk from a loose dict (e.g. an agent's /retrieve chunk)."""
        d = d or {}
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kwargs = {k: d[k] for k in known if k in d}
        extra = {k: v for k, v in d.items() if k not in known and not k.startswith("_")}
        chunk = cls(**kwargs)
        # Preserve retriever-internal keys if present.
        if "_rerank_score" in d and chunk.rerank_score is None:
            chunk.rerank_score = d["_rerank_score"]
        if "_rrf_score" in d:
            extra.setdefault("rrf_score", d["_rrf_score"])
        chunk.extra = extra
        return chunk

    def to_dict(self) -> dict[str, Any]:
        """Flat dict for JSON responses and the BM25/RRF code paths."""
        d = asdict(self)
        extra = d.pop("extra", {}) or {}
        d.update(extra)
        return d

    def dedupe_key(self) -> str:
        """Stable key for dedupe before text-overlap is computed."""
        if self.chunk_id:
            return self.chunk_id
        return f"{self.file}::{self.text[:80]}"

    def source_ref(self) -> dict[str, Any]:
        """The provenance record attached to an answer's `sources` list."""
        ref = {
            "chunk_id":       self.chunk_id,
            "file":           self.file,
            "section":        self.section,
            "collection":     self.collection,
            "modified_at":    self.modified_at,
            "score":          round(self.score, 6),
            "rerank_score":   (round(self.rerank_score, 6)
                               if self.rerank_score is not None else None),
            "retrieval_mode": self.retrieval_mode,
            "source":         self.source,
            "source_agent":   self.source_agent,
        }
        # A media citation points at an asset and, for a transcript hit, a moment.
        if self.asset_id:
            ref["asset_id"]   = self.asset_id
            ref["media_type"] = self.media_type
            if self.t_start is not None:
                ref["t_start"] = self.t_start
                ref["t_end"]   = self.t_end
            if self.speaker:
                ref["speaker"] = self.speaker
            if self.thumbnail_path:
                ref["thumbnail_path"] = self.thumbnail_path
        return ref
