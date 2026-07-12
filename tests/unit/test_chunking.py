"""Unit tests for vault chunking and section capture."""

from __future__ import annotations

from rag.indexer import chunk_text, chunk_with_sections


def test_chunk_text_window_and_overlap():
    words = " ".join(str(i) for i in range(1000))
    chunks = chunk_text(words, chunk_size=400, overlap=50)
    # step = 350, so starts at 0, 350, 700 -> 3 chunks
    assert len(chunks) == 3
    assert chunks[0].split()[0] == "0"
    assert chunks[1].split()[0] == "350"


def test_chunk_with_sections_captures_heading():
    md = (
        "intro text before any heading\n"
        "# First Heading\n"
        "alpha beta gamma\n"
        "## Second Heading\n"
        "delta epsilon\n"
    )
    pairs = chunk_with_sections(md, chunk_size=400, overlap=50)
    sections = {s for _, s in pairs}
    assert "First Heading" in sections
    assert "Second Heading" in sections
    # Pre-heading text keeps an empty section.
    assert any(sec == "" and "intro text" in txt for txt, sec in pairs)


def test_chunk_with_sections_empty_input():
    assert chunk_with_sections("") == []
    assert chunk_with_sections("   \n  \n") == []


def test_chunk_with_sections_no_headings():
    pairs = chunk_with_sections("just some words with no heading at all")
    assert len(pairs) == 1
    assert pairs[0][1] == ""
