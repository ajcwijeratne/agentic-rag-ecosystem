"""Role-based access helpers for product-grade deployment."""

from __future__ import annotations

import hmac
import json
import os
from typing import Callable

from fastapi import HTTPException, Request, status

from .security import is_loopback

ROLES = ("viewer", "operator", "admin")
ROLE_RANK = {role: i for i, role in enumerate(ROLES)}


def _role_keys() -> list[tuple[str, str]]:
    """Return (role, key) pairs: RBAC_ROLE_KEYS first, then the legacy keys.

    ADMIN_API_KEY is admin and API_KEY is operator unless RBAC_ROLE_KEYS
    already names that key. Before 21 Nov 2026 the legacy keys were dropped
    whenever RBAC_ROLE_KEYS was set, so the API_KEY every remote client uses
    resolved to "anonymous" here, while security.require_admin accepted it
    as admin. Remote monitoring and backups then answered 403.
    """
    out: list[tuple[str, str]] = []
    raw = os.getenv("RBAC_ROLE_KEYS", "").strip()
    if raw:
        try:
            data = json.loads(raw)
            out.extend((role, str(data[role]).strip()) for role in ROLES if data.get(role))
        except Exception:
            pass
    named = {key for _, key in out}
    for role, var in (("admin", "ADMIN_API_KEY"), ("operator", "API_KEY")):
        key = os.getenv(var, "").strip()
        if key and key not in named:
            out.append((role, key))
            named.add(key)
    return out


def role_for_request(request: Request) -> str:
    """Resolve a request role. Loopback is admin for local operator ergonomics."""
    if is_loopback(request):
        return "admin"
    provided = request.headers.get("x-api-key", "")
    if not provided:
        return "anonymous"
    for role, key in _role_keys():
        if key and hmac.compare_digest(provided.encode(), key.encode()):
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
