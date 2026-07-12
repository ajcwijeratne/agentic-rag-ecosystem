"""Unit tests for the Chunk provenance schema."""

from __future__ import annotations

from rag.schema import Chunk


def test_from_qdrant_payload_fills_defaults():
    c = Chunk.from_qdrant_payload({"text": "hello", "file": "a.md"}, score=0.5,
                                  collection="vault", chunk_id="x1")
    assert c.text == "hello"
    assert c.file == "a.md"
    assert c.collection == "vault"
    assert c.chunk_id == "x1"
    assert c.section == ""        # missing field defaults cleanly
    assert c.modified_at == ""
    assert c.retrieval_mode == "dense"


def test_from_dict_preserves_internal_keys():
    c = Chunk.from_dict({"text": "t", "file": "f", "_rerank_score": 0.42, "_rrf_score": 0.1})
    assert c.rerank_score == 0.42
    assert c.extra.get("rrf_score") == 0.1


def test_to_dict_roundtrip_flattens_extra():
    c = Chunk(text="t", file="f", score=0.3)
    c.extra = {"rrf_score": 0.2}
    d = c.to_dict()
    assert d["text"] == "t" and d["rrf_score"] == 0.2 and "extra" not in d


def test_source_ref_has_all_provenance_fields():
    c = Chunk(text="t", file="f.md", section="S", collection="wijerco_knowledge",
              modified_at="2026-06-01T00:00:00+00:00", chunk_id="c9",
              score=0.7, rerank_score=0.8, source="wijerco",
              source_agent="local_data", retrieval_mode="rerank")
    ref = c.source_ref()
    for k in ("chunk_id", "file", "section", "collection", "modified_at",
              "score", "rerank_score", "retrieval_mode", "source", "source_agent"):
        assert k in ref
    assert ref["rerank_score"] == 0.8


def test_dedupe_key_prefers_chunk_id():
    assert Chunk(chunk_id="abc").dedupe_key() == "abc"
    assert Chunk(file="f", text="hello world").dedupe_key().startswith("f::")
