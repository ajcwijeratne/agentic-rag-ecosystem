from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

import orchestrator.daemon as daemon_ctl
import orchestrator.schedule_registry as reg


# --- cron -------------------------------------------------------------------

def test_cron_next_weekdays_skips_weekend():
    fri_evening = datetime(2026, 10, 2, 18, 0)  # Friday
    assert reg.cron_next("30 6 * * 1-5", fri_evening) == datetime(2026, 10, 5, 6, 30)


def test_cron_next_same_day_and_monthly():
    mon_early = datetime(2026, 9, 28, 4, 0)
    assert reg.cron_next("0 5 * * 1", mon_early) == datetime(2026, 9, 28, 5, 0)
    assert reg.cron_next("0 8 1 * *", mon_early) == datetime(2026, 10, 1, 8, 0)
    assert reg.cron_next("0 2 * * 0", mon_early) == datetime(2026, 10, 4, 2, 0)
    assert reg.cron_next("*/15 * * * *", datetime(2026, 9, 28, 4, 7)) == datetime(2026, 9, 28, 4, 15)


def test_cron_next_rejects_garbage():
    assert reg.cron_next("not a cron", datetime(2026, 1, 1)) is None


def test_describe_cron():
    assert reg.describe_cron("30 6 * * 1-5") == "Weekdays · 6:30am"
    assert reg.describe_cron("0 7 * * 5") == "Fri · 7:00am"
    assert reg.describe_cron("0 2 * * 0") == "Sun · 2:00am"
    assert reg.describe_cron("0 8 1 * *") == "Monthly · day 1 · 8:00am"
    assert reg.describe_cron("0 23 * * *") == "Daily · 11:00pm"
    assert reg.describe_cron("*/5 * * * *") == "cron */5 * * * *"


def test_fmt_next():
    now = datetime(2026, 9, 28, 6, 0)
    assert reg.fmt_next(datetime(2026, 9, 28, 20, 30), now) == "Today 8:30pm"
    assert reg.fmt_next(datetime(2026, 9, 29, 0, 5), now) == "Tomorrow 12:05am"
    assert reg.fmt_next(datetime(2026, 10, 2, 7, 0), now) == "Fri 7:00am"
    assert reg.fmt_next(datetime(2026, 10, 12, 7, 0), now) == "12 Oct 7:00am"


# --- daemon -----------------------------------------------------------------

