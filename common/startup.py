"""
Startup dependency checks. Each service calls require_dependencies() in its
FastAPI startup event so a missing hard dependency fails fast with a clear line
instead of serving broken responses.

STRICT_STARTUP=1 turns a missing hard dependency into a non-zero exit. Otherwise
it logs an error and continues, which suits a dev machine where services start in
any order.
"""

from __future__ import annotations

import logging
import os
import sys

from .health import deep_health

logger = logging.getLogger(__name__)

STRICT = os.getenv("STRICT_STARTUP", "0").lower() in ("1", "true", "yes")


async def require_dependencies(dependencies: list[dict], service: str = "") -> dict:
    """Probe dependencies at startup. Log results; exit if STRICT and a hard
    dependency is down. Returns the health report."""
    report = await deep_health(dependencies, service=service)
    for name, check in report["checks"].items():
        if check["ok"]:
            logger.info(f"[startup:{service}] dependency '{name}' ok ({check.get('latency_ms')}ms)")
        else:
            kind = next((d.get("kind", "soft") for d in dependencies if d["name"] == name), "soft")
            msg = f"[startup:{service}] dependency '{name}' UNAVAILABLE: {check.get('error') or check.get('status_code')}"
            if kind == "hard":
                logger.error(msg)
            else:
                logger.warning(msg)

    if report["status"] == "down" and STRICT:
        logger.error(f"[startup:{service}] hard dependency down and STRICT_STARTUP set; exiting.")
        sys.exit(1)

    return report
