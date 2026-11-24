"""
Google Drive: keep a text copy of one Drive folder so the brain can search it.

Decided 23 Nov 2026: index one folder, "WijerCo" (GOOGLE_DRIVE_FOLDER, a
name or a folder id). Drive is a personal/WijerCo source, so its content may
go to cloud models.

How it works:
  * The folder and its subfolders are listed through the Drive API
    (drive.readonly; nothing in Drive is changed).
  * Each file becomes one markdown file under GOOGLE_DRIVE_MIRROR (default
    C:\\dev\\drive-mirror next to the repo), keeping the folder structure:
      Google Docs    exported as markdown (plain text if markdown fails)
      Google Slides  exported as plain text
      Google Sheets  exported as CSV (first sheet)
      PDF, DOCX      text extracted locally (pypdf, python-docx)
      .md / .txt     copied
    Other types (images, video, archives) are skipped.
  * A file is re-exported only when its Drive modifiedTime changes. Files
    removed or moved out of the folder are removed from the copy.
  * rag/indexer.py indexes the copy into the vault collection under "drive/",
    so everything that searches the vault also finds Drive content.

The copy sits outside the repo and outside the nightly backup: it can always
be rebuilt from Drive.
"""

from __future__ import annotations

import io
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import records

_ROOT = Path(__file__).resolve().parent.parent
FOLDER = os.getenv("GOOGLE_DRIVE_FOLDER", "WijerCo").strip()
MIRROR = Path(os.getenv("GOOGLE_DRIVE_MIRROR", "").strip() or str(_ROOT.parent / "drive-mirror"))
MAX_BYTES = int(os.getenv("GOOGLE_DRIVE_MAX_MB", "20")) * 1024 * 1024
MAX_FILES = int(os.getenv("GOOGLE_DRIVE_MAX_FILES", "2000"))
MAX_CHARS = 200_000

FOLDER_MIME = "application/vnd.google-apps.folder"
EXPORTS = {
    "application/vnd.google-apps.document": ["text/markdown", "text/plain"],
    "application/vnd.google-apps.presentation": ["text/plain"],
    "application/vnd.google-apps.spreadsheet": ["text/csv"],
}
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_FIELDS = "nextPageToken, files(id, name, mimeType, modifiedTime, size, webViewLink, parents)"


def _safe(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:120] or "untitled"


def find_folder(svc: Any, name_or_id: str = FOLDER) -> dict | None:
    """The folder to mirror: an id, else a folder with this exact name.
    With several matches, one at the top of My Drive wins."""
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", name_or_id):
        try:
            f = svc.files().get(fileId=name_or_id, fields="id, name, mimeType").execute()
            if f.get("mimeType") == FOLDER_MIME:
                return f
        except Exception:
            pass
    esc = name_or_id.replace("\\", "\\\\").replace("'", "\\'")
    q = f"name = '{esc}' and mimeType = '{FOLDER_MIME}' and trashed = false"
    found = svc.files().list(q=q, fields="files(id, name, parents)", pageSize=20, spaces="drive").execute().get("files", [])
    if not found:
        return None
    if len(found) > 1:
        try:
            root = svc.files().get(fileId="root", fields="id").execute().get("id")
            top = [f for f in found if root in (f.get("parents") or [])]
            if top:
                return top[0]
        except Exception:
            pass
    return found[0]


def walk(svc: Any, folder_id: str, prefix: str = "") -> Iterator[tuple[str, dict]]:
    """Yield (relative folder path, file) for every non-folder file below."""
    page = None
    while True:
        resp = svc.files().list(q=f"'{folder_id}' in parents and trashed = false", fields=_FIELDS,
                                pageSize=200, pageToken=page, spaces="drive").execute()
        for f in resp.get("files", []):
            if f.get("mimeType") == FOLDER_MIME:
                yield from walk(svc, f["id"], f"{prefix}{_safe(f['name'])}/")
            else:
                yield prefix, f
        page = resp.get("nextPageToken")
        if not page:
            return


def _bytes(data: Any) -> bytes:
    return data if isinstance(data, (bytes, bytearray)) else str(data or "").encode("utf-8")


