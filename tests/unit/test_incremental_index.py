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
    monkeypatch.setenv("KB_EXTRA_DIRS", "")  # hermetic: no Drive copy from the machine running tests
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


def test_load_vault_skips_archive_and_tool_folders(env):
    idx, _, vault = env
    (vault / "13_Command Centre" / "_archive" / "Engagements").mkdir(parents=True)
    (vault / "13_Command Centre" / "_archive" / "Engagements" / "old.md").write_text("retired", encoding="utf-8")
    (vault / ".stversions").mkdir()
    (vault / ".stversions" / "x.md").write_text("old version", encoding="utf-8")
    (vault / "13_Command Centre" / "live.md").write_text("live", encoding="utf-8")
    (vault / "_archive.md").write_text("a note named like the folder is kept", encoding="utf-8")
    assert sorted(r for r, _, _ in idx.load_vault(vault)) == ["13_Command Centre/live.md", "_archive.md"]


@respx.mock
async def test_index_vault_includes_extra_dirs_under_their_label(env, monkeypatch, tmp_path):
    idx, _, vault = env
    (vault / "note.md").write_text("A vault note.", encoding="utf-8")
    drive = tmp_path / "drive-mirror" / "WijerCo"
    (drive / "Clients").mkdir(parents=True)
    (drive / "Clients" / "Brief.md").write_text("A Drive document.", encoding="utf-8")
    monkeypatch.setenv("KB_EXTRA_DIRS", f"drive={tmp_path / 'drive-mirror'};missing={tmp_path / 'nope'}")
    assert [label for label, _ in idx.extra_dirs()] == ["drive"]
    respx.get(f"{QD}/collections/{idx.QDRANT_COLLECTION}").mock(return_value=httpx.Response(200, json={"result": {}}))
    respx.post(f"{QD}/collections/{idx.QDRANT_COLLECTION}/points/scroll").mock(
        return_value=httpx.Response(200, json={"result": {"points": [], "next_page_offset": None}}))
    upsert = respx.put(f"{QD}/collections/{idx.QDRANT_COLLECTION}/points").mock(return_value=httpx.Response(200, json={}))
    _embed_route()
    result = await idx.index_vault(vault)
    assert result["extra_dirs"] == {"drive": 1}
    payloads = {p["payload"]["file"]: p["payload"]["source"] for p in json.loads(upsert.calls[0].request.content)["points"]}
    assert payloads == {"note.md": "vault", "drive/WijerCo/Clients/Brief.md": "drive"}
