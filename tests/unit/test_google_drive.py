"""Drive: a text copy of one folder, refreshed only when files change."""

from __future__ import annotations

import io

import pytest

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeDrive:
    """files: id -> dict(name, mimeType, parents, modifiedTime, body?)"""

    def __init__(self, files, markdown_fails=False):
        self.files_, self.exports, self.media = files, [], []
        self.markdown_fails = markdown_fails

    def files(self):
        outer = self

        class F:
            def get(self, fileId, fields=None):
                if fileId == "root":
                    return _Call(lambda: {"id": "root"})
                return _Call(lambda: {"id": fileId, **outer.files_[fileId]})

            def list(self, q, fields=None, pageSize=None, pageToken=None, spaces=None):
                def run():
                    if q.startswith("name = "):
                        name = q.split("'")[1]
                        return {"files": [{"id": i, **f} for i, f in outer.files_.items()
                                          if f["name"] == name and f["mimeType"] == FOLDER]}
                    parent = q.split("'")[1]
                    return {"files": [{"id": i, **f} for i, f in outer.files_.items() if parent in f.get("parents", [])]}
                return _Call(run)

            def export(self, fileId, mimeType):
                def run():
                    outer.exports.append((fileId, mimeType))
                    if mimeType == "text/markdown" and outer.markdown_fails:
                        raise RuntimeError("markdown export not available")
                    return outer.files_[fileId]["body"].encode()
                return _Call(run)

            def get_media(self, fileId):
                def run():
                    outer.media.append(fileId)
                    return outer.files_[fileId]["body"]
                return _Call(run)
        return F()


def _docx_bytes(text: str) -> bytes:
    import docx
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


@pytest.fixture
def drive_env(tmp_path, monkeypatch):
    import orchestrator.records as records
    monkeypatch.setattr(records, "DB_PATH", tmp_path / "records.db")
    from orchestrator import google_drive
    files = {
        "top": {"name": "WijerCo", "mimeType": FOLDER, "parents": ["root"], "modifiedTime": "t0"},
        "old": {"name": "WijerCo", "mimeType": FOLDER, "parents": ["elsewhere"], "modifiedTime": "t0"},
        "sub": {"name": "Clients", "mimeType": FOLDER, "parents": ["top"], "modifiedTime": "t0"},
        "d1": {"name": "Services", "mimeType": DOC, "parents": ["top"], "modifiedTime": "t1",
               "webViewLink": "https://docs.example/d1", "body": "We run diagnostic sprints."},
        "d2": {"name": "Brief.docx", "mimeType": DOCX, "parents": ["sub"], "modifiedTime": "t1",
               "body": _docx_bytes("Client brief text.")},
        "img": {"name": "logo.png", "mimeType": "image/png", "parents": ["top"], "modifiedTime": "t1", "body": b"x"},
    }
    return google_drive, files, tmp_path / "mirror"


def test_first_sync_writes_text_copies_in_folder_structure(drive_env):
    gd, files, mirror = drive_env
    svc = FakeDrive(files)
    out = gd.sync_drive(svc, folder="WijerCo", mirror=mirror)
    assert out["written"] == 2 and out["skipped"] == 1 and out["files"] == 3 and out["errors"] == 0
    services = (mirror / "WijerCo" / "Services.md").read_text(encoding="utf-8")
    assert "drive_id: d1" in services and "We run diagnostic sprints." in services
    assert "link: https://docs.example/d1" in services
    assert "Client brief text." in (mirror / "WijerCo" / "Clients" / "Brief.md").read_text(encoding="utf-8")
    assert not list(mirror.rglob("logo*"))


def test_second_sync_skips_unchanged_and_refreshes_changed(drive_env):
    gd, files, mirror = drive_env
    gd.sync_drive(FakeDrive(files), folder="WijerCo", mirror=mirror)
    files["d1"] = {**files["d1"], "modifiedTime": "t2", "body": "Updated services."}
    svc = FakeDrive(files)
    out = gd.sync_drive(svc, folder="WijerCo", mirror=mirror)
    assert out["written"] == 1 and out["unchanged"] == 2
    assert svc.exports == [("d1", "text/markdown")] and svc.media == []
    assert "Updated services." in (mirror / "WijerCo" / "Services.md").read_text(encoding="utf-8")


def test_removed_files_leave_the_copy(drive_env):
    gd, files, mirror = drive_env
    gd.sync_drive(FakeDrive(files), folder="WijerCo", mirror=mirror)
    del files["d2"]
    out = gd.sync_drive(FakeDrive(files), folder="WijerCo", mirror=mirror)
    assert out["removed"] == 1
    assert not (mirror / "WijerCo" / "Clients" / "Brief.md").exists()


def test_markdown_export_falls_back_to_plain_text(drive_env):
    gd, files, mirror = drive_env
    svc = FakeDrive(files, markdown_fails=True)
    gd.sync_drive(svc, folder="WijerCo", mirror=mirror)
    assert ("d1", "text/plain") in svc.exports


def test_missing_folder_is_reported(drive_env):
    gd, files, mirror = drive_env
    out = gd.sync_drive(FakeDrive(files), folder="Nope", mirror=mirror)
    assert "no Drive folder" in out["error"]


def test_run_once_reindexes_only_when_the_drive_copy_changed(tmp_path, monkeypatch):
    import asyncio
    import orchestrator.records as records
    monkeypatch.setattr(records, "DB_PATH", tmp_path / "records.db")
    from orchestrator import google_drive, google_sync
    monkeypatch.setattr(google_sync, "sync_calendar", lambda: {"events": 0})
    monkeypatch.setattr(google_sync, "sync_gmail", lambda: {"listed": 0})
    calls = []

    async def fake_reindex():
        calls.append(1)
        return {"status": "ok"}

    monkeypatch.setattr(google_sync, "_reindex_vault", fake_reindex)
    monkeypatch.setattr(google_drive, "sync_drive", lambda: {"written": 2, "removed": 0})
    out = asyncio.run(google_sync.run_once(extract=False, drive=True))
    assert out["reindex"] == {"status": "ok"} and calls == [1]
    monkeypatch.setattr(google_drive, "sync_drive", lambda: {"written": 0, "removed": 0, "unchanged": 5})
    out = asyncio.run(google_sync.run_once(extract=False, drive=True))
    assert "reindex" not in out and calls == [1]
    out = asyncio.run(google_sync.run_once(extract=False, drive=False))
    assert "drive" not in out
