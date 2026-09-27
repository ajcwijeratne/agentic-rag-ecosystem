"""
Live registry of everything that runs on its own, for GET /schedule/list.

Replaces the hand-authored `13_Command Centre/Scheduled Runs/` vault notes,
whose names, times and statuses were typed in and never changed. Every row
here is read from the scheduler that actually owns the loop:

  daemon  — the operating daemon's own cycle plus its periodic jobs
            (consolidation, measure, learning, weekly initiative, monthly
            review). Schedule from orchestrator.daemon's settings, last run
            from data/daemon_state.json, failures from logs/daemon.jsonl,
            liveness from the heartbeat file.
  n8n     — workflows with a Schedule Trigger. With N8N_API_KEY set, the live
            instance is the source (active flag, last execution). Without it,
            the definitions in n8n-workflows/ and n8n/workflows/ are listed
            as unverified, since nothing confirms they are imported or active.
  os      — Windows Task Scheduler tasks, or systemd timers on Linux, whose
            names start with one of SCHEDULE_TASK_PREFIXES.

Each row keeps the Command Centre contract (name, when, next, st, stl) and
adds source, last (ISO timestamp or null) and detail.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from . import daemon as daemon_ctl

_ROOT = Path(__file__).resolve().parent.parent
N8N_DEF_DIRS = [_ROOT / "n8n-workflows", _ROOT / "n8n" / "workflows"]
N8N_API_URL = os.getenv("N8N_API_URL", "http://localhost:5678/api/v1").rstrip("/")
# n8n evaluates cron in GENERIC_TIMEZONE, and defaults to America/New_York
# when it is unset. Mirror whatever the container is configured with.
N8N_TIMEZONE = os.getenv("N8N_TIMEZONE") or os.getenv("GENERIC_TIMEZONE") or "America/New_York"
TASK_PREFIXES = tuple(
    p.strip() for p in os.getenv("SCHEDULE_TASK_PREFIXES", "RAG,WijerCo,rag-").split(",") if p.strip()
)
CACHE_TTL = float(os.getenv("SCHEDULE_CACHE_TTL", "30"))

_cache: dict[str, Any] = {"at": 0.0, "items": None}

_ST_ORDER = {"st-fail": 0, "st-warn": 1, "st-good": 2, "st-mute": 3}
_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _row(name: str, when: str, nxt: datetime | None, st: str, stl: str, *,
         source: str, last: datetime | None = None, detail: str = "",
         next_label: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "when": when,
        "next": next_label or (fmt_next(nxt, now) if nxt else "—"),
        "st": st,
        "stl": stl,
        "source": source,
        "last": last.isoformat(timespec="seconds") if last else None,
        "next_at": nxt.isoformat(timespec="minutes") if nxt else None,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# Time formatting
# ---------------------------------------------------------------------------

def fmt_clock(t: datetime | time) -> str:
    h = t.hour % 12 or 12
    return f"{h}:{t.minute:02d}{'am' if t.hour < 12 else 'pm'}"


def fmt_next(nxt: datetime, now: datetime | None = None) -> str:
    """'Today 6:30am', 'Tomorrow 5:00am', 'Fri 7:00am', or '1 Oct 8:00am'."""
    now = now or datetime.now(nxt.tzinfo)
    days = (nxt.date() - now.date()).days
    if days <= 0:
        return f"Today {fmt_clock(nxt)}"
    if days == 1:
        return f"Tomorrow {fmt_clock(nxt)}"
    if days < 7:
        return f"{_DOW[nxt.weekday()]} {fmt_clock(nxt)}"
    return f"{nxt.day} {nxt.strftime('%b')} {fmt_clock(nxt)}"


def fmt_ago(then: datetime, now: datetime) -> str:
    secs = max(0, int((now - then).total_seconds()))
    if secs < 120:
        return "just now"
    if secs < 7200:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} days ago"


# ---------------------------------------------------------------------------
# Cron (standard 5-field: minute hour day-of-month month day-of-week)
# ---------------------------------------------------------------------------

def _cron_field(spec: str, lo: int, hi: int) -> set[int]:
    vals: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", ""):
            a, b = lo, hi
        elif "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
        else:
            a = int(part)
            b = hi if step > 1 else a
        vals.update(range(a, b + 1, step))
    return vals


def cron_next(expr: str, after: datetime) -> datetime | None:
    """Next fire time strictly after `after`, in after's timezone."""
    f = expr.split()
    if len(f) != 5:
        return None
    try:
        mins = sorted(_cron_field(f[0], 0, 59))
        hours = sorted(_cron_field(f[1], 0, 23))
        doms = _cron_field(f[2], 1, 31)
        mons = _cron_field(f[3], 1, 12)
        dows = {d % 7 for d in _cron_field(f[4], 0, 7)}  # 0 and 7 are Sunday
    except ValueError:
        return None
    dom_any, dow_any = f[2] == "*", f[4] == "*"
    start = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    day = start.date()
    for _ in range(366 * 4):
        if day.month in mons:
            dm = day.day in doms
            wm = (day.weekday() + 1) % 7 in dows
            # Cron semantics: when both day fields are restricted, either matches.
            if dom_any and dow_any:
                ok = True
            elif dom_any or dow_any:
                ok = wm if dom_any else dm
            else:
                ok = dm or wm
            if ok:
                for h in hours:
                    for m in mins:
                        cand = datetime.combine(day, time(h, m), tzinfo=after.tzinfo)
                        if cand >= start:
                            return cand
        day += timedelta(days=1)
    return None


