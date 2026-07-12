"""Shared multimedia generation job contracts."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

CAPABILITIES = ("video", "image", "voice", "avatar", "animation")
STATUSES = ("queued", "running", "completed", "failed", "blocked")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class GenerationJob:
    capability: str
    brief: dict[str, Any]
    production_id: str | None = None
    tool: str | None = None
    source_assets: list[str] = field(default_factory=list)
    rights: str = "unknown"
    job_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=_now)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITIES:
            raise ValueError(f"capability must be one of {CAPABILITIES}")
        if not isinstance(self.brief, dict):
            raise TypeError("brief must be a dict")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GenerationJob":
        return cls(**data)


@dataclass
class GenerationResult:
    job_id: str
    capability: str
    status: str
    tool: str
    asset_id: str | None = None
    path: str | None = None
    provider: str = "self"
    license_status: str = "unchecked"
    prompt: str | None = None
    error: str | None = None
    completed_at: str = field(default_factory=_now)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITIES:
            raise ValueError(f"capability must be one of {CAPABILITIES}")
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def completed(
        cls,
        job: GenerationJob,
        *,
        tool: str,
        asset_id: str | None = None,
        path: str | None = None,
        provider: str = "self",
        license_status: str = "unchecked",
        prompt: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> "GenerationResult":
        return cls(
            job_id=job.job_id,
            capability=job.capability,
            status="completed",
            tool=tool,
            asset_id=asset_id,
            path=path,
            provider=provider,
            license_status=license_status,
            prompt=prompt,
            meta=meta or {},
        )

    @classmethod
    def failed(cls, job: GenerationJob, *, tool: str, error: str) -> "GenerationResult":
        return cls(
            job_id=job.job_id,
            capability=job.capability,
            status="failed",
            tool=tool,
            error=error,
        )

    @classmethod
    def blocked(cls, job: GenerationJob, *, tool: str, reason: str) -> "GenerationResult":
        return cls(
            job_id=job.job_id,
            capability=job.capability,
            status="blocked",
            tool=tool,
            error=reason,
        )