@pytest.fixture
def daemon_files(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon_ctl, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(daemon_ctl, "LOG_PATH", tmp_path / "daemon.jsonl")
    monkeypatch.setattr(daemon_ctl, "HEARTBEAT_PATH", tmp_path / "heartbeat")
    monkeypatch.setattr(daemon_ctl, "INTERVAL_SEC", 60.0)
    return tmp_path


def _beat(path, when: datetime):
    path.write_text("x", encoding="utf-8")
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def _by_name(rows):
    return {r["name"]: r for r in rows}


def test_daemon_stopped_marks_every_job_failed(daemon_files):
    now = datetime.now(timezone.utc)
    _beat(daemon_files / "heartbeat", now - timedelta(days=24))
    rows = _by_name(reg.daemon_rows(now))
    assert rows["Operating daemon"]["st"] == "st-fail"
    assert "24 days ago" in rows["Operating daemon"]["detail"]
    for name in ("Memory consolidation", "Monthly review"):
        assert rows[name]["stl"] == "Daemon stopped"
        assert rows[name]["next"] == "when daemon restarts"


def test_daemon_running_ok_overdue_and_failed(daemon_files):
    now = datetime.now(timezone.utc)
    local = now.astimezone().replace(tzinfo=None)
    _beat(daemon_files / "heartbeat", now - timedelta(seconds=20))
    iso = local.isocalendar()
    state = {
        "paused": False, "cycles": 12,
        # consolidation is current whatever the hour: ran today and yesterday both count
        "consolidated_date": local.strftime("%Y-%m-%d"),
        "measured_date": "2020-01-01",  # long overdue
        "initiative_week": f"{iso[0]}-W{iso[1]:02d}",
        "learned_month": local.strftime("%Y-%m"),
        "reviewed_month": local.strftime("%Y-%m"),
    }
    daemon_ctl.STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    ts = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    later = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    events = [
        {"ts": ts, "action": "consolidation"},
        {"ts": ts, "action": "learning_reflection"},
        {"ts": ts, "action": "monthly_review"},
        {"ts": later, "action": "review_error", "error": "report dir missing"},
    ]
    daemon_ctl.LOG_PATH.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    rows = _by_name(reg.daemon_rows(now))
    assert rows["Operating daemon"]["stl"] == "Running"
    assert rows["Memory consolidation"]["stl"] == "OK"
    assert rows["Memory consolidation"]["last"] is not None
    assert rows["Outcome measure sweep"]["stl"] == "Overdue"
    assert rows["Monthly learning reflection"]["stl"] == "OK"
    assert rows["Monthly review"]["stl"] == "Failed"
    assert "report dir missing" in rows["Monthly review"]["detail"]


def test_daemon_paused(daemon_files):
    now = datetime.now(timezone.utc)
    _beat(daemon_files / "heartbeat", now)
    daemon_ctl.STATE_PATH.write_text(json.dumps({"paused": True, "paused_by": "cowork"}), encoding="utf-8")
    rows = _by_name(reg.daemon_rows(now))
    assert rows["Operating daemon"]["stl"] == "Paused"
    assert rows["Memory consolidation"]["st"] == "st-warn"


# --- n8n --------------------------------------------------------------------

def _wf(name, cron, **extra):
    return {"name": name, "nodes": [{"type": "n8n-nodes-base.scheduleTrigger", "parameters": {
        "rule": {"interval": [{"field": "cronExpression", "expression": cron}]}}}], **extra}


def test_n8n_repo_definitions_are_unverified_without_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("N8N_API_KEY", raising=False)
    (tmp_path / "a.json").write_text(json.dumps(_wf("Morning", "30 6 * * 1-5")), encoding="utf-8")
    (tmp_path / "hook.json").write_text(json.dumps({"name": "Hook", "nodes": [{"type": "n8n-nodes-base.webhook"}]}),
                                        encoding="utf-8")
    monkeypatch.setattr(reg, "N8N_DEF_DIRS", [tmp_path])
    monkeypatch.setattr(reg, "_ROOT", tmp_path)
    rows = reg.n8n_rows()
    assert [r["name"] for r in rows] == ["Morning"]
    assert rows[0]["stl"] == "Unverified" and rows[0]["st"] == "st-mute"
    assert rows[0]["when"].startswith("Weekdays · 6:30am")
    assert "N8N_API_KEY" in rows[0]["detail"]


def test_n8n_next_run_respects_daylight_saving(tmp_path, monkeypatch):
    # Melbourne moves to AEDT on 4 Oct 2026; a Monday 05:00 cron must stay 05:00.
    from zoneinfo import ZoneInfo
    monkeypatch.delenv("N8N_API_KEY", raising=False)
    monkeypatch.setattr(reg, "N8N_TIMEZONE", "Australia/Melbourne")
    (tmp_path / "a.json").write_text(json.dumps(_wf("Weekly", "0 5 * * 1")), encoding="utf-8")
    monkeypatch.setattr(reg, "N8N_DEF_DIRS", [tmp_path])
    monkeypatch.setattr(reg, "_ROOT", tmp_path)
    rows = reg.n8n_rows(datetime(2026, 9, 28, 1, 0, tzinfo=timezone.utc))  # Mon 11:00 AEST
    raw = datetime.fromisoformat(rows[0]["next_at"])
    nxt = raw.astimezone(ZoneInfo("Australia/Melbourne"))
    assert (nxt.month, nxt.day, nxt.hour) == (10, 5, 5)
    # Shown in local time using the offset in force on that date, not today's.
    assert raw.utcoffset() == raw.astimezone().utcoffset()


def test_n8n_live_status(monkeypatch):
    monkeypatch.setenv("N8N_API_KEY", "k")
    live = [
        {"name": "Good", "crons": ["0 5 * * 1"], "active": True, "tz": None,
         "last": {"status": "success", "startedAt": "2026-09-21T19:00:00.000Z"}},
        {"name": "Bad", "crons": ["0 6 * * 1"], "active": True, "tz": "Australia/Melbourne",
         "last": {"status": "error", "startedAt": "2026-09-21T20:00:00.000Z"}},
        {"name": "Off", "crons": ["0 7 * * 5"], "active": False, "tz": None, "last": None},
    ]
    monkeypatch.setattr(reg, "_n8n_live", lambda key: live)
    rows = _by_name(reg.n8n_rows())
    assert rows["Good"]["stl"] == "OK" and rows["Good"]["last"].startswith("2026-09-21T19:00")
    assert rows["Bad"]["st"] == "st-fail"
    assert rows["Off"]["stl"] == "Inactive" and rows["Off"]["next"] == "off"


def test_n8n_api_failure_falls_back_to_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("N8N_API_KEY", "k")
    (tmp_path / "a.json").write_text(json.dumps(_wf("Weekly", "0 5 * * 1")), encoding="utf-8")
    monkeypatch.setattr(reg, "N8N_DEF_DIRS", [tmp_path])
    monkeypatch.setattr(reg, "_ROOT", tmp_path)

    def boom(key):
        raise RuntimeError("401 unauthorized")
    monkeypatch.setattr(reg, "_n8n_live", boom)
    rows = reg.n8n_rows()
    assert rows[0]["stl"] == "Unverified"
    assert "401 unauthorized" in rows[0]["detail"]


# --- Windows Task Scheduler -------------------------------------------------

def test_windows_task_rows():
    now = datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)
    raw = [
        {"name": "WijerCo Watchdog", "state": "Ready", "desc": "Hourly check", "result": 0,
         "last": "2026-09-27T19:47:19.0000000Z", "next": "2026-09-27T20:47:18.0000000Z",
         "triggers": [{"kind": "MSFT_TaskTimeTrigger", "interval": "PT1H", "start": "2026-09-22T07:47:18+10:00"},
                      {"kind": "MSFT_TaskLogonTrigger"}]},
        {"name": "RAG Weekly Rehearsal", "state": "Ready", "result": 1,
         "last": "2026-09-21T19:30:00.0000000Z", "next": None,
         "triggers": [{"kind": "MSFT_TaskWeeklyTrigger", "days": 2, "start": "2026-09-01T05:30:00"}]},
        {"name": "RAG New", "state": "Ready", "result": 267011, "last": "1999-11-30T00:00:00Z",
         "triggers": [{"kind": "MSFT_TaskDailyTrigger", "start": "2026-09-01T23:00:00"}]},
        {"name": "RAG Off", "state": "Disabled", "result": 0, "last": None, "next": None, "triggers": []},
    ]
    rows = _by_name(reg.windows_task_rows(raw, now))
    wd = rows["WijerCo Watchdog"]
    assert wd["stl"] == "OK" and wd["when"] == "Every 1 h + At logon"
    assert wd["last"].startswith("2026-09-27T19:47:19")
    assert rows["RAG Weekly Rehearsal"]["stl"] == "Failed (0x1)"
    assert rows["RAG Weekly Rehearsal"]["when"] == "Mon · 5:30am"
    assert rows["RAG New"]["stl"] == "Not run yet" and rows["RAG New"]["last"] is None
    assert rows["RAG New"]["when"] == "Daily · 11:00pm"
    assert rows["RAG Off"]["stl"] == "Disabled"


# --- aggregate --------------------------------------------------------------

def test_list_schedules_orders_problems_first_and_survives_a_broken_source(monkeypatch):
    now_rows = lambda *a: [reg._row("Fine", "Daily", None, "st-good", "OK", source="n8n")]  # noqa: E731

    def broken(*a):
        raise RuntimeError("powershell missing")
    monkeypatch.setattr(reg, "daemon_rows", now_rows)
    monkeypatch.setattr(reg, "n8n_rows", lambda *a: [])
    monkeypatch.setattr(reg, "os_task_rows", broken)
    items = reg.list_schedules(use_cache=False)
    assert [i["stl"] for i in items] == ["Unreadable", "OK"]
    assert "powershell missing" in items[0]["detail"]
    for i in items:
        assert {"name", "when", "next", "st", "stl", "source", "last", "detail"} <= i.keys()