def describe_cron(expr: str) -> str:
    """Human label for the cron shapes this system uses; raw cron otherwise."""
    f = expr.split()
    if len(f) != 5 or not (f[0].isdigit() and f[1].isdigit()) or f[3] != "*":
        return f"cron {expr}"
    at = fmt_clock(time(int(f[1]), int(f[0])))
    dom, dow = f[2], f[4]
    if dom == "*" and dow == "*":
        return f"Daily · {at}"
    if dom == "*" and dow == "1-5":
        return f"Weekdays · {at}"
    if dom == "*" and dow.isdigit():
        return f"{_DOW[(int(dow) - 1) % 7]} · {at}"
    if dow == "*" and dom.isdigit():
        return f"Monthly · day {dom} · {at}"
    return f"cron {expr}"


def _n8n_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(N8N_TIMEZONE)
    except Exception:
        return datetime.now().astimezone().tzinfo


# ---------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------

# name, state key, success action, error action
_DAEMON_JOBS = [
    ("Memory consolidation", "consolidated_date", "consolidation", "consolidation_error"),
    ("Outcome measure sweep", "measured_date", "measure_sweep", "measure_error"),
    ("Weekly content initiative", "initiative_week", "weekly_initiative", "initiative_error"),
    ("Monthly learning reflection", "learned_month", "learning_reflection", "learning_error"),
    ("Monthly review", "reviewed_month", "monthly_review", "review_error"),
]


def _last_daemon_events(actions: set[str], max_bytes: int = 400_000) -> dict[str, dict]:
    """Most recent logs/daemon.jsonl entry for each action, reading only the tail."""
    found: dict[str, dict] = {}
    try:
        with daemon_ctl.LOG_PATH.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return found
    for line in reversed(lines):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        act = ev.get("action")
        if act in actions and act not in found:
            found[act] = ev
            if len(found) == len(actions):
                break
    return found


