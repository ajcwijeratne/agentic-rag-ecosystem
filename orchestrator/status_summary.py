"""
One-call status for small screens
=================================

  GET /status/summary

The phone's Approvals tab shows how the brain is doing in one glance: the
services, the daemon, voice, spend and what is waiting for Aaron. The desktop
pages gather this from half a dozen endpoints, several of them pointed at
`localhost` ports that only resolve on wijerco itself; from a phone over
Tailscale those always read as down. Here the orchestrator asks on the
phone's behalf, in parallel, each check with its own short deadline, so one
slow dependency makes its own row grey instead of stalling the whole screen.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["status"])

CHECK_TIMEOUT_S = 4.0

SERVICE_LABELS = {
    "qdrant": "Vector store (Qdrant)",
    "ollama": "Local models (Ollama)",
    "local_data": "Local data agent",
    "search": "Search agent",
    "cloud": "Cloud agent",
}


# ---------------------------------------------------------------------------
# Collectors. Each is a seam for tests and returns plain data or raises.
# ---------------------------------------------------------------------------

async def _deep_health() -> dict:
    from common.health import deep_health

    from .main import _orchestrator_dependencies

    return await deep_health(_orchestrator_dependencies(), service="orchestrator")


async def _daemon() -> dict:
    from . import daemon

    return await asyncio.to_thread(daemon.status)


async def _voice_engines() -> dict:
    from .voice import voice_engines

    return await voice_engines()


async def _tts() -> dict:
    from .voice_tts import voice_tts_status

    return await voice_tts_status()


async def _gates() -> list:
    from . import governance

    return (await asyncio.to_thread(governance.pending)).get("items", [])


async def _proposals() -> list:
    from harness.store import list_proposals

    return await asyncio.to_thread(list_proposals, "pending")


def _spend_today() -> dict:
    """Model spend since local midnight, from the tail of the persistent ledger."""
    from .cost_tracker import LOG_PATH

    start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    total, calls = 0.0, 0
    if LOG_PATH.exists():
        with LOG_PATH.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 2_000_000))        # a day is far smaller than this
            lines = f.read().decode("utf-8", "ignore").splitlines()
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if float(row.get("timestamp") or 0) >= start:
                total += float(row.get("cost_usd") or 0.0)
                calls += 1
    return {"today_usd": round(total, 4), "calls_today": calls}


async def _spend() -> dict:
    return await asyncio.to_thread(_spend_today)


async def _drafting() -> int:
    from . import outputs

    return outputs.running_count()


async def _guard(name: str, fn: Callable[[], Awaitable[Any]]) -> tuple[bool, Any]:
    try:
        return True, await asyncio.wait_for(fn(), timeout=CHECK_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — a failed check is a status, not an error
        logger.info("status summary: %s check failed: %s", name, exc)
        return False, str(exc)[:200] or exc.__class__.__name__


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------

def _services(ok: bool, deep: Any) -> list[dict]:
    rows = [{"name": "Orchestrator", "ok": True, "detail": "answering"}]
    if not ok or not isinstance(deep, dict):
        rows.append({"name": "Dependencies", "ok": None, "detail": f"could not check: {deep}"})
        return rows
    for key, check in (deep.get("checks") or {}).items():
        good = bool(check.get("ok"))
        rows.append({
            "name": SERVICE_LABELS.get(key, key),
            "ok": good,
            "detail": "" if good else str(check.get("error") or check.get("status") or "not answering")[:120],
        })
    return rows


def _daemon_state(ok: bool, st: Any, now: datetime) -> dict:
    if not ok or not isinstance(st, dict):
        return {"state": "unknown", "detail": str(st)[:120]}
    hb = st.get("last_heartbeat")
    age = None
    if hb:
        try:
            age = (now - datetime.fromisoformat(hb)).total_seconds()
        except ValueError:
            age = None
    stale_after = max(600, 3 * int(st.get("interval_sec") or 0))
    if age is None or age > stale_after:
        state = "offline"
    elif st.get("paused"):
        state = "paused"
    else:
        state = "running"
    budget = st.get("budget") or {}
    return {
        "state": state,
        "cycles": st.get("cycles", 0),
        "last_heartbeat": hb,
        "heartbeat_age_s": None if age is None else int(age),
        "budget": {k: budget.get(k) for k in ("enabled", "spent_usd", "budget_usd", "level") if k in budget},
    }


def _voice(ok_e: bool, eng: Any, ok_t: bool, tts: Any) -> dict:
    hearing = []
    if ok_e and isinstance(eng, dict) and eng.get("mode") != "unavailable":
        if (eng.get("whisper") or {}).get("available"):
            hearing.append("Whisper")
        if (eng.get("vosk") or {}).get("available"):
            hearing.append("VOSK")
    speaking = []
    rtf = None
    if ok_t and isinstance(tts, dict):
        engines = tts.get("engines") or {}
        k = engines.get("kokoro") or {}
        if k.get("available"):
            speaking.append("Kokoro")
            rtf = k.get("rtf")
        if (engines.get("elevenlabs") or {}).get("available"):
            speaking.append("ElevenLabs")
    detail = []
    detail.append("hears with " + " + ".join(hearing) if hearing else "no speech recognition")
    if speaking:
        detail.append("speaks with " + " + ".join(speaking) + (f" ({rtf:.2f}x real time)" if isinstance(rtf, (int, float)) else ""))
    else:
        detail.append("browser voice only")
    return {"ok": bool(hearing), "hears": hearing, "speaks": speaking, "detail": "; ".join(detail)}


@router.get("/status/summary")
async def status_summary() -> dict:
    now = datetime.now(timezone.utc)
    (ok_d, deep), (ok_dm, dm), (ok_e, eng), (ok_t, tts), (ok_g, gates), (ok_p, props), (ok_s, spend), (ok_o, drafting) = \
        await asyncio.gather(
            _guard("health", _deep_health), _guard("daemon", _daemon), _guard("voice", _voice_engines),
            _guard("tts", _tts), _guard("gates", _gates), _guard("proposals", _proposals),
            _guard("spend", _spend), _guard("outputs", _drafting),
        )
    services = _services(ok_d, deep)
    down = [s for s in services if s["ok"] is False]
    overall = "ok" if not down else ("down" if any(s["name"] == SERVICE_LABELS["qdrant"] for s in down) else "degraded")
    n_gates = len(gates) if ok_g and isinstance(gates, list) else None
    n_props = len(props) if ok_p and isinstance(props, list) else None
    return {
        "at": now.isoformat(timespec="seconds"),
        "overall": overall,
        "services": services,
        "daemon": _daemon_state(ok_dm, dm, now),
        "voice": _voice(ok_e, eng, ok_t, tts),
        "spend": spend if ok_s and isinstance(spend, dict) else None,
        "queue": {
            "gates": n_gates, "proposals": n_props,
            "total": (n_gates or 0) + (n_props or 0),
        },
        "outputs": {"drafting": drafting if ok_o else None},
    }
