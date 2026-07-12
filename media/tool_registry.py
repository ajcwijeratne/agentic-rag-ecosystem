"""Local multimedia tool registry.

The registry records which local tools are allowed for each generation
capability. Defaults live in code so a new install has a useful baseline, while
SQLite stores enablement and operator notes.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Generator

_DB_PATH = Path(os.getenv("MEDIA_DB_PATH", "data/media.db"))

CAPABILITIES = (
    "video",
    "image",
    "voice",
    "avatar",
    "animation",
    "transcribe",
    "visual_embed",
    "document",
    "presentation",
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    capability: str
    kind: str
    command: str | None = None
    endpoint_env: str | None = None
    license: str = "unknown"
    commercial_safe: bool = False
    enabled_by_default: bool = True
    gpu_required: bool = False
    notes: str = ""


DEFAULT_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "ffmpeg",
        "video",
        "cli",
        command="ffmpeg",
        license="LGPL/GPL depending on build",
        commercial_safe=True,
        notes="Core local video processing: trim, concat, audio extract, captions, transcode.",
    ),
    ToolSpec(
        "remotion",
        "video",
        "node",
        command="npx",
        license="Remotion commercial terms may apply",
        commercial_safe=False,
        notes="Programmatic React video rendering. Check license before commercial use.",
    ),
    ToolSpec(
        "comfyui",
        "image",
        "http",
        endpoint_env="COMFYUI_URL",
        license="GPL-3.0 code; model licenses vary",
        commercial_safe=False,
        gpu_required=True,
        notes="Local workflow runner for SDXL, FLUX, ControlNet, image-to-image, inpainting.",
    ),
    ToolSpec(
        "piper",
        "voice",
        "cli",
        command="piper",
        license="MIT code; voice model licenses vary",
        commercial_safe=True,
        notes="Fast local TTS for narration when an approved voice model is available.",
    ),
    ToolSpec(
        "xtts",
        "voice",
        "http",
        endpoint_env="XTTS_URL",
        license="Model/license varies",
        commercial_safe=False,
        gpu_required=True,
        notes="Optional higher quality TTS or cloning path. Requires consent and license review.",
    ),
    ToolSpec(
        "musetalk",
        "avatar",
        "http",
        endpoint_env="MUSETALK_URL",
        license="Open source; model licenses vary",
        commercial_safe=False,
        gpu_required=True,
        notes="Talking-avatar and lip-sync worker endpoint.",
    ),
    ToolSpec(
        "sadtalker",
        "avatar",
        "http",
        endpoint_env="SADTALKER_URL",
        license="Apache-2.0 code; model licenses vary",
        commercial_safe=False,
        gpu_required=True,
        notes="Optional portrait animation path. Requires output review before publish.",
    ),
    ToolSpec(
        "manim",
        "animation",
        "cli",
        command="manim",
        license="MIT",
        commercial_safe=True,
        notes="Python animation engine for diagrams, equations, and explainers.",
    ),
    ToolSpec(
        "blender",
        "animation",
        "cli",
        command="blender",
        license="GPL-3.0",
        commercial_safe=True,
        gpu_required=True,
        notes="3D animation, compositing, and rendered scene generation.",
    ),
    ToolSpec(
        "faster-whisper",
        "transcribe",
        "python",
        license="MIT code; model licenses vary",
        commercial_safe=True,
        notes="Existing local transcription path.",
    ),
    ToolSpec(
        "open_clip",
        "visual_embed",
        "python",
        license="MIT code; model licenses vary",
        commercial_safe=True,
        notes="Existing local visual embedding path.",
    ),
    ToolSpec(
        "python-docx",
        "document",
        "python",
        license="MIT",
        commercial_safe=True,
        notes="Local document generation support when configured.",
    ),
    ToolSpec(
        "python-pptx",
        "presentation",
        "python",
        license="MIT",
        commercial_safe=True,
        notes="Local presentation generation support when configured.",
    ),
    ToolSpec(
        "f5-tts",
        "voice",
        "http",
        endpoint_env="F5_TTS_URL",
        license="MIT (F5-TTS); clone requires owner consent",
        commercial_safe=True,
        gpu_required=True,
        notes="Free self-hosted voice clone worker (gpu_workers/voice_worker.py on the GPU PC). All output is clone output.",
    ),
    ToolSpec(
        "elevenlabs",
        "voice",
        "api",
        endpoint_env="ELEVENLABS_API_KEY",
        license="ElevenLabs commercial terms; cloned voice requires owner consent",
        commercial_safe=True,
        notes="Cloud TTS with Aaron's cloned voice. All output is clone output and gates before leaving the system.",
    ),
    ToolSpec(
        "heygen",
        "avatar",
        "api",
        endpoint_env="HEYGEN_API_KEY",
        license="HeyGen commercial terms; avatar clone requires owner consent",
        commercial_safe=True,
        notes="Cloud talking-head video with Aaron's avatar clone. All output is clone output and gates before leaving the system.",
    ),
)

_DEFAULT_BY_NAME = {tool.name: tool for tool in DEFAULT_TOOLS}
_CAPABILITY_DEFAULT = {
    "video": "ffmpeg",
    "image": "comfyui",
    "audio": "piper",
    "voice": "piper",
    "avatar": "musetalk",
    "animation": "manim",
    "transcribe": "faster-whisper",
    "visual_embed": "open_clip",
    "document": "python-docx",
    "presentation": "python-pptx",
}


def _now_path() -> Path:
    return Path(os.getenv("MEDIA_DB_PATH", str(_DB_PATH)))


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    db_path = _now_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_tools (
            name            TEXT PRIMARY KEY,
            capability      TEXT NOT NULL,
            kind            TEXT NOT NULL,
            command         TEXT,
            endpoint_env    TEXT,
            license         TEXT NOT NULL,
            commercial_safe INTEGER NOT NULL,
            enabled         INTEGER NOT NULL,
            gpu_required    INTEGER NOT NULL,
            notes           TEXT
        )
    """)
    conn.commit()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def bootstrap_defaults() -> None:
    """Insert default tools without overwriting operator changes."""
    with _db() as conn:
        for tool in DEFAULT_TOOLS:
            conn.execute(
                "INSERT OR IGNORE INTO media_tools "
                "(name,capability,kind,command,endpoint_env,license,commercial_safe,enabled,gpu_required,notes) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    tool.name,
                    tool.capability,
                    tool.kind,
                    tool.command,
                    tool.endpoint_env,
                    tool.license,
                    int(tool.commercial_safe),
                    int(tool.enabled_by_default),
                    int(tool.gpu_required),
                    tool.notes,
                ),
            )


