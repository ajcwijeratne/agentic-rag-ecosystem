"""
Dependency health checks shared by every service.

Each service's /health can report real downstream state instead of a static
{"status":"ok"}. deep_health() probes a list of dependencies in parallel and
aggregates them into ok | degraded | down.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx


# Standard probe endpoints per dependency kind.
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")


async def check_http(name: str, url: str, expect_status: int = 200,
                     timeout: float = 3.0) -> dict[str, Any]:
    """Probe a URL with GET. Returns a uniform check record. Never raises."""
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=timeout)
        ok = resp.status_code == expect_status
        return {
            "name": name, "ok": ok, "status_code": resp.status_code,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "url": url,
        }
    except Exception as exc:
        return {
            "name": name, "ok": False, "error": str(exc)[:200],
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
            "url": url,
        }


def qdrant_check() -> dict:
    return {"name": "qdrant", "url": f"{QDRANT_URL}/healthz", "kind": "hard"}


def ollama_check() -> dict:
    return {"name": "ollama", "url": f"{OLLAMA_URL}/api/tags", "kind": "soft"}


def agent_check(name: str, port: int, kind: str = "soft") -> dict:
    host = os.getenv("HEALTH_AGENT_HOST", "localhost")
    return {"name": name, "url": f"http://{host}:{port}/health", "kind": kind}


async def deep_health(dependencies: list[dict], service: str = "") -> dict[str, Any]:
    """Probe every dependency in parallel and aggregate.

    Each dependency dict: {name, url, kind: 'hard'|'soft', [expect_status]}.
    A failed hard dependency makes the service 'down'; a failed soft dependency
    makes it 'degraded'.
    """
    results = await asyncio.gather(*[
        check_http(d["name"], d["url"], d.get("expect_status", 200))
        for d in dependencies
    ]) if dependencies else []

    kind_by_name = {d["name"]: d.get("kind", "soft") for d in dependencies}
    status = "ok"
    for r in results:
        if not r["ok"]:
            if kind_by_name.get(r["name"]) == "hard":
                status = "down"
                break
            status = "degraded"

    return {
        "status":  status,
        "service": service,
        "checks":  {r["name"]: r for r in results},
    }
