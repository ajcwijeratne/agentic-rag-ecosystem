"""
Search Engine Agent — FastMCP Server
======================================
Provides web search retrieval using:
  • SearXNG (self-hosted, preferred)
  • Tavily API (fallback if TAVILY_API_KEY is set)

MCP Tools:
  • web_search(query, num_results)    — returns ranked markdown snippets
  • fetch_url(url)                     — fetch and clean a single URL

REST endpoint:
  POST /retrieve                       — for rag_node httpx calls

Run:
  python -m agents.search_agent
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx
import uvicorn
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastmcp import FastMCP
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEARXNG_URL: str    = os.getenv("SEARXNG_URL", "http://localhost:8080")
TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
PORT: int           = int(os.getenv("SEARCH_AGENT_PORT", "8002"))
DEFAULT_RESULTS: int = 5

# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="search-agent",
    instructions=(
        "Retrieves live web information. Use `web_search` for general queries "
        "and `fetch_url` to get the full cleaned text of a specific webpage."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _html_to_markdown(html: str) -> str:
    """Strip HTML tags and return readable plain text."""
    soup = BeautifulSoup(html, "html.parser")
    # Remove script/style noise
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    text = soup.get_text(separator="\n")
    # Collapse blank lines
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return "\n".join(lines)


async def _searxng_search(query: str, num_results: int) -> list[dict[str, Any]]:
    params = {
        "q": query,
        "format": "json",
        "language": "en",
        "categories": "general",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{SEARXNG_URL}/search",
            params=params,
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", [])[:num_results]:
        results.append({
            "title":   item.get("title", ""),
            "url":     item.get("url", ""),
            "snippet": item.get("content", ""),
            "source":  "searxng",
        })
    return results


async def _tavily_search(query: str, num_results: int) -> list[dict[str, Any]]:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.tavily.com/search",
            json={
                "api_key":              TAVILY_API_KEY,
                "query":                query,
                "search_depth":         "basic",
                "include_answer":       False,
                "include_raw_content":  False,
                "max_results":          num_results,
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for item in data.get("results", []):
        results.append({
            "title":   item.get("title", ""),
            "url":     item.get("url", ""),
            "snippet": item.get("content", ""),
            "source":  "tavily",
        })
    return results


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def web_search(query: str, num_results: int = DEFAULT_RESULTS) -> list[dict[str, Any]]:
    """
    Search the web using SearXNG (self-hosted) or Tavily API.
    Returns a list of {title, url, snippet, source} dicts.
    """
    # Prefer SearXNG; fall back to Tavily if key is configured
    try:
        results = await _searxng_search(query, num_results)
        if results:
            return results
    except Exception:
        pass

    if TAVILY_API_KEY:
        try:
            return await _tavily_search(query, num_results)
        except Exception as exc:
            return [{"error": str(exc), "title": "", "url": "", "snippet": "", "source": ""}]

    return [{"error": "No search backend available", "title": "", "url": "", "snippet": "", "source": ""}]


@mcp.tool()
async def fetch_url(url: str) -> dict[str, Any]:
    """
    Fetch a URL and return cleaned Markdown text.
    Strips HTML noise, nav elements, scripts, and styles.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                url,
                timeout=20.0,
                headers={"User-Agent": "AgenticRAG/1.0 (research bot)"},
            )
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                text = _html_to_markdown(resp.text)
            else:
                text = resp.text[:8000]  # plain text / JSON cap
        return {"url": url, "text": text[:6000], "error": None}
    except Exception as exc:
        return {"url": url, "text": "", "error": str(exc)}


# ---------------------------------------------------------------------------
# REST wrapper for orchestrator rag_node
# ---------------------------------------------------------------------------

from fastapi import Depends
from fastapi.middleware.cors import CORSMiddleware
from common.security import require_api_key, cors_kwargs, bind_host
rest_app = FastAPI(title="Search Agent REST Bridge", dependencies=[Depends(require_api_key)])
rest_app.add_middleware(CORSMiddleware, **cors_kwargs())


class RetrieveRequest(BaseModel):
    query: str
    top_k: int = DEFAULT_RESULTS


@rest_app.post("/retrieve")
async def rest_retrieve(req: RetrieveRequest):
    results = await web_search(req.query, req.top_k)
    chunks = [
        {
            "text": f"{r['title']}\n{r['snippet']}",
            "url": r.get("url", ""),
            "source": r.get("source", "search"),
        }
        for r in results
        if not r.get("error")
    ]
    return {"chunks": chunks}


@rest_app.get("/health")
def health():
    return {"status": "ok", "agent": "search-agent"}


# FastMCP renamed the ASGI factory across versions, so try each known name.
def _mcp_asgi(m):
    for name in ("http_app", "streamable_http_app", "sse_app", "get_asgi_app"):
        fn = getattr(m, name, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                continue
    return None

_mcp_app = _mcp_asgi(mcp)
if _mcp_app is not None:
    rest_app.mount("/mcp", _mcp_app)
else:
    print("[search-agent] MCP ASGI app unavailable in this FastMCP version; REST endpoint still active.")


if __name__ == "__main__":
    uvicorn.run("agents.search_agent:rest_app", host=bind_host(), port=PORT, reload=False)
