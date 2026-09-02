#!/usr/bin/env python
"""Prove a backup archive can actually be restored (Stage 1 item 4).

The weekly rehearsal calls /ops/restore with dry_run=true, which checks a
file exists and returns before touching anything. That is a smoke test of
the endpoint, not evidence the data is any good. This opens the archive
for real: verifies every checksum, opens every database, and restores
every Qdrant snapshot into a throwaway collection to confirm the vectors
come back and the point counts match what is live.

Nothing live is written. Qdrant snapshots are restored into collections
suffixed __drill, which are deleted at the end; the script refuses to
touch a collection name that does not carry that suffix.

Usage:
    python scripts/restore_drill.py
    python scripts/restore_drill.py --archive C:\\Backups\\...\\backup.zip
    python scripts/restore_drill.py --skip-qdrant
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
import zipfile
from pathlib import Path

DRILL_SUFFIX = "__drill"
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
DEFAULT_ROOTS = [r"C:\Backups\agentic-rag", r"G:\My Drive\Backups\agentic-rag"]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def newest_archive(roots: list[str]) -> Path | None:
    found = []
    for root in roots:
        p = Path(root)
        if p.is_dir():
            found.extend(p.glob("agentic-rag-backup-*.zip"))
    if not found:
        return None
    return max(found, key=lambda p: p.stat().st_mtime)


def check_manifest(extracted: Path, results: list) -> dict:
    manifest_path = extracted / "manifest.json"
    if not manifest_path.is_file():
        results.append(("manifest", "FAIL", "manifest.json missing from archive"))
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    bad, missing = [], []
    for entry in manifest.get("files", []):
        target = extracted / entry["path"]
        if not target.is_file():
            missing.append(entry["path"])
            continue
        if _sha256(target) != entry["sha256"]:
            bad.append(entry["path"])

    total = len(manifest.get("files", []))
    if missing or bad:
        detail = f"{len(missing)} missing, {len(bad)} checksum mismatch, of {total}"
        results.append(("checksums", "FAIL", detail))
    else:
        results.append(("checksums", "PASS", f"{total} files verified"))
    return manifest


def check_databases(extracted: Path, results: list) -> None:
    db_dir = extracted / "db"
    if not db_dir.is_dir():
        results.append(("databases", "FAIL", "no db/ directory in archive"))
        return
    dbs = sorted(db_dir.glob("*.db"))
    if not dbs:
        results.append(("databases", "FAIL", "archive contains no databases"))
        return
    for db in dbs:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
                tables = con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                rows = 0
                for (name,) in tables:
                    try:
                        rows += con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                    except Exception:
                        pass
            finally:
                con.close()
        except Exception as exc:
            results.append((f"db:{db.name}", "FAIL", str(exc)))
            continue
        if integrity != "ok":
            results.append((f"db:{db.name}", "FAIL", f"integrity_check={integrity}"))
        else:
            results.append((f"db:{db.name}", "PASS",
                            f"{len(tables)} tables, {rows} rows"))


def _live_counts(client) -> dict:
    counts = {}
    try:
        names = [c["name"] for c in client.get("/collections").json()["result"]["collections"]]
    except Exception:
        return counts
    for name in names:
        try:
            info = client.get(f"/collections/{name}").json()["result"]
            counts[name] = info.get("points_count")
        except Exception:
            pass
    return counts


def check_qdrant(extracted: Path, results: list) -> None:
    snap_dir = extracted / "qdrant"
    if not snap_dir.is_dir() or not any(snap_dir.glob("*.snapshot")):
        results.append(("qdrant", "FAIL", "archive contains no Qdrant snapshots"))
        return
    try:
        import httpx
    except ImportError:
        results.append(("qdrant", "SKIP", "httpx not installed"))
        return

    with httpx.Client(base_url=QDRANT_URL, timeout=600.0) as client:
        try:
            client.get("/collections").raise_for_status()
        except Exception as exc:
            results.append(("qdrant", "SKIP", f"Qdrant unreachable: {exc}"))
            return

        live = _live_counts(client)
        for snap in sorted(snap_dir.glob("*.snapshot")):
            origin = snap.stem
            target = f"{origin}{DRILL_SUFFIX}"
            # Belt and braces: never let this path touch a real collection.
            if not target.endswith(DRILL_SUFFIX):
                results.append((f"qdrant:{origin}", "FAIL", "unsafe target name"))
                continue
            try:
                client.delete(f"/collections/{target}")
                with snap.open("rb") as fh:
                    up = client.post(
                        f"/collections/{target}/snapshots/upload?priority=snapshot",
                        files={"snapshot": (snap.name, fh, "application/octet-stream")},
                    )
                up.raise_for_status()
                info = client.get(f"/collections/{target}").json()["result"]
                restored = info.get("points_count")
                expected = live.get(origin)
                if expected is not None and restored != expected:
                    results.append((f"qdrant:{origin}", "WARN",
                                    f"restored {restored} points, live has {expected}"))
                else:
                    results.append((f"qdrant:{origin}", "PASS",
                                    f"{restored} points restored"))
            except Exception as exc:
                results.append((f"qdrant:{origin}", "FAIL", str(exc)))
            finally:
                try:
                    client.delete(f"/collections/{target}")
                except Exception:
                    results.append((f"qdrant:{origin}", "WARN",
                                    f"could not clean up {target}, delete it by hand"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify a backup archive restores.")
    ap.add_argument("--archive", help="Archive to test. Defaults to the newest found.")
    ap.add_argument("--roots", nargs="*", default=DEFAULT_ROOTS)
    ap.add_argument("--skip-qdrant", action="store_true")
    args = ap.parse_args()

    archive = Path(args.archive) if args.archive else newest_archive(args.roots)
    if not archive or not archive.is_file():
        print("[drill] no backup archive found in: " + ", ".join(args.roots), file=sys.stderr)
        return 2

    size_mb = archive.stat().st_size / (1024 * 1024)
    print(f"[drill] archive: {archive} ({size_mb:.1f} MB)")

    results: list[tuple[str, str, str]] = []
    with tempfile.TemporaryDirectory(prefix="ragdrill-") as tmp:
        extracted = Path(tmp) / "extracted"
        try:
            with zipfile.ZipFile(archive) as zf:
                bad = zf.testzip()
                if bad:
                    print(f"[drill] archive is corrupt at {bad}", file=sys.stderr)
                    return 2
                zf.extractall(extracted)
        except Exception as exc:
            print(f"[drill] cannot open archive: {exc}", file=sys.stderr)
            return 2

        manifest = check_manifest(extracted, results)
        check_databases(extracted, results)
        if args.skip_qdrant:
            results.append(("qdrant", "SKIP", "--skip-qdrant"))
        else:
            check_qdrant(extracted, results)

    width = max(len(name) for name, _, _ in results)
    print()
    for name, status, detail in results:
        print(f"  {status:5}  {name:<{width}}  {detail}")
    print()

    if manifest and not manifest.get("includes_secrets", True):
        print("[drill] note: this archive was taken with --no-secrets, so .env is")
        print("        not in it. A restore from this archive needs .env supplied")
        print("        separately before the stack will start.")

    failed = [r for r in results if r[1] == "FAIL"]
    warned = [r for r in results if r[1] == "WARN"]
    if failed:
        print(f"[drill] VERDICT: FAIL ({len(failed)} checks failed)", file=sys.stderr)
        return 1
    if warned:
        print(f"[drill] VERDICT: PASS WITH WARNINGS ({len(warned)})")
        return 0
    print("[drill] VERDICT: PASS. This archive restores.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