def _parse_ts(v: Any) -> datetime | None:
    if not v:
        return None
    # .NET emits 7 fractional digits; fromisoformat accepts at most 6.
    text = re.sub(r"(\.\d{6})\d+", r"", str(v).replace("Z", "+00:00"))
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _daemon_job_timing(key: str, state: dict, now: datetime) -> tuple[str, datetime, bool]:
    """(when label, next run, overdue) for one periodic daemon job. `now` is naive local,
    matching the daemon, which schedules on datetime.now()."""
    d = daemon_ctl
    marker = state.get(key) or ""
    if key in ("consolidated_date", "measured_date"):
        hour = d.CONSOLIDATION_HOUR if key == "consolidated_date" else d.MEASURE_HOUR
        today = now.strftime("%Y-%m-%d")
        due_today = datetime.combine(now.date(), time(hour))
        if marker == today:
            nxt = due_today + timedelta(days=1)
        else:
            nxt = due_today if now < due_today else now
        expected = today if now >= due_today else (now.date() - timedelta(days=1)).isoformat()
        return f"Daily · {fmt_clock(time(hour))}", nxt, marker < expected
    if key == "initiative_week":
        wd, hour = d.INITIATIVE_WEEKDAY, d.INITIATIVE_HOUR
        this_slot = datetime.combine(now.date() - timedelta(days=(now.weekday() - wd) % 7), time(hour))
        iso = now.isocalendar()
        week = f"{iso[0]}-W{iso[1]:02d}"
        if now < this_slot:
            prev = (this_slot - timedelta(days=7)).isocalendar()
            expected = f"{prev[0]}-W{prev[1]:02d}"
            nxt = this_slot
        else:
            expected = week
            # The daemon only fires on the weekday itself, so a missed slot waits a week.
            late_same_day = now.date() == this_slot.date() and marker != week
            nxt = now if late_same_day else this_slot + timedelta(days=7)
        return f"{_DOW[wd]} · {fmt_clock(time(hour))}", nxt, marker < expected
    # monthly jobs
    day = d.LEARN_DAY if key == "learned_month" else d.REVIEW_DAY
    month = now.strftime("%Y-%m")
    first = now.date().replace(day=1)
    if marker == month or now.day < day:
        nm = (first + timedelta(days=32)).replace(day=1) if marker == month else first
        nxt = datetime.combine(nm.replace(day=min(day, 28)), time(0))
    else:
        nxt = now
    expected = month if now.day >= day else (first - timedelta(days=1)).strftime("%Y-%m")
    return f"Monthly · day {day}", nxt, marker < expected


def daemon_rows(now_utc: datetime | None = None) -> list[dict[str, Any]]:
    now_utc = now_utc or datetime.now(timezone.utc)
    now_local = now_utc.astimezone().replace(tzinfo=None)
    state = daemon_ctl.load_state()
    interval = float(daemon_ctl.INTERVAL_SEC)
    try:
        beat = datetime.fromtimestamp(daemon_ctl.HEARTBEAT_PATH.stat().st_mtime, timezone.utc)
    except OSError:
        beat = None
    alive = beat is not None and (now_utc - beat).total_seconds() <= max(3 * interval, 300)
    paused = bool(state.get("paused"))

    rows: list[dict[str, Any]] = []
    every = f"Every {int(interval // 60)} min" if interval >= 60 else f"Every {int(interval)} s"
    if not alive:
        seen = f"last heartbeat {fmt_ago(beat, now_utc)}" if beat else "no heartbeat on record"
        rows.append(_row("Operating daemon", every, None, "st-fail", "Stopped", source="daemon",
                         last=beat, next_label="not running",
                         detail=f"Daemon process is not running ({seen}). Start it with python -m orchestrator.daemon."))
    elif paused:
        rows.append(_row("Operating daemon", every, None, "st-warn", "Paused", source="daemon",
                         last=beat, next_label="paused",
                         detail=f"Paused by {state.get('paused_by') or 'operator'}. Resume from the cockpit or the resume_daemon tool."))
    else:
        rows.append(_row("Operating daemon", every, (beat + timedelta(seconds=interval)).astimezone(),
                         "st-good", "Running", source="daemon", last=beat, now=now_utc.astimezone(),
                         detail=f"{state.get('cycles', 0)} cycles; last: {(state.get('last_result') or {}).get('action', '—')}."))

    events = _last_daemon_events({a for j in _DAEMON_JOBS for a in j[2:]})
    for name, key, ok_act, err_act in _DAEMON_JOBS:
        when, nxt, overdue = _daemon_job_timing(key, state, now_local)
        ok_ev, err_ev = events.get(ok_act), events.get(err_act)
        ok_at, err_at = _parse_ts((ok_ev or {}).get("ts")), _parse_ts((err_ev or {}).get("ts"))
        last = max([t for t in (ok_at, err_at) if t], default=None)
        failed = err_at is not None and (ok_at is None or err_at > ok_at)
        nxt_aware = nxt.astimezone() if nxt else None
        if not alive:
            st, stl, detail = "st-fail", "Daemon stopped", "Runs inside the operating daemon, which is not running."
            nxt_label = "when daemon restarts"
        elif failed:
            st, stl = "st-fail", "Failed"
            detail, nxt_label = str(err_ev.get("error") or "")[:300], None
        elif paused:
            st, stl, detail, nxt_label = "st-warn", "Paused", "Daemon is paused.", None
        elif overdue:
            st, stl, detail, nxt_label = "st-warn", "Overdue", f"Last run marker: {state.get(key) or 'never'}.", None
        elif last is None:
            st, stl, detail, nxt_label = "st-mute", "Not run yet", "", None
        else:
            st, stl, detail, nxt_label = "st-good", "OK", "", None
        if nxt_label is None and nxt is not None and nxt <= now_local:
            nxt_label = "next daemon cycle"
        rows.append(_row(name, when, nxt_aware, st, stl, source="daemon", last=last,
                         detail=detail, next_label=nxt_label, now=now_utc.astimezone()))
    return rows


