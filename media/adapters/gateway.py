"""Single adapter selection and paid-call gate."""

from __future__ import annotations

import os
import uuid
from typing import Any

from media import tool_registry
from orchestrator import governance

from .base import NotAvailable
from .mcp import (
    MCPAudioGenerator,
    MCPCanvaDocumentGenerator,
    MCPCanvaPresentationGenerator,
    MCPImageGenerator,
    MCPTranscriber,
    MCPVideoGenerator,
)
from .selfhosted import (
    SelfHostedAudioGenerator,
    SelfHostedDocumentGenerator,
    SelfHostedImageGenerator,
    SelfHostedPresentationGenerator,
    SelfHostedTranscriber,
    SelfHostedVideoGenerator,
    SelfHostedVisualEmbedder,
)

_SELF = {
    "image": SelfHostedImageGenerator,
    "video": SelfHostedVideoGenerator,
    "audio": SelfHostedAudioGenerator,
    "document": SelfHostedDocumentGenerator,
    "presentation": SelfHostedPresentationGenerator,
    "transcribe": SelfHostedTranscriber,
    "visual_embed": SelfHostedVisualEmbedder,
}
_MCP = {
    "image": MCPImageGenerator,
    "video": MCPVideoGenerator,
    "audio": MCPAudioGenerator,
    "document": MCPCanvaDocumentGenerator,
    "presentation": MCPCanvaPresentationGenerator,
    "transcribe": MCPTranscriber,
}
_ENV = {
    "image": "ADAPTER_IMAGE",
    "video": "ADAPTER_VIDEO",
    "audio": "ADAPTER_AUDIO",
    "document": "ADAPTER_DOCUMENT",
    "presentation": "ADAPTER_PRESENTATION",
    "transcribe": "ADAPTER_TRANSCRIBE",
    "visual_embed": "ADAPTER_VISUAL_EMBED",
}


def check_paid_gate(context: dict[str, Any] | None = None) -> str:
    context = dict(context or {})
    target_id = context.get("target_id") or context.get("job_id") or str(uuid.uuid4())
    result = governance.check("paid_job", {**context, "target_id": target_id, "job_id": target_id})
    if not result["ok"]:
        raise PermissionError(result["reason"])
    return str(target_id)


def select(capability: str, context: dict[str, Any] | None = None):
    if capability not in _ENV:
        raise ValueError(f"unknown adapter capability: {capability}")
    context = context or {}
    setting = os.getenv(_ENV[capability], "self").strip() or "self"
    if setting == "self":
        tool_registry.require_tool(
            capability,
            tool_name=context.get("tool"),
            require_commercial_safe=bool(context.get("require_commercial_safe")),
        )
        return _SELF[capability]()
    if setting.startswith("mcp:"):
        if capability not in _MCP:
            raise NotAvailable(f"{capability} has no MCP adapter")
        check_paid_gate(context)
        return _MCP[capability](setting.split(":", 1)[1])
    raise ValueError(f"adapter setting must be self or mcp:<name>, got {setting!r}")


def create_document_from_production(production: dict[str, Any], **options: Any) -> dict:
    context = {
        "target_id": options.pop("target_id", None) or production.get("production_id"),
        "production_id": production.get("production_id"),
        "capability": "document",
    }
    adapter = select("document", context)
    return adapter.create_from_production(production, **options)


def copy_document_from_production(production: dict[str, Any], template_id: str, **options: Any) -> dict:
    context = {
        "target_id": options.pop("target_id", None) or production.get("production_id"),
        "production_id": production.get("production_id"),
        "capability": "document",
        "template_id": template_id,
    }
    adapter = select("document", context)
    return adapter.copy_from_production(production, template_id, **options)


def create_presentation_from_production(production: dict[str, Any], **options: Any) -> dict:
    context = {
        "target_id": options.pop("target_id", None) or production.get("production_id"),
        "production_id": production.get("production_id"),
        "capability": "presentation",
    }
    adapter = select("presentation", context)
    return adapter.create_from_production(production, **options)


def copy_presentation_from_production(production: dict[str, Any], template_id: str, **options: Any) -> dict:
    context = {
        "target_id": options.pop("target_id", None) or production.get("production_id"),
        "production_id": production.get("production_id"),
        "capability": "presentation",
        "template_id": template_id,
    }
    adapter = select("presentation", context)
    return adapter.copy_from_production(production, template_id, **options)
