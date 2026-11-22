"""Reindex embeds only new or changed chunks, in batches (21 Nov 2026)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

QD = "http://qdrant.test"
OLL = "http://ollama.test"


@pytest.fixture()
def env(monkeypatch, tmp_path):
    import importlib
    monkeypatch.setenv("QDRANT_URL", QD)
    monkeypatch.setenv("EMBED_URL", f"{OLL}/api/embeddings")
    monkeypatch.delenv("INDEX_FULL", raising=False)
    import rag.embedder as emb
    import rag.indexer as idx
    emb = importlib.reload(emb)
    idx = importlib.reload(idx)
    monkeypatch.setattr(idx, "_record_run", lambda *a, **k: None)
    import rag.retriever as ret
    monkeypatch.setattr(ret, "update_bm25_corpus", lambda *a, **k: None, raising=False)
    vault = tmp_path / "vault"
    vault.mkdir()
    return idx, emb, vault


def _embed_route():
    def handler(request):
        n = len(json.loads(request.content)["input"])
        return httpx.Response(200, json={"embeddings": [[0.1] * 768 for _ in range(n)]})
    return respx.post(f"{OLL}/api/embed").mock(side_effect=handler)


@respx.mock
async def test_embed_batch_uses_one_call_per_batch(env):
    _, emb, _ = env
    route = _embed_route()
    out = await emb.embed_batch([f"t{i}" for i in range(70)], batch_size=32)
    assert len(out) == 70
    assert route.call_count == 3


@respx.mock
async def test_embed_batch_falls_back_to_single_endpoint(env):
    _, emb, _ = env
    respx.post(f"{OLL}/api/embed").mock(return_value=httpx.Response(404))
    single = respx.post(f"{OLL}/api/embeddings").mock(return_value=httpx.Response(200, json={"embedding": [0.2] * 768}))
    out = await emb.embed_batch(["a", "b", "c"], batch_size=2)
    assert len(out) == 3 and single.call_count == 3


@respx.mock
async def test_index_vault_embeds_only_changed_and_removes_stale(env):
    idx, _, vault = env
    (vault / "same.md").write_text("Unchanged note text that stays the same.", encoding="utf-8")
    (vault / "edited.md").write_text("This note was edited today.", encoding="utf-8")
    notes = idx.load_vault(vault)
    chunks = {}
    for rel, text, _ in notes:
        for j, (chunk, _sec) in enumerate(idx.chunk_with_sections(text)):
            chunks[str(idx.uuid.uuid5(idx.uuid.NAMESPACE_URL, f"{rel}::{j}"))] = (rel, chunk)
    same_id = next(k for k, v in chunks.items() if v[0] == "same.md")
    edited_id = next(k for k, v in chunks.items() if v[0] == "edited.md")
    stored = [
        {"id": same_id, "payload": {"text": chunks[same_id][1]}},
        {"id": edited_id, "payload": {"text": "old text before the edit"}},
        {"id": "gone-1", "payload": {"text": "a note that was deleted"}},
    ]
    respx.get(f"{QD}/collections/{idx.QDRANT_COLLECTION}").mock(return_value=httpx.Response(200, json={"result": {}}))
    respx.post(f"{QD}/collections/{idx.QDRANT_COLLECTION}/points/scroll").mock(
        return_value=httpx.Response(200, json={"result": {"points": stored, "next_page_offset": None}}))
    deleted = respx.post(f"{QD}/collections/{idx.QDRANT_COLLECTION}/points/delete").mock(return_value=httpx.Response(200, json={}))
    upsert = respx.put(f"{QD}/collections/{idx.QDRANT_COLLECTION}/points").mock(return_value=httpx.Response(200, json={}))
    embed = _embed_route()

    result = await idx.index_vault(vault)

    assert result["chunks_embedded"] == 1
    assert result["chunks_removed"] == 1
    sent = json.loads(embed.calls[0].request.content)["input"]
    assert sent == ["This note was edited today."]
    assert json.loads(deleted.calls[0].request.content)["points"] == ["gone-1"]
    assert [p["id"] for p in json.loads(upsert.calls[0].request.content)["points"]] == [edited_id]


@respx.mock
async def test_index_vault_full_reembeds_everything(env):
    idx, _, vault = env
    (vault / "a.md").write_text("Alpha note.", encoding="utf-8")
    (vault / "b.md").write_text("Beta note.", encoding="utf-8")
    respx.get(f"{QD}/collections/{idx.QDRANT_COLLECTION}").mock(return_value=httpx.Response(200, json={"result": {}}))
    scroll = respx.post(f"{QD}/collections/{idx.QDRANT_COLLECTION}/points/scroll")
    respx.put(f"{QD}/collections/{idx.QDRANT_COLLECTION}/points").mock(return_value=httpx.Response(200, json={}))
    _embed_route()
    result = await idx.index_vault(vault, full=True)
    assert result["chunks_embedded"] == result["chunks_total"] == 2
    assert scroll.call_count == 0
