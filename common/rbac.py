"""Role-based access helpers for product-grade deployment."""

from __future__ import annotations

import json
import os
from typing import Callable

from fastapi import HTTPException, Request, status

from .security import is_loopback

ROLES = ("viewer", "operator", "admin")
ROLE_RANK = {role: i for i, role in enumerate(ROLES)}


def _role_keys() -> dict[str, str]:
    """Return role -> key mapping from RBAC_ROLE_KEYS, with API_KEY fallbacks."""
    raw = os.getenv("RBAC_ROLE_KEYS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            return {role: str(data[role]) for role in ROLES if data.get(role)}
        except Exception:
            return {}
    out: dict[str, str] = {}
    if os.getenv("API_KEY"):
        out["operator"] = os.getenv("API_KEY", "")
    if os.getenv("ADMIN_API_KEY"):
        out["admin"] = os.getenv("ADMIN_API_KEY", "")
    return out


def role_for_request(request: Request) -> str:
    """Resolve a request role. Loopback is admin for local operator ergonomics."""
    if is_loopback(request):
        return "admin"
    provided = request.headers.get("x-api-key", "")
    for role, key in _role_keys().items():
        if provided and provided == key:
            return role
    return "anonymous"


def require_role(min_role: str) -> Callable[[Request], None]:
    if min_role not in ROLE_RANK:
        raise ValueError(f"unknown role: {min_role}")

    def _dep(request: Request) -> None:
        role = role_for_request(request)
        if ROLE_RANK.get(role, -1) < ROLE_RANK[min_role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"{min_role} role required",
            )

    return _dep
