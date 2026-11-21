"""Retiring a stale plan: archive it, cancel open tasks, keep completed work."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture()
def ops(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DB_PATH", str(tmp_path / "media.db"))
    import orchestrator.operating as operating
    return importlib.reload(operating)


def test_cancel_plan_archives_and_cancels_open_tasks(ops):
    plan_id = ops.create_plan("Stale weekly plan", goal="old goal")
    done = ops.add_task(plan_id, "finished step")
    ops.update_task(done, status="done")
    gone = ops.add_task(plan_id, "already cancelled")
    ops.update_task(gone, status="cancelled")
    open_ids = [ops.add_task(plan_id, f"open {s}", status=s) for s in ("todo", "blocked", "waiting_approval")]

    result = ops.cancel_plan(plan_id, actor="test", reason="superseded")

    assert result == {"plan_id": plan_id, "status": "archived", "cancelled_tasks": 3, "actor": "test"}
    after = ops.get_plan(plan_id)
    assert after["status"] == "archived"
    by_id = {t["task_id"]: t["status"] for t in after["tasks"]}
    assert by_id[done] == "done"
    assert by_id[gone] == "cancelled"
    assert all(by_id[t] == "cancelled" for t in open_ids)
    assert after["meta"]["cancelled"]["reason"] == "superseded"


def test_cancel_plan_unknown_raises(ops):
    with pytest.raises(KeyError):
        ops.cancel_plan("no-such-plan")