# ---------------------------------------------------------------------------
# n8n
# ---------------------------------------------------------------------------

def _schedule_crons(nodes: list[dict]) -> list[str]:
    crons = []
    for n in nodes or []:
        if n.get("disabled") or "scheduletrigger" not in str(n.get("type", "")).lower():
            continue
        for rule in ((n.get("parameters") or {}).get("rule") or {}).get("interval") or []:
            if rule.get("field") == "cronExpression" and rule.get("expression"):
                crons.append(str(rule["expression"]).strip())
    return crons


def _repo_n8n_definitions() -> list[dict[str, Any]]:
    defs = []
    for d in N8N_DEF_DIRS:
        for p in sorted(d.glob("*.json")) if d.is_dir() else []:
            try:
                wf = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            crons = _schedule_crons(wf.get("nodes") or [])
            if crons:
                defs.append({"name": wf.get("name") or p.stem, "crons": crons,
                             "file": str(p.relative_to(_ROOT)).replace("\\", "/")})
    return defs


def _n8n_live(api_key: str) -> list[dict[str, Any]]:
    headers = {"X-N8N-API-KEY": api_key, "Accept": "application/json"}
    out = []
    with httpx.Client(timeout=5.0, headers=headers) as client:
        r = client.get(f"{N8N_API_URL}/workflows", params={"limit": 250})
        r.raise_for_status()
        for wf in r.json().get("data") or []:
            crons = _schedule_crons(wf.get("nodes") or [])
            if not crons:
                continue
            last = None
            try:
                ex = client.get(f"{N8N_API_URL}/executions",
                                params={"workflowId": wf.get("id"), "limit": 1})
                if ex.status_code == 200:
                    data = ex.json().get("data") or []
                    last = data[0] if data else None
            except httpx.HTTPError:
                pass
            out.append({"name": wf.get("name"), "crons": crons, "active": bool(wf.get("active")),
                        "tz": (wf.get("settings") or {}).get("timezone"), "last": last})
    return out


