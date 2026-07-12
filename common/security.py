"""
Shared security helpers for every FastAPI service in the ecosystem.

Design (single trusted machine):
  - Requests from loopback (127.0.0.1 / ::1) are trusted and skip the key check,
    so the local Command Centre cockpit and service-to-service calls keep working
    untouched.
  - Any non-local caller must present a valid X-API-Key header. With services
    bound to 127.0.0.1 by default, remote callers cannot reach them at all; the
    key is the second layer for when HOST is deliberately widened.
  - require_admin is a stricter gate for destructive or paid actions (cost reset,
    memory/vault mutation, uploads, harness runs, n8n tool calls).

Also provides CORS/bind configuration and file-safety helpers (path
confinement, append-only audit log, pre-write backups).
"""
from __future__ import annotations

import hmac
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request, status

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def _client_host(request: Request) -> str:
    client = request.client
    return client.host if client else ""


def is_loopback(request: Request) -> bool:
    return _client_host(request) in _LOOPBACK


def _constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


def _configured_keys() -> list[str]:
    keys = [os.getenv("API_KEY", "").strip(), os.getenv("ADMIN_API_KEY", "").strip()]
    raw_roles = os.getenv("RBAC_ROLE_KEYS", "").strip()
    if raw_roles:
        try:
            role_keys = json.loads(raw_roles)
            keys.extend(str(v).strip() for v in role_keys.values() if v)
        except Exception:
            pass
    return [k for k in keys if k]


def require_api_key(request: Request) -> None:
    """Allow loopback unconditionally; otherwise require API_KEY or ADMIN_API_KEY."""
    if is_loopback(request):
        return
    keys = _configured_keys()
    if not keys:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_KEY not configured; remote access is disabled",
        )
    provided = request.headers.get("x-api-key", "")
    if not any(_constant_eq(provided, k) for k in keys):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )


def require_admin(request: Request) -> None:
    """Stricter gate for destructive or paid actions."""
    if is_loopback(request):
        return
    admins = []
    raw_roles = os.getenv("RBAC_ROLE_KEYS", "").strip()
    if raw_roles:
        try:
            role_keys = json.loads(raw_roles)
            admin_key = str(role_keys.get("admin", "")).strip()
            if admin_key:
                admins.append(admin_key)
        except Exception:
            pass
    admins.extend(k for k in [
        os.getenv("ADMIN_API_KEY", "").strip(),
        os.getenv("API_KEY", "").strip(),
    ] if k)
    if not admins:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_API_KEY not configured; remote admin is disabled",
        )
    provided = request.headers.get("x-api-key", "")
    if not any(_constant_eq(provided, admin) for admin in admins):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required for this action",
        )


# ---------------------------------------------------------------------------
# Network config (CORS + bind host)
# ---------------------------------------------------------------------------

def allowed_origins() -> list[str]:
    """Trusted browser origins. Comma-separated ALLOWED_ORIGINS, or localhost."""
    raw = os.getenv("ALLOWED_ORIGINS", "").strip()
    if not raw:
        return ["http://localhost:8000", "http://127.0.0.1:8000"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def cors_kwargs() -> dict:
    """Keyword args for CORSMiddleware. Replaces allow_origins=['*']."""
    return {
        "allow_origins": allowed_origins(),
        "allow_credentials": True,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }


def bind_host() -> str:
    """Address services bind to. Loopback by default; widen via HOST env."""
    return os.getenv("HOST", "127.0.0.1")


# ---------------------------------------------------------------------------
# File-safety helpers
# ---------------------------------------------------------------------------

def confine_to_roots(candidate: str | Path, roots: list[Path]) -> Path:
    """Resolve candidate and confirm it sits inside one of roots.

    Raises HTTP 400 on an unparseable path, 403 when the path escapes every
    permitted root. Returns the resolved absolute path on success.
    """
    try:
        resolved = Path(candidate).resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid path")
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return resolved
        except ValueError:
            continue
    raise HTTPException(
        status_code=403,
        detail="Path is outside the permitted media roots",
    )


def audit_log(event: str, detail: dict | None = None) -> None:
    """Append one JSON line to the audit log. Never raises."""
    log_path = Path(os.getenv("AUDIT_LOG_PATH", "logs/audit.jsonl"))
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": datetime.now(timezone.utc).isoformat(), "event": event}
        if detail:
            record.update(detail)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def backup_file(target: Path) -> str | None:
    """Copy target to the backup dir with a timestamp before it is mutated.

    Returns the backup path as a string, or None if there was nothing to back
    up. Never raises.
    """
    target = Path(target)
    if not target.is_file():
        return None
    backup_root = Path(os.getenv("VAULT_BACKUP_DIR", "logs/vault_backups"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = backup_root / f"{target.stem}.{stamp}{target.suffix}.bak"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest)
        return str(dest)
    except Exception:
        return None
