"""Unit tests for upload text extraction and per-session chat context."""

from __future__ import annotations

import importlib

from orchestrator import uploads


def test_extract_text_plain_and_markdown():
    assert "hello" in uploads.extract_text("note.txt", b"hello world")
    assert "# Title" in uploads.extract_text("note.md", b"# Title\nbody")


def test_extract_text_handles_non_utf8():
    out = uploads.extract_text("weird.txt", b"\xff\xfeabc")
    assert isinstance(out, str)


def test_chat_context_add_get_list_clear():
    importlib.reload(uploads)   # fresh in-memory store
    uploads.add_chat_context("s1", "a.md", "content of a")
    uploads.add_chat_context("s1", "b.md", "content of b")
    assert uploads.list_chat_context("s1") == ["a.md", "b.md"]
    ctx = uploads.get_chat_context("s1")
    assert "a.md" in ctx and "content of a" in ctx
    removed = uploads.clear_chat_context("s1")
    assert removed == 2
    assert uploads.list_chat_context("s1") == []


def test_get_chat_context_empty_session():
    importlib.reload(uploads)
    assert uploads.get_chat_context("nope") == ""


def test_internal_chunk_windows():
    words = " ".join(str(i) for i in range(900))
    chunks = uploads._chunk(words, size=400, overlap=50)
    assert len(chunks) == 3