def _env_name(tool: str, field: str) -> str:
    return f"MEDIA_TOOL_{tool.upper().replace('-', '_')}_{field}"


def _apply_env(row: dict[str, Any]) -> dict[str, Any]:
    name = row["name"]
    enabled = os.getenv(_env_name(name, "ENABLED"))
    command = os.getenv(_env_name(name, "COMMAND"))
    endpoint = os.getenv(_env_name(name, "ENDPOINT"))
    if enabled is not None:
        row["enabled"] = enabled.strip().lower() not in {"0", "false", "no", "off"}
    if command:
        row["command"] = command
    if endpoint and row.get("endpoint_env"):
        row["endpoint"] = endpoint
    elif row.get("endpoint_env"):
        row["endpoint"] = os.getenv(row["endpoint_env"], "")
    return row


def _row_to_tool(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["commercial_safe"] = bool(data["commercial_safe"])
    data["enabled"] = bool(data["enabled"])
    data["gpu_required"] = bool(data["gpu_required"])
    data = _apply_env(data)
    command = data.get("command")
    endpoint = data.get("endpoint")
    data["available"] = True
    if data["kind"] in {"cli", "node"} and command:
        data["available"] = shutil.which(command) is not None
    if data["kind"] == "http":
        data["available"] = bool(endpoint)
    if data["kind"] == "api":
        data["available"] = bool(os.getenv(data.get("endpoint_env") or ""))
    return data


def list_tools(capability: str | None = None, *, enabled_only: bool = False) -> list[dict[str, Any]]:
    bootstrap_defaults()
    clauses: list[str] = []
    params: list[Any] = []
    if capability:
        lookup = "voice" if capability == "audio" else capability
        clauses.append("capability = ?")
        params.append(lookup)
    if enabled_only:
        clauses.append("enabled = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with _db() as conn:
        rows = conn.execute(
            f"SELECT * FROM media_tools {where} ORDER BY capability, name",
            params,
        ).fetchall()
    return [_row_to_tool(row) for row in rows]


def get_tool(name: str) -> dict[str, Any] | None:
    bootstrap_defaults()
    with _db() as conn:
        row = conn.execute("SELECT * FROM media_tools WHERE name=?", (name,)).fetchone()
    return _row_to_tool(row) if row else None


def set_tool_enabled(name: str, enabled: bool, *, notes: str | None = None) -> bool:
    bootstrap_defaults()
    with _db() as conn:
        if notes is None:
            cur = conn.execute("UPDATE media_tools SET enabled=? WHERE name=?", (int(enabled), name))
        else:
            cur = conn.execute(
                "UPDATE media_tools SET enabled=?, notes=? WHERE name=?",
                (int(enabled), notes, name),
            )
    return cur.rowcount > 0


def default_tool_for(capability: str) -> dict[str, Any] | None:
    name = os.getenv(f"MEDIA_TOOL_DEFAULT_{capability.upper()}") or _CAPABILITY_DEFAULT.get(capability)
    return get_tool(name) if name else None


def require_tool(
    capability: str,
    *,
    tool_name: str | None = None,
    require_commercial_safe: bool = False,
) -> dict[str, Any]:
    tool = get_tool(tool_name) if tool_name else default_tool_for(capability)
    if not tool:
        raise RuntimeError(f"no media tool registered for capability {capability!r}")
    if not tool["enabled"]:
        raise RuntimeError(f"media tool {tool['name']!r} is disabled")
    if require_commercial_safe and not tool["commercial_safe"]:
        raise RuntimeError(f"media tool {tool['name']!r} needs license approval before commercial use")
    return tool


def as_plain_specs() -> list[dict[str, Any]]:
    """Return code defaults without SQLite state, useful for docs and diagnostics."""
    return [asdict(tool) for tool in DEFAULT_TOOLS]
