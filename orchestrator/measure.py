"""Measure stage: attach a number to published work, then close it.

The production state machine ends at `measure`, but nothing filled it. This
sweep is the back half of the loop. A window after a production reaches
`publish` it tries to measure every publication:

  * YouTube publications are pulled from the Data API.
  * LinkedIn and other handoff channels cannot be scraped safely, so the sweep
    asks once by notification and waits for a pasted `outcome ...` reply.

A production advances publish -> measure once every publication carries an
outcome or the grace period after the request has lapsed. Idempotent: a
`measure_requested_at` flag in each publication's meta stops repeat pings, and
an existing outcome row stops repeat pulls.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from . import outcomes

WINDOW_DAYS = int(os.getenv("MEASURE_AFTER_DAYS", "7"))
GRACE_DAYS = int(os.getenv("MEASURE_GRACE_DAYS", "3"))


def _age_days(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - ts).total_seconds() / 86400.0


def _pub_age(pub: dict[str, Any]) -> float | None:
    return _age_days(pub.get("published_at") or pub.get("created_at"))


async def _try_pull(pub: dict[str, Any]) -> dict[str, Any] | None:
    """Pull metrics for channels we can read. None when not pullable."""
    if pub.get("channel") == "youtube" and pub.get("external_id"):
        try:
            from publishers import youtube

            return await youtube.fetch_stats(str(pub["external_id"]))
        except Exception:
            return None
    return None


async def _request_once(pub: dict[str, Any], production: dict[str, Any]) -> bool:
    """Ask for numbers a single time; record that we asked."""
    from publishers import store as pub_store

    meta = dict(pub.get("meta") or {})
    if meta.get("measure_requested_at"):
        return False
    pid = production.get("production_id")
    title = production.get("title") or pid
    body = (f"Time to measure: {title} ({pub.get('channel')}).\n"
            f"Reply: outcome {pid} 4200 views 38 comments {pub.get('channel')}")
    try:
        from notifications.notifier import notify

        await notify(title="Measure a publication", body=body)
    except Exception:
        pass
    meta["measure_requested_at"] = outcomes._now()
    pub_store.update_publication(pub["publication_id"], meta=meta)
    return True


def _close_note(production_id: str) -> str:
    agg = outcomes.outcomes_for_production(production_id)
    totals = agg.get("totals") or {}
    parts = [f"{v} {k.replace('_', ' ')}" for k, v in totals.items() if v]
    body = ", ".join(parts) if parts else "no external metrics recorded"
    return f"measured and closed: {body}"


async def run_measure_sweep(actor: str = "daemon") -> dict[str, Any]:
    """One pass over productions sitting in `publish`. Returns a summary."""
    from . import production as pstore
    from publishers import store as pub_store

    summary = {"checked": 0, "pulled": 0, "requested": 0, "closed": 0, "errors": []}
    for prod in pstore.list_productions(state="publish", limit=500):
        summary["checked"] += 1
        pid = prod["production_id"]
        pubs = [p for p in pub_store.list_publications(production_id=pid)
                if p.get("status") in ("published", "handoff_ready")]

        if not pubs:
            # Nothing external to measure; close once the window has passed.
            if (_age_days(prod.get("updated_at")) or 0) >= WINDOW_DAYS:
                await _advance_to_measure(pstore, pid, actor, summary)
            continue

        ready_to_close = True
        for pub in pubs:
            age = _pub_age(pub)
            if age is None or age < WINDOW_DAYS:
                ready_to_close = False
                continue
            has_outcome = outcomes.get_outcome(pub["publication_id"]) is not None
            if not has_outcome:
                pulled = await _try_pull(pub)
                if pulled:
                    outcomes.record_outcome(
                        pub["publication_id"], pid, pub["channel"], pulled,
                        source="youtube_api", note="auto-pulled",
                    )
                    summary["pulled"] += 1
                    has_outcome = True
            if not has_outcome:
                if await _request_once(pub, prod):
                    summary["requested"] += 1
                if age < WINDOW_DAYS + GRACE_DAYS:
                    ready_to_close = False

        if ready_to_close:
            await _advance_to_measure(pstore, pid, actor, summary)
    return summary


async def _advance_to_measure(pstore, production_id: str, actor: str, summary: dict) -> None:
    try:
        pstore.transition(production_id, "measure", actor=actor, note=_close_note(production_id))
        summary["closed"] += 1
    except Exception as exc:
        summary["errors"].append({"production_id": production_id, "error": str(exc)[:200]})
