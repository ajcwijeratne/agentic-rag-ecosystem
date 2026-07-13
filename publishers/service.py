"""Governed publication orchestration across channels."""

from __future__ import annotations

from typing import Any

from . import linkedin, obsidian, store, youtube


def _channel(value: str) -> str:
    normalized = (value or "").strip().lower().replace("_handoff", "")
    if normalized not in store.CHANNELS:
        raise ValueError(f"channel must be one of {store.CHANNELS}")
    return normalized


def _assert_publishable(production: dict) -> None:
    from orchestrator import governance

    if production.get("state") not in {"publish", "measure"}:
        raise ValueError("production must be in publish or measure before delivery")
    target_id = str(production.get("production_id") or "")
    # Reconstruct the governed review -> publish boundary. A publication may
    # be retried after the state has already advanced, and direct/manual state
    # changes must never bypass the gates required for external delivery.
    gate_context = dict(production)
    gate_context["state"] = "review"
    for gate in governance.required_for_transition(gate_context, "publish"):
        result = governance.check(gate, {"target_id": target_id, "production": production})
        if not result.get("ok"):
            raise PermissionError(f"{gate}: {result.get('reason')}")


def _sync_obsidian(production: dict, publication: dict, *, published: bool = False) -> dict:
    """Keep publication delivery successful even if the local vault is unavailable."""
    meta = dict(publication.get("meta") or {})
    try:
        result = (
            obsidian.sync_published(production, publication)
            if published
            else obsidian.sync_ready(production, publication)
        )
        meta["obsidian_sync"] = result
    except Exception as exc:
        meta["obsidian_sync"] = {"status": "error", "error": str(exc)[:500]}
    return store.update_publication(publication["publication_id"], meta=meta)


async def publish(
    production_id: str,
    channel: str,
    *,
    actor: str = "operator",
    options: dict[str, Any] | None = None,
) -> dict:
    from orchestrator import production as production_store

    production = production_store.get_production(production_id)
    if not production:
        raise KeyError("production not found")
    _assert_publishable(production)
    channel = _channel(channel)
    options = options or {}
    publication = store.create_or_get(production_id, channel, actor, {"options": options})
    if publication.get("status") in {"published", "handoff_ready"}:
        return _sync_obsidian(
            production, publication, published=publication.get("status") == "published"
        )
    publication = store.update_publication(
        publication["publication_id"], status="publishing", actor=actor, error=None
    )
    try:
        if channel == "linkedin":
            result = await linkedin.prepare_handoff(production, options)
            publication = store.update_publication(
                publication["publication_id"],
                status="handoff_ready",
                actor=actor,
                meta=result,
                error=None,
            )
            return _sync_obsidian(production, publication)
        result = await youtube.upload(production, options)
        publication = store.mark_published(
            publication["publication_id"],
            url=result["url"],
            external_id=result.get("external_id"),
            actor=actor,
            meta=result,
        )
        return _sync_obsidian(production, publication, published=True)
    except Exception as exc:
        store.update_publication(
            publication["publication_id"], status="failed", actor=actor, error=str(exc)[:1000]
        )
        raise


async def publish_targets(production: dict, actor: str = "operator") -> list[dict]:
    results: list[dict] = []
    for target in production.get("publish_targets") or []:
        spec = target if isinstance(target, dict) else {"channel": str(target)}
        try:
            item = await publish(
                str(production.get("production_id")),
                str(spec.get("channel") or ""),
                actor=actor,
                options=dict(spec.get("options") or {}),
            )
            results.append(item)
        except Exception as exc:
            results.append({"channel": spec.get("channel"), "status": "failed", "error": str(exc)})
    return results


def confirm_publication(
    publication_id: str,
    *,
    url: str,
    actor: str = "operator",
    external_id: str | None = None,
    note: str = "",
) -> dict:
    publication = store.get_publication(publication_id)
    if not publication:
        raise KeyError("publication not found")
    meta = dict(publication.get("meta") or {})
    if note:
        meta["confirmation_note"] = note
    publication = store.mark_published(
        publication_id,
        url=url,
        external_id=external_id,
        actor=actor,
        meta=meta,
    )
    from orchestrator import production as production_store

    production = production_store.get_production(str(publication.get("production_id") or ""))
    if not production:
        return publication
    return _sync_obsidian(production, publication, published=True)
