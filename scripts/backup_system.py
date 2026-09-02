#!/usr/bin/env python
"""Full-system backup for the agentic-rag stack (Stage 1 item 4).

What /ops/backup already does: copies data/media.db into logs/db_backups,
on the same disk, inside the repo. That protects against one file being
corrupted. It does not protect against losing the drive, and it leaves
out most of what the system actually needs to come back.

This captures the whole restorable surface:
  - every SQLite database under data/, not just media.db
  - every Qdrant collection (the entire retrieval layer)
  - .env and config/ (what the stack needs to start)
  - the cost ledger and routing decisions the daemon and evals read
and writes it to a root on a DIFFERENT drive, with a manifest of
sha256 checksums so a restore can be verified rather than assumed.

SECRETS: .env holds live API keys, so the archive is sensitive. Keep the
backup root out of any shared folder, or pass --no-secrets to omit it.

Usage:
    python scripts/backup_system.py
    python scripts/backup_system.py --keep 14
    python scripts/backup_system.py --roots G:\\Backups\\agentic-rag
    python scripts/backup_system.py --no-secrets
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOTS = [r"G:\Backups\agentic-rag"]
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")

# Logs worth keeping: small, append-only, and load-bearing for the budget
# breaker, the routing eval and the measure/learn loop.
LOG_FILES = [
    "cost_log.jsonl",
    "routing_decisions.jsonl",
    "kb_index_runs.jsonl",
    "kb_misses.jsonl",
    "traces.jsonl",
]


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def copy_sqlite(src: Path, dest: Path) -> dict:
    """Copy a live SQLite DB using the online backup API.

    A plain file copy of a WAL-mode database can capture a torn state:
    the .db without the committed tail still sitting in the -wal file.
    con.backup() takes a consistent snapshot while writers stay active,
    which matters because the daemon writes to these while this runs.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        dest_con = sqlite3.connect(dest)
        try:
            src_con.backup(dest_con)
        finally:
            dest_con.close()
    finally:
        src_con.close()
    return {"source": str(src), "bytes": dest.stat().st_size}


def capture_databases(staging: Path) -> list[dict]:
    out = []
    data_dir = PROJECT_ROOT / "data"
    if not data_dir.is_dir():
        return out
    for db in sorted(data_dir.glob("*.db")):
        try:
            info = copy_sqlite(db, staging / "db" / db.name)
            info["name"] = db.name
            info["status"] = "ok"
        except Exception as exc:  # a locked or corrupt DB must not kill the run
            info = {"name": db.name, "status": "error", "error": str(exc)}
        out.append(info)
    return out


def capture_qdrant(staging: Path) -> list[dict]:
    """Snapshot every collection and pull the file down over HTTP.

    Snapshots are created inside the container's storage volume, so they
    are only a backup once downloaded off it. Each one is deleted server
    side afterwards so repeated runs don't grow the volume without bound.
    """
    try:
        import httpx
    except ImportError:
        return [{"status": "error", "error": "httpx not installed"}]

    out: list[dict] = []
    try:
        with httpx.Client(base_url=QDRANT_URL, timeout=300.0) as client:
            resp = client.get("/collections")
            resp.raise_for_status()
            names = [c["name"] for c in resp.json()["result"]["collections"]]
    except Exception as exc:
        return [{"status": "error", "error": f"cannot reach Qdrant at {QDRANT_URL}: {exc}"}]

    dest_dir = staging / "qdrant"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        entry: dict = {"name": name}
        try:
            with httpx.Client(base_url=QDRANT_URL, timeout=600.0) as client:
                created = client.post(f"/collections/{name}/snapshots")
                created.raise_for_status()
                snap = created.json()["result"]["name"]
                target = dest_dir / f"{name}.snapshot"
                with client.stream("GET", f"/collections/{name}/snapshots/{snap}") as stream:
                    stream.raise_for_status()
                    with target.open("wb") as fh:
                        for chunk in stream.iter_bytes(1024 * 1024):
                            fh.write(chunk)
                try:
                    client.delete(f"/collections/{name}/snapshots/{snap}")
                except Exception:
                    pass  # leaving one behind is untidy, not a failure
                entry.update(status="ok", bytes=target.stat().st_size)
        except Exception as exc:
            entry.update(status="error", error=str(exc))
        out.append(entry)
    return out


