"""
Cowork MCP Server
=================
Exposes the orchestrator to Claude (Cowork, Claude Desktop, Claude Code) as
MCP tools, so a Cowork session talks to the same brain instead of running a
parallel one. Thin HTTP wrappers over the orchestrator API; no logic here.

Register in Claude Desktop / Cowork as a local stdio server:

    {
      "mcpServers": {
        "wijerco-system": {
          "command": "C:/Users/ajwij/OneDrive/Documents/Agents/agentic-rag-ecosystem/.venv/Scripts/python.exe",
          "args": ["-m", "mcp_server.cowork_mcp"],
          "cwd": "C:/Users/ajwij/OneDrive/Documents/Agents/agentic-rag-ecosystem"
        }
      }
    }

On the mini PC, point ORCHESTRATOR_URL at the Tailscale address and set
ORCH_API_KEY (remote calls need the key; loopback does not).

Run standalone for a quick check: python -m mcp_server.cowork_mcp
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from dotenv import load_dotenv
from fastmcp import FastMCP

load_dotenv()

ORCH_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.getenv("ORCH_API_KEY", os.getenv("API_KEY", ""))

mcp = FastMCP(
    "wijerco-system",
    instructions=(
        "Aaron's agentic operating system: planner, 32-agent roster, memory, "
        "governance, and an autonomous daemon. Submit work, read the brief, "
        "recall memory, approve gates."
    ),
)


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


async def _call(method: str, path: str, payload: dict | None = None,
                timeout: float = 180.0) -> Any:
    async with httpx.AsyncClient() as client:
        r = await client.request(method, f"{ORCH_URL}{path}", json=payload,
                                 headers=_headers(), timeout=timeout)
        r.raise_for_status()
        return r.json()


@mcp.tool()
async def submit_task(text: str, mode: str = "auto", project: str = "") -> dict:
    """Send work to the system's inbox. mode: auto (classify), ask (answer now),
    task (queue for the daemon), plan (decompose into a full operating plan).
    Returns the answer, task id, or plan id."""
    payload: dict[str, Any] = {"channel": "cowork", "sender": "claude", "text": text, "mode": mode}
    if project:
        payload["project"] = project
    return await _call("POST", "/inbox", payload)


@mcp.tool()
async def get_daily_brief() -> dict:
    """The operating daily brief: priorities, pending approvals, productions,
    and project memory."""
    return await _call("GET", "/operating/daily-brief")


@mcp.tool()
async def recall_memory(query: str, k: int = 8) -> dict:
    """Unified memory recall across semantic facts, episodic summaries, and
    project memory. Ask before assuming the system does not know something."""
    result = await _call("POST", "/inbox", {
        "channel": "cowork", "sender": "claude",
        "text": query if query.endswith("?") else query + "?", "mode": "ask",
    })
    return result


@mcp.tool()
async def list_pending_approvals() -> dict:
    """Governance gates waiting on Aaron. Each item names the gate and target."""
    return await _call("GET", "/governance/pending")


@mcp.tool()
async def approve_gate(gate: str, target_id: str, decision: str = "approve",
                       note: str = "") -> dict:
    """Approve or reject a governance gate on Aaron's explicit instruction.
    decision: approve | reject. Never call this without Aaron saying so in
    the current conversation."""
    verb = "approve" if decision.lower().startswith("a") else "reject"
    return await _call("POST", "/inbox", {
        "channel": "cowork", "sender": "claude-on-behalf-of-aaron",
        "text": f"{verb} {gate} {target_id} {note}".strip(),
    })


@mcp.tool()
async def daemon_status() -> dict:
    """Operating daemon state: running or paused, cycles, heartbeat, budget,
    last result."""
    return await _call("GET", "/operating/daemon/status")


@mcp.tool()
async def pause_daemon() -> dict:
    """Pause the operating daemon (kill switch). Persists across restarts."""
    return await _call("POST", "/operating/daemon/pause", {"actor": "cowork"})


@mcp.tool()
async def resume_daemon() -> dict:
    """Resume the operating daemon after a pause."""
    return await _call("POST", "/operating/daemon/resume", {"actor": "cowork"})


@mcp.tool()
async def system_health() -> dict:
    """Deep health check across Qdrant, Ollama, and the sub-agents."""
    return await _call("GET", "/health/deep", timeout=30.0)


if __name__ == "__main__":
    mcp.run()