def text_for(svc: Any, f: dict) -> str | None:
    """Text of one Drive file, or None for a type that is not indexed."""
    mime, fid, name = f.get("mimeType", ""), f["id"], f.get("name", "")
    if int(f.get("size") or 0) > MAX_BYTES:
        return None
    if mime in EXPORTS:
        last: Exception | None = None
        for target in EXPORTS[mime]:
            try:
                return _bytes(svc.files().export(fileId=fid, mimeType=target).execute()).decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001 - try the next format
                last = exc
        raise last or RuntimeError("export failed")
    if mime == PDF:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(_bytes(svc.files().get_media(fileId=fid).execute())))
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)
    if mime == DOCX:
        import docx
        doc = docx.Document(io.BytesIO(_bytes(svc.files().get_media(fileId=fid).execute())))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if mime.startswith("text/") or name.lower().endswith((".md", ".txt")):
        return _bytes(svc.files().get_media(fileId=fid).execute()).decode("utf-8", "replace")
    return None


def _note(f: dict, rel_dir: str, text: str) -> str:
    title = f.get("name", "untitled").replace('"', "'")
    return ("---\n"
            f'title: "{title}"\n'
            "source: google_drive\n"
            f"drive_id: {f['id']}\n"
            f"drive_path: \"{FOLDER}/{rel_dir}{title}\"\n"
            f"modified: {f.get('modifiedTime', '')}\n"
            f"link: {f.get('webViewLink', '')}\n"
            "---\n\n"
            f"# {f.get('name', 'untitled')}\n\n{text.strip()[:MAX_CHARS]}\n")


def sync_drive(svc: Any = None, *, folder: str = FOLDER, mirror: Path | None = None) -> dict[str, Any]:
    from .google_link import service
    svc = svc or service("drive", "v3")
    mirror = mirror or MIRROR
    out: dict[str, Any] = {"folder": folder, "files": 0, "written": 0, "unchanged": 0, "skipped": 0,
                           "removed": 0, "errors": 0}
    top = find_folder(svc, folder)
    if not top:
        out["error"] = f"no Drive folder named {folder!r}"
        return out
    base = mirror / _safe(top["name"])
    base.mkdir(parents=True, exist_ok=True)
    state: dict[str, dict] = records.get_kv("drive_state", {}) or {}
    seen: set[str] = set()
    used: set[str] = set()
    for rel_dir, f in walk(svc, top["id"]):
        if out["files"] >= MAX_FILES:
            out["truncated"] = True
            break
        out["files"] += 1
        fid = f["id"]
        seen.add(fid)
        stem = _safe(re.sub(r"\.(md|txt|pdf|docx)$", "", f.get("name", ""), flags=re.I))
        rel = f"{rel_dir}{stem}.md"
        if rel.lower() in used:  # two Drive files with the same name in one folder
            rel = f"{rel_dir}{stem} ({fid[:6]}).md"
        used.add(rel.lower())
        old = state.get(fid) or {}
        target = base / rel
        if old.get("modified") == f.get("modifiedTime") and old.get("path") == rel and (target.exists() or old.get("skipped")):
            out["unchanged"] += 1
            continue
        try:
            text = text_for(svc, f)
        except Exception as exc:  # noqa: BLE001 - one file must not stop the folder
            out["errors"] += 1
            out.setdefault("error_files", []).append(f"{rel}: {str(exc)[:120]}")
            continue
        if old.get("path") and old["path"] != rel:
            (base / old["path"]).unlink(missing_ok=True)
        if not text or not text.strip():
            out["skipped"] += 1
            state[fid] = {"modified": f.get("modifiedTime"), "path": rel, "skipped": True}
            target.unlink(missing_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_note(f, rel_dir, text), encoding="utf-8")
        state[fid] = {"modified": f.get("modifiedTime"), "path": rel}
        out["written"] += 1
    if not out.get("truncated"):
        for fid in [k for k in state if k not in seen]:
            path = state.pop(fid).get("path")
            if path:
                (base / path).unlink(missing_ok=True)
                out["removed"] += 1
    records.set_kv("drive_state", state)
    records.set_kv("drive_last_sync", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    return out