def capture_config(staging: Path, include_secrets: bool) -> list[dict]:
    out = []
    dest = staging / "config"
    dest.mkdir(parents=True, exist_ok=True)

    if include_secrets:
        env = PROJECT_ROOT / ".env"
        if env.is_file():
            shutil.copy2(env, dest / ".env")
            out.append({"name": ".env", "status": "ok", "bytes": env.stat().st_size})
    else:
        out.append({"name": ".env", "status": "skipped", "reason": "--no-secrets"})

    config_dir = PROJECT_ROOT / "config"
    if config_dir.is_dir():
        target = dest / "config"
        shutil.copytree(config_dir, target, dirs_exist_ok=True)
        out.append({"name": "config/", "status": "ok",
                    "files": sum(1 for _ in target.rglob("*") if _.is_file())})
    return out


def capture_logs(staging: Path) -> list[dict]:
    out = []
    logs_dir = PROJECT_ROOT / "logs"
    dest = staging / "logs"
    dest.mkdir(parents=True, exist_ok=True)
    for name in LOG_FILES:
        src = logs_dir / name
        if src.is_file():
            shutil.copy2(src, dest / name)
            out.append({"name": name, "status": "ok", "bytes": src.stat().st_size})
    return out


def build_manifest(staging: Path, sections: dict, include_secrets: bool) -> dict:
    files = []
    for path in sorted(staging.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files.append({
                "path": path.relative_to(staging).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": _sha256(path),
            })
    return {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "project_root": str(PROJECT_ROOT),
        "host": os.getenv("COMPUTERNAME") or socket.gethostname(),
        "includes_secrets": include_secrets,
        "sections": sections,
        "files": files,
        "total_bytes": sum(f["bytes"] for f in files),
    }


def prune(root: Path, keep: int) -> list[str]:
    archives = sorted(root.glob("agentic-rag-backup-*.zip"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in archives[keep:]:
        try:
            old.unlink()
            removed.append(old.name)
        except Exception:
            pass
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="Full-system backup for agentic-rag.")
    ap.add_argument("--roots", nargs="*", default=None,
                    help="Backup roots. Defaults to %s" % DEFAULT_ROOTS)
    ap.add_argument("--keep", type=int, default=10, help="Archives to retain per root.")
    ap.add_argument("--no-secrets", action="store_true", help="Omit .env from the archive.")
    args = ap.parse_args()

    include_secrets = not args.no_secrets
    roots = [Path(r) for r in (args.roots or DEFAULT_ROOTS)]
    stamp = _now_stamp()
    archive_name = f"agentic-rag-backup-{stamp}.zip"

    print(f"[backup] project={PROJECT_ROOT}")
    with tempfile.TemporaryDirectory(prefix="ragbackup-") as tmp:
        staging = Path(tmp) / "payload"
        staging.mkdir(parents=True)

        sections = {}
        print("[backup] databases...")
        sections["databases"] = capture_databases(staging)
        print("[backup] qdrant collections...")
        sections["qdrant"] = capture_qdrant(staging)
        print("[backup] config...")
        sections["config"] = capture_config(staging, include_secrets)
        print("[backup] logs...")
        sections["logs"] = capture_logs(staging)

        manifest = build_manifest(staging, sections, include_secrets)
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")

        staged_zip = Path(tmp) / archive_name
        with zipfile.ZipFile(staged_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(staging).as_posix())
        size_mb = staged_zip.stat().st_size / (1024 * 1024)
        print(f"[backup] archive built: {archive_name} ({size_mb:.1f} MB)")

        written, failed = [], []
        for root in roots:
            try:
                root.mkdir(parents=True, exist_ok=True)
                target = root / archive_name
                shutil.copy2(staged_zip, target)
                removed = prune(root, args.keep)
                written.append(str(target))
                print(f"[backup] wrote {target}"
                      + (f" (pruned {len(removed)})" if removed else ""))
            except Exception as exc:
                failed.append({"root": str(root), "error": str(exc)})
                print(f"[backup] FAILED writing to {root}: {exc}", file=sys.stderr)

    # Section-level problems are worth surfacing even when the zip landed:
    # a backup missing its Qdrant snapshots looks fine on disk and is not.
    problems = [
        f"{sec}:{item.get('name', '?')}"
        for sec, items in sections.items()
        for item in items
        if item.get("status") == "error"
    ]
    if problems:
        print("[backup] SECTIONS WITH ERRORS: " + ", ".join(problems), file=sys.stderr)
    if not written:
        print("[backup] no archive written to any root", file=sys.stderr)
        return 2
    if problems or failed:
        return 1
    print("[backup] ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