def n8n_rows(now_utc: datetime | None = None) -> list[dict[str, Any]]:
    now_utc = now_utc or datetime.now(timezone.utc)
    api_key = os.getenv("N8N_API_KEY", "")
    rows: list[dict[str, Any]] = []

    def timing(crons: list[str], tz) -> tuple[str, datetime | None]:
        now_tz = now_utc.astimezone(tz)
        nexts = [n for n in (cron_next(c, now_tz) for c in crons) if n]
        when = " + ".join(describe_cron(c) for c in crons)
        if getattr(tz, "key", None) and now_tz.utcoffset() != now_utc.astimezone().utcoffset():
            when += f" ({tz.key})"
        return when, (min(nexts).astimezone() if nexts else None)

    if api_key:
        try:
            live = _n8n_live(api_key)
        except Exception as exc:
            live = None
            err = str(exc)[:200]
        if live is not None:
            for wf in live:
                tz = _n8n_tz()
                if wf.get("tz") and wf["tz"] != "DEFAULT":
                    try:
                        from zoneinfo import ZoneInfo
                        tz = ZoneInfo(wf["tz"])
                    except Exception:
                        pass
                when, nxt = timing(wf["crons"], tz)
                last = wf.get("last") or {}
                last_at = _parse_ts(last.get("startedAt"))
                status = str(last.get("status") or ("success" if last.get("finished") else "")).lower()
                if not wf["active"]:
                    st, stl, nxt = "st-mute", "Inactive", None
                elif status in ("error", "crashed", "failed"):
                    st, stl = "st-fail", "Failed"
                elif status in ("running", "waiting", "new"):
                    st, stl = "st-warn", "Running"
                elif status == "success":
                    st, stl = "st-good", "OK"
                else:
                    st, stl = "st-mute", "Not run yet"
                rows.append(_row(wf["name"], when, nxt, st, stl, source="n8n", last=last_at,
                                 next_label=None if wf["active"] else "off", now=now_utc.astimezone(),
                                 detail=f"Last execution {status or 'none'}."))
            return rows
        note = f"n8n API unreachable ({err}); showing repo definitions."
    else:
        note = "Defined in the repo. Set N8N_API_KEY to confirm it is imported, active and succeeding."

    for wf in _repo_n8n_definitions():
        when, nxt = timing(wf["crons"], _n8n_tz())
        rows.append(_row(wf["name"], when, nxt, "st-mute", "Unverified", source="n8n",
                         now=now_utc.astimezone(), detail=f"{note} ({wf['file']})"))
    return rows


# ---------------------------------------------------------------------------
# OS scheduler (Windows Task Scheduler / systemd timers)
# ---------------------------------------------------------------------------

_PS_TASKS = r"""
$ErrorActionPreference = 'SilentlyContinue'
$p = @(%s)
Get-ScheduledTask | Where-Object { $n = $_.TaskName; ($p | Where-Object { $n -like "$_*" }).Count -gt 0 } | ForEach-Object {
  $i = $_ | Get-ScheduledTaskInfo
  [pscustomobject]@{
    name = $_.TaskName; state = [string]$_.State; desc = $_.Description
    last = if ($i.LastRunTime) { $i.LastRunTime.ToUniversalTime().ToString('o') } else { $null }
    next = if ($i.NextRunTime) { $i.NextRunTime.ToUniversalTime().ToString('o') } else { $null }
    result = $i.LastTaskResult
    triggers = @($_.Triggers | ForEach-Object { [pscustomobject]@{
      kind = $_.CimClass.CimClassName; interval = $_.Repetition.Interval
      start = $_.StartBoundary; days = $_.DaysOfWeek; daysInterval = $_.DaysInterval } })
  }
} | ConvertTo-Json -Depth 4 -Compress
"""

_TASK_NEVER_RAN = {267011, 0x41303}
_TASK_RUNNING = {267009, 0x41301}


def _iso_duration(v: str) -> str:
    """PT5M -> '5 min', PT1H -> '1 h', P1D -> '1 day'."""
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v or "")
    if not m:
        return v
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    parts = [f"{d} day" if d else "", f"{h} h" if h else "", f"{mi} min" if mi else "", f"{s} s" if s else ""]
    return " ".join(p for p in parts if p) or v


def _win_trigger_label(trigs: list[dict]) -> str:
    labels = []
    for t in trigs or []:
        kind = str(t.get("kind") or "")
        at = ""
        try:
            at = fmt_clock(datetime.fromisoformat(str(t.get("start"))[:19]))
        except ValueError:
            pass
        if t.get("interval"):
            labels.append(f"Every {_iso_duration(t['interval'])}")
        elif "Logon" in kind:
            labels.append("At logon")
        elif "Boot" in kind:
            labels.append("At startup")
        elif "Weekly" in kind and t.get("days"):
            bits = int(t["days"])  # 1=Sun, 2=Mon, ... 64=Sat
            days = [_DOW[(i - 1) % 7] for i in range(7) if bits & (1 << i)]
            labels.append(f"{', '.join(days)} · {at}")
        elif "Daily" in kind:
            labels.append(f"Daily · {at}")
        elif "Time" in kind:
            labels.append(f"Once · {at}")
        else:
            labels.append(kind.replace("MSFT_Task", "").replace("Trigger", "") or "Custom")
    return " + ".join(labels) or "No trigger"


