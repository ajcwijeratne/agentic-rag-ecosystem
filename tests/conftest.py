"""Shared pytest fixtures and helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the repo importable regardless of where pytest is invoked.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _clear_proxies(monkeypatch):
    """Strip proxy env vars so mocked httpx clients build a normal transport.
    Some CI sandboxes inject a SOCKS proxy that httpx cannot use without the
    optional socksio package; clearing it lets respx intercept cleanly."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(var, raising=False)


def _service_up(url: str, timeout: float = 1.0) -> bool:
    try:
        import httpx
        return httpx.get(url, timeout=timeout).status_code < 500
    except Exception:
        return False


@pytest.fixture
def require_live():
    """Return a callable that skips the test unless a probed URL is reachable."""
    def _check(url: str):
        if not _service_up(url):
            pytest.skip(f"live service not reachable: {url}")
    return _check


@pytest.fixture
def sample_chunks():
    """A handful of chunks with full provenance, including near-duplicates and
    a stale vs recent pair, for assembler tests."""
    return [
        {
            "text": "WijerCo Diagnostic Sprint is a two week rapid audit of teaching quality.",
            "file": "kb/sprint.md", "section": "What It Is", "collection": "wijerco_knowledge",
            "chunk_id": "c1", "score": 0.9, "rerank_score": 0.95,
            "modified_at": "2026-06-01T00:00:00+00:00", "source": "wijerco",
            "source_agent": "local_data", "retrieval_mode": "rerank",
        },
        {
            # near-duplicate of c1
            "text": "WijerCo Diagnostic Sprint is a two week rapid audit of teaching quality today.",
            "file": "kb/sprint.md", "section": "What It Is", "collection": "wijerco_knowledge",
            "chunk_id": "c2", "score": 0.8, "rerank_score": 0.90,
            "modified_at": "2026-06-01T00:00:00+00:00", "source": "wijerco",
            "source_agent": "local_data", "retrieval_mode": "rerank",
        },
        {
            "text": "Competitors include Deloitte, KPMG, PwC and EY in higher education advisory.",
            "file": "kb/competitors.md", "section": "Categories", "collection": "wijerco_knowledge",
            "chunk_id": "c3", "score": 0.7, "rerank_score": 0.85,
            "modified_at": "2020-01-01T00:00:00+00:00", "source": "wijerco",
            "source_agent": "local_data", "retrieval_mode": "rerank",
        },
        {
            "text": "The free twenty minute triage call is the entry point for every engagement.",
            "file": "kb/services.md", "section": "Entry", "collection": "wijerco_knowledge",
            "chunk_id": "c4", "score": 0.6, "rerank_score": 0.80,
            "modified_at": "", "source": "wijerco",
            "source_agent": "local_data", "retrieval_mode": "rerank",
        },
    ]
