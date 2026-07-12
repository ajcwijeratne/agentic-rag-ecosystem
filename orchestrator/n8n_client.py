"""
n8n MCP Client
==============
Talks to the n8n "MCP Server Trigger" node over its Streamable HTTP transport
(default endpoint http://localhost:5678/mcp-server/http), exposing your n8n
workflows to the orchestrator as callable tools.

Implements a minimal MCP JSON-RPC client with httpx (no SDK version coupling):
  initialize → notifications/initialized → tools/list → tools/call

Auth: if N8N_MCP_TOKEN is set, it is sent as a Bearer token. For n8n's
"Header Auth" option, set N8N_MCP_HEADER (name) and N8N_MCP_TOKEN (value).
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

N8N_MCP_URL:    str = os.getenv("N8N_MCP_URL", "http://localhost:5678/mcp-server/http")
N8N_MCP_TOKEN:  str = os.getenv("N8N_MCP_TOKEN", "")
N8N_MCP_HEADER: str = os.getenv("N8N_MCP_HEADER", "")   # custom header name (optional)
_PROTOCOL = "2025-03-26"


def _base_headers() -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "Accept":       "application/json, text/event-stream",
    }
    if N8N_MCP_TOKEN:
        if N8N_MCP_HEADER:
            h[N8N_MCP_HEADER] = N8N_MCP_TOKEN
        else:
            h["Authorization"] = f"Bearer {N8N_MCP_TOKEN}"
    return h


def _parse_rpc(resp: httpx.Response) -> dict:
    """Parse a JSON-RPC response that may be plain JSON or an SSE stream."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        # Find the last `data:` line carrying a JSON-RPC object
        result = {}
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    try:
                        result = json.loads(payload)
                    except Exception:
                        pass
        return result
    try:
        return resp.json()
    except Exception:
        return {}


async def _rpc(client: httpx.AsyncClient, method: str, params: dict,
               session_id: str | None, msg_id: int | None) -> tuple[dict, str | None]:
    """Send one JSON-RPC call. Returns (parsed_result, session_id)."""
    headers = _base_headers()
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params

    resp = await client.post(N8N_MCP_URL, json=body, headers=headers, timeout=30.0)
    new_session = resp.headers.get("mcp-session-id") or session_id
    if resp.status_code >= 400:
        raise RuntimeError(f"n8n MCP {method} -> HTTP {resp.status_code}: {resp.text[:300]}")
    return _parse_rpc(resp), new_session


async def _handshake(client: httpx.AsyncClient) -> str | None:
    """initialize + initialized; returns the session id (if the server uses one)."""
    init_params = {
        "protocolVersion": _PROTOCOL,
        "capabilities":    {},
        "clientInfo":      {"name": "wijerco-orchestrator", "version": "1.0"},
    }
    _, session_id = await _rpc(client, "initialize", init_params, None, 1)
    # initialized notification (no id, ignore response)
    try:
        await _rpc(client, "notifications/initialized", {}, session_id, None)
    except Exception:
        pass
    return session_id


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def list_tools() -> list[dict]:
    """Return the workflows the n8n MCP server exposes as tools."""
    async with httpx.AsyncClient() as client:
        session_id = await _handshake(client)
        result, _ = await _rpc(client, "tools/list", {}, session_id, 2)
    tools = (result.get("result", {}) or {}).get("tools", [])
    return [
        {
            "name":        t.get("name", ""),
            "description": t.get("description", ""),
            "input_schema": t.get("inputSchema", {}),
        }
        for t in tools
    ]


async def call_tool(name: str, arguments: dict | None = None) -> dict:
    """Invoke an n8n workflow-tool by name with arguments."""
    async with httpx.AsyncClient() as client:
        session_id = await _handshake(client)
        result, _ = await _rpc(
            client, "tools/call",
            {"name": name, "arguments": arguments or {}},
            session_id, 3,
        )
    payload = result.get("result", {}) or {}
    # Flatten content blocks into text
    text_parts = []
    for block in payload.get("content", []) or []:
        if isinstance(block, dict):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            else:
                text_parts.append(json.dumps(block))
    return {
        "tool":     name,
        "content":  "\n".join(text_parts) if text_parts else payload,
        "is_error": payload.get("isError", False),
        "raw":      payload,
    }


async def health() -> dict:
    """Quick connectivity check against the n8n MCP endpoint."""
    try:
        async with httpx.AsyncClient() as client:
            await _handshake(client)
        return {"status": "ok", "url": N8N_MCP_URL, "auth": bool(N8N_MCP_TOKEN)}
    except Exception as exc:
        return {"status": "down", "url": N8N_MCP_URL, "error": str(exc)}