def windows_task_rows(raw: list[dict], now_utc: datetime) -> list[dict[str, Any]]:
    rows = []
    for t in raw:
        last = _parse_ts(t.get("last"))
        if last and last.year < 2000:
            last = None
        nxt = _parse_ts(t.get("next"))
        code = t.get("result")
        state = str(t.get("state") or "")
        if state.lower() == "disabled":
            st, stl, nxt = "st-mute", "Disabled", None
        elif code in _TASK_RUNNING or state.lower() == "running":
            st, stl = "st-warn", "Running"
        elif code in _TASK_NEVER_RAN or last is None:
            st, stl = "st-mute", "Not run yet"
        elif code == 0:
            st, stl = "st-good", "OK"
        else:
            st, stl = "st-fail", f"Failed (0x{int(code) & 0xFFFFFFFF:X})"
        rows.append(_row(t.get("name") or "?", _win_trigger_label(t.get("triggers") or []),
                         nxt.astimezone() if nxt else None, st, stl, source="task-scheduler",
                         last=last, detail=str(t.get("desc") or "")[:300],
                         now=now_utc.astimezone()))
    return rows


def _systemd_rows(now_utc: datetime) -> list[dict[str, Any]]:
    try:
        out = subprocess.run(["systemctl", "list-timers", "--all", "--output=json"],
                             capture_output=True, text=True, timeout=8)
        timers = json.loads(out.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    rows = []
    for t in timers:
        unit = str(t.get("unit") or "")
        if not unit.startswith(TASK_PREFIXES):
            continue
        svc = str(t.get("activates") or unit.replace(".timer", ".service"))
        to_dt = lambda us: datetime.fromtimestamp(us / 1e6, timezone.utc) if us else None  # noqa: E731
        nxt, last = to_dt(t.get("next")), to_dt(t.get("last"))
        result = ""
        try:
            show = subprocess.run(["systemctl", "show", svc, "-p", "Result", "--value"],
                                  capture_output=True, text=True, timeout=5)
            result = show.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        if last is None:
            st, stl = "st-mute", "Not run yet"
        elif result in ("", "success"):
            st, stl = "st-good", "OK"
        else:
            st, stl = "st-fail", f"Failed ({result})"
        rows.append(_row(unit.removesuffix(".timer"), "systemd timer",
                         nxt.astimezone() if nxt else None, st, stl, source="systemd",
                         last=last, detail=f"Activates {svc}.", now=now_utc.astimezone()))
    return rows


def os_task_rows(now_utc: datetime | None = None) -> list[dict[str, Any]]:
    now_utc = now_utc or datetime.now(timezone.utc)
    if sys.platform != "win32":
        return _systemd_rows(now_utc)
    prefixes = ",".join("'" + p.replace("'", "''") + "'" for p in TASK_PREFIXES)
    try:
        out = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _PS_TASKS % prefixes],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = json.loads(out.stdout) if out.stdout.strip() else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []
    if isinstance(raw, dict):  # ConvertTo-Json unwraps a single object
        raw = [raw]
    return windows_task_rows(raw, now_utc)


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------

def list_schedules(*, use_cache: bool = True) -> list[dict[str, Any]]:
    if use_cache and _cache["items"] is not None and _time.monotonic() - _cache["at"] < CACHE_TTL:
        return _cache["items"]
    now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    for source in (daemon_rows, n8n_rows, os_task_rows):
        try:
            items.extend(source(now))
        except Exception as exc:  # one broken source must not blank the page
            items.append(_row(f"{source.__name__.replace('_rows', '')} source", "—", None, "st-fail",
                              "Unreadable", source="registry", detail=str(exc)[:300]))
    # Problems first; within a status, keep each scheduler's rows together,
    # the daemon's own cycle ahead of the jobs it runs.
    src_order = {"daemon": 0, "n8n": 1}
    items.sort(key=lambda r: (_ST_ORDER.get(r["st"], 9), src_order.get(r["source"], 2),
                              r["name"] != "Operating daemon", r.get("next_at") or "9999"))
    _cache.update(at=_time.monotonic(), items=items)
    return items
