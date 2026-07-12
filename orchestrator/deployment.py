"""Product-grade deployment operations: migrations, backups, releases, status."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

DB_PATH = Path(os.getenv("MEDIA_DB_PATH", "data/media.db"))
BACKUP_DIR = Path(os.getenv("DB_BACKUP_DIR", "logs/db_backups"))
RELEASES_PATH = Path(os.getenv("AGENT_RELEASES_PATH", "config/agent_releases.json"))
RELEASE_SNAPSHOT_DIR = Path(os.getenv("AGENT_RELEASE_SNAPSHOT_DIR", "logs/agent_releases"))
SCHEMA_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_under(path: str | Path, root: Path) -> Path:
    """Resolve a user-supplied operational path under a known directory."""
    root_resolved = root.resolve()
    candidate = Path(path)
    candidates = [candidate] if candidate.is_absolute() else [candidate, root / candidate]
    last_error: ValueError | None = None
    for item in candidates:
        resolved = item.resolve()
        try:
            resolved.relative_to(root_resolved)
            return resolved
        except ValueError as exc:
            last_error = exc
    raise ValueError(f"path must be under {root}") from last_error


def _sidecar_paths(path: Path) -> list[Path]:
    return [Path(f"{path}-wal"), Path(f"{path}-shm")]


def _checkpoint_database() -> None:
    if not DB_PATH.exists():
        return
    try:
        with sqlite3.connect(str(DB_PATH), check_same_thread=False) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def migrate() -> dict[str, Any]:
    """Apply idempotent deployment metadata migrations."""
    with _db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version    INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                note       TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deployment_meta (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
        applied = {int(r["version"]) for r in rows}
        changed = []
        if SCHEMA_VERSION not in applied:
            conn.execute(
                "INSERT INTO schema_migrations (version,applied_at,note) VALUES (?,?,?)",
                (SCHEMA_VERSION, _now(), "product-grade deployment baseline"),
            )
            changed.append(SCHEMA_VERSION)
        conn.execute(
            "INSERT OR REPLACE INTO deployment_meta (key,value,updated_at) VALUES (?,?,?)",
            ("schema_version", str(SCHEMA_VERSION), _now()),
        )
    return {"schema_version": SCHEMA_VERSION, "applied": changed}


def migration_status() -> dict[str, Any]:
    try:
        migrate()
        with _db() as conn:
            rows = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
        return {"schema_version": SCHEMA_VERSION, "migrations": [dict(r) for r in rows]}
    except Exception as exc:
        return {"schema_version": 0, "error": str(exc)}


def backup_database() -> dict[str, Any]:
    """Copy the SQLite DB to the backup directory."""
    migrate()
    if not DB_PATH.exists():
        return {"status": "missing", "path": str(DB_PATH)}
    _checkpoint_database()
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    target = BACKUP_DIR / f"{DB_PATH.stem}.{stamp}{DB_PATH.suffix}.bak"
    shutil.copy2(DB_PATH, target)
    return {"status": "ok", "path": str(target), "source": str(DB_PATH)}


def restore_database(backup_path: str, dry_run: bool = True) -> dict[str, Any]:
    """Restore the SQLite DB from a managed backup, with rehearsal by default."""
    migrate()
    source = _resolve_under(backup_path, BACKUP_DIR)
    if not source.is_file():
        return {"status": "missing", "path": str(source), "dry_run": dry_run}
    if source.suffix != ".bak":
        return {"status": "invalid", "path": str(source), "dry_run": dry_run, "reason": "backup must be a .bak file"}

    result: dict[str, Any] = {
        "status": "ready" if dry_run else "ok",
        "dry_run": dry_run,
        "source": str(source),
        "target": str(DB_PATH),
        "source_bytes": source.stat().st_size,
    }
    if dry_run:
        return result

    pre_restore = backup_database()
    for sidecar in _sidecar_paths(DB_PATH):
        try:
            sidecar.unlink(missing_ok=True)
        except Exception:
            pass
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, DB_PATH)
    result["pre_restore_backup"] = pre_restore
    return result


def list_backups(limit: int = 20) -> list[dict[str, Any]]:
    if not BACKUP_DIR.is_dir():
        return []
    files = sorted(BACKUP_DIR.glob("*.bak"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {"path": str(p), "bytes": p.stat().st_size, "created_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat()}
        for p in files[:limit]
    ]


def releases() -> dict[str, Any]:
    if RELEASES_PATH.is_file():
        try:
            return json.loads(RELEASES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "release": os.getenv("AGENT_RELEASE", "local-dev"),
        "agents": {},
        "notes": "No release manifest found.",
    }


def snapshot_release(note: str = "") -> dict[str, Any]:
    """Capture the current agent release manifest for rollback rehearsal."""
    RELEASE_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = releases()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    release = str(manifest.get("release") or os.getenv("AGENT_RELEASE", "local-dev"))
    safe_release = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "-" for ch in release)
    target = RELEASE_SNAPSHOT_DIR / f"{safe_release}.{stamp}.json"
    payload = {
        "captured_at": _now(),
        "source": str(RELEASES_PATH),
        "note": note,
        "manifest": manifest,
    }
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {"status": "ok", "path": str(target), "release": release}


def rollback_release(snapshot_path: str, dry_run: bool = True) -> dict[str, Any]:
    """Restore a captured release manifest, with rehearsal by default."""
    source = _resolve_under(snapshot_path, RELEASE_SNAPSHOT_DIR)
    if not source.is_file():
        return {"status": "missing", "path": str(source), "dry_run": dry_run}
    payload = json.loads(source.read_text(encoding="utf-8"))
    manifest = payload.get("manifest") if isinstance(payload, dict) else None
    if not isinstance(manifest, dict):
        return {"status": "invalid", "path": str(source), "dry_run": dry_run, "reason": "snapshot has no manifest"}
    result = {
        "status": "ready" if dry_run else "ok",
        "dry_run": dry_run,
        "source": str(source),
        "target": str(RELEASES_PATH),
        "release": manifest.get("release"),
    }
    if dry_run:
        return result

    current_snapshot = snapshot_release(note=f"pre-rollback backup before restoring {source.name}")
    RELEASES_PATH.parent.mkdir(parents=True, exist_ok=True)
    RELEASES_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    result["pre_rollback_snapshot"] = current_snapshot
    return result


def monitoring_summary(trace_limit: int = 50) -> dict[str, Any]:
    """Small operational dashboard summary for rehearsal and daily checks."""
    try:
        from orchestrator import governance, trace

        traces = trace.read_traces(limit=trace_limit)
        error_traces = [
            t for t in traces
            if t.get("errors") or t.get("error") or any(e.get("kind") == "error" for e in t.get("events", []))
        ]
        approvals = governance.pending()
    except Exception as exc:
        return {"status": "degraded", "error": str(exc), "database": migration_status()}
    return {
        "status": "ok" if not error_traces else "attention",
        "database": migration_status(),
        "pending_approvals": len(approvals.get("items", [])),
        "recent_traces": len(traces),
        "recent_error_traces": len(error_traces),
        "latest_errors": error_traces[:5],
    }


def operational_rehearsal() -> dict[str, Any]:
    """Report the hardening checklist needed before production operation."""
    db = migration_status()
    backups = list_backups(limit=1)
    release_manifest = releases()
    monitoring = monitoring_summary(trace_limit=25)
    checks = [
        {"name": "schema_migrations_current", "ok": db.get("schema_version") == SCHEMA_VERSION and not db.get("error")},
        {"name": "recent_database_backup_available", "ok": bool(backups)},
        {"name": "release_manifest_available", "ok": bool(release_manifest.get("release"))},
        {"name": "rbac_keys_configured", "ok": bool(os.getenv("RBAC_ROLE_KEYS") or os.getenv("API_KEY") or os.getenv("ADMIN_API_KEY"))},
        {"name": "monitoring_readable", "ok": monitoring.get("status") in {"ok", "attention"}},
    ]
    return {
        "status": "ready" if all(c["ok"] for c in checks) else "needs_attention",
        "checks": checks,
        "next_actions": [c["name"] for c in checks if not c["ok"]],
        "monitoring": monitoring,
    }


def status() -> dict[str, Any]:
    return {
        "database": {
            "path": str(DB_PATH),
            "exists": DB_PATH.exists(),
            "bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
            **migration_status(),
        },
        "backups": list_backups(limit=5),
        "releases": releases(),
        "rbac": {
            "enabled": bool(os.getenv("RBAC_ROLE_KEYS")),
            "fallback_keys": bool(os.getenv("API_KEY") or os.getenv("ADMIN_API_KEY")),
        },
    }
