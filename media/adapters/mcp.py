"""Remote MCP adapters behind the common media interface."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .base import (
    AudioGenerator,
    DocumentGenerator,
    ImageGenerator,
    NotAvailable,
    PresentationGenerator,
    Transcriber,
    VideoGenerator,
)

_PROTOCOL = "2025-03-26"


class MCPBridge:
    """Minimal Streamable HTTP MCP client for media service bridges."""

    def __init__(self, url: str | None = None, token: str | None = None, header: str | None = None):
        self.url = url or os.getenv("MCP_MEDIA_URL") or os.getenv("CANVA_MCP_URL") or ""
        self.token = token if token is not None else os.getenv("MCP_MEDIA_TOKEN", "")
        self.header = header if header is not None else os.getenv("MCP_MEDIA_HEADER", "")
        if not self.url:
            raise NotAvailable("MCP_MEDIA_URL or CANVA_MCP_URL must be set before MCP adapters can run")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            if self.header:
                headers[self.header] = self.token
            else:
                headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _parse(resp: httpx.Response) -> dict:
        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            parsed: dict[str, Any] = {}
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload and payload != "[DONE]":
                        try:
                            parsed = json.loads(payload)
                        except Exception:
                            pass
            return parsed
        try:
            return resp.json()
        except Exception:
            return {}

    def _rpc(self, client: httpx.Client, method: str, params: dict | None, session_id: str | None, msg_id: int | None) -> tuple[dict, str | None]:
        headers = self._headers()
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if msg_id is not None:
            body["id"] = msg_id
        if params is not None:
            body["params"] = params
        resp = client.post(self.url, json=body, headers=headers, timeout=float(os.getenv("MCP_MEDIA_TIMEOUT", "60")))
        if resp.status_code >= 400:
            raise RuntimeError(f"MCP {method} -> HTTP {resp.status_code}: {resp.text[:300]}")
        return self._parse(resp), resp.headers.get("mcp-session-id") or session_id

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        with httpx.Client() as client:
            init = {
                "protocolVersion": _PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "wijerco-media-adapters", "version": "1.0"},
            }
            _, session_id = self._rpc(client, "initialize", init, None, 1)
            try:
                self._rpc(client, "notifications/initialized", {}, session_id, None)
            except Exception:
                pass
            result, _ = self._rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, session_id, 2)
        payload = result.get("result", {}) or {}
        if payload.get("isError"):
            raise RuntimeError(f"MCP tool {name!r} failed: {payload}")
        return _flatten_tool_payload(name, payload)


def _flatten_tool_payload(name: str, payload: dict) -> dict:
    content = payload.get("content")
    if not content:
        return {"tool": name, "raw": payload}
    if isinstance(content, list):
        text_parts = []
        other_parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            else:
                other_parts.append(block)
        text = "\n".join(part for part in text_parts if part)
        parsed = _maybe_json(text)
        if isinstance(parsed, dict):
            return {"tool": name, **parsed, "raw": payload}
        return {"tool": name, "content": text, "blocks": other_parts, "raw": payload}
    return {"tool": name, "content": content, "raw": payload}


def _maybe_json(value: str) -> Any:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value


def _production_payload(production: dict, kind: str, options: dict[str, Any]) -> dict:
    return {
        "kind": kind,
        "production_id": production.get("production_id"),
        "title": options.get("title") or production.get("title"),
        "project": production.get("project"),
        "format": production.get("format"),
        "owner": production.get("owner"),
        "brand": options.get("brand") or {},
        "brief": production.get("brief") or {},
        "research": production.get("research") or {},
        "script": production.get("script") or {},
        "asset_plan": production.get("asset_plan") or {},
        "edit_plan": production.get("edit_plan") or {},
        "review": production.get("review") or {},
        "linked_assets": production.get("linked_assets") or [],
        "instructions": options.get("instructions") or "",
        "output": {
            "document_type": "presentation" if kind == "presentation" else "document",
            "share": bool(options.get("share", False)),
        },
    }


class _MCPAdapter:
    def __init__(self, name: str, bridge: MCPBridge | None = None):
        self.name = name
        self.bridge = bridge

    def _not_configured(self) -> None:
        raise NotAvailable(f"MCP adapter {self.name!r} does not implement this capability yet")

    def _bridge(self) -> MCPBridge:
        return self.bridge or MCPBridge()


class MCPTranscriber(_MCPAdapter, Transcriber):
    def transcribe(self, path: str, **options: Any) -> dict:
        self._not_configured()


class MCPImageGenerator(_MCPAdapter, ImageGenerator):
    def generate(self, brief: dict, **options: Any) -> dict:
        self._not_configured()


class MCPVideoGenerator(_MCPAdapter, VideoGenerator):
    def generate(self, brief: dict, **options: Any) -> dict:
        self._not_configured()


class MCPAudioGenerator(_MCPAdapter, AudioGenerator):
    def generate(self, brief: dict, **options: Any) -> dict:
        self._not_configured()


class _CanvaMixin:
    kind: str

    def _tool_name(self, action: str) -> str:
        prefix = "CANVA_PRESENTATION" if self.kind == "presentation" else "CANVA_DOCUMENT"
        fallback = "CANVA_COPY_TOOL" if action == "copy" else "CANVA_CREATE_TOOL"
        default = f"canva_{action}_{self.kind}"
        return os.getenv(f"{prefix}_{action.upper()}_TOOL") or os.getenv(fallback) or default

    def _call_canva(self, action: str, production: dict, **options: Any) -> dict:
        if self.name != "canva":
            raise NotAvailable(f"{self.name!r} is not a Canva adapter")
        payload = _production_payload(production, self.kind, options)
        if action == "copy":
            payload["template_id"] = options["template_id"]
        result = self._bridge().call_tool(self._tool_name(action), payload)
        return {
            "provider": "mcp:canva",
            "action": action,
            "kind": self.kind,
            "production_id": production.get("production_id"),
            **result,
        }


class MCPCanvaDocumentGenerator(_CanvaMixin, _MCPAdapter, DocumentGenerator):
    kind = "document"

    def create_from_production(self, production: dict, **options: Any) -> dict:
        return self._call_canva("create", production, **options)

    def copy_from_production(self, production: dict, template_id: str, **options: Any) -> dict:
        return self._call_canva("copy", production, template_id=template_id, **options)


class MCPCanvaPresentationGenerator(_CanvaMixin, _MCPAdapter, PresentationGenerator):
    kind = "presentation"

    def create_from_production(self, production: dict, **options: Any) -> dict:
        return self._call_canva("create", production, **options)

    def copy_from_production(self, production: dict, template_id: str, **options: Any) -> dict:
        return self._call_canva("copy", production, template_id=template_id, **options)
