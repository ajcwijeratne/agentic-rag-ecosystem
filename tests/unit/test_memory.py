"""Unit tests for the memory store, with Qdrant and the embedder mocked (respx)."""

from __future__ import annotations

import re

import pytest

respx = pytest.importorskip("respx")
import httpx  # noqa: E402

from memory.memory_store import MemoryStore  # noqa: E402

VEC = [0.0] * 768


@pytest.mark.asyncio
@respx.mock
async def test_add_returns_id():
    respx.post(re.compile(r".*/api/embeddings")).mock(
        return_value=httpx.Response(200, json={"embedding": VEC}))
    respx.get(re.compile(r".*/collections/agent_memory$")).mock(
        return_value=httpx.Response(200, json={"result": {}}))
    respx.put(re.compile(r".*/collections/agent_memory/points")).mock(
        return_value=httpx.Response(200, json={"result": "ok"}))

    store = MemoryStore()
    entry_id = await store.add("Swinburne", "Needs an MBA unit redesign.", source="learning_design")
    assert isinstance(entry_id, str) and len(entry_id) > 10


@pytest.mark.asyncio
@respx.mock
async def test_recall_parses_results():
    respx.post(re.compile(r".*/api/embeddings")).mock(
        return_value=httpx.Response(200, json={"embedding": VEC}))
    respx.get(re.compile(r".*/collections/agent_memory$")).mock(
        return_value=httpx.Response(200, json={"result": {}}))
    respx.post(re.compile(r".*/collections/agent_memory/points/search")).mock(
        return_value=httpx.Response(200, json={"result": [
            {"id": "m1", "score": 0.91,
             "payload": {"entity": "Swinburne", "content": "MBA redesign", "source": "ld", "timestamp": 1.0}},
        ]}))

    store = MemoryStore()
    memories = await store.recall("MBA curriculum", top_k=3)
    assert len(memories) == 1
    assert memories[0].entity == "Swinburne"
    assert memories[0].score == 0.91


@pytest.mark.asyncio
@respx.mock
async def test_recall_returns_empty_on_qdrant_error():
    respx.post(re.compile(r".*/api/embeddings")).mock(
        return_value=httpx.Response(200, json={"embedding": VEC}))
    respx.get(re.compile(r".*/collections/agent_memory$")).mock(
        return_value=httpx.Response(200, json={"result": {}}))
    respx.post(re.compile(r".*/collections/agent_memory/points/search")).mock(
        return_value=httpx.Response(500, text="boom"))

    store = MemoryStore()
    assert await store.recall("anything") == []


def test_format_for_prompt():
    from memory.memory_store import MemoryEntry
    store = MemoryStore()
    out = store.format_for_prompt([MemoryEntry("m1", "Swinburne", "fact", "ld", 1.0)])
    assert "Swinburne" in out and "fact" in out
    assert store.format_for_prompt([]) == ""
