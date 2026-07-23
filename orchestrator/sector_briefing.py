"""Sector Intel — client briefing layer (Phase 4).

Reads the same published dataset the command centre reads and turns one
institution's benchmarked position into a client briefing. Two paths:

  - the Sector Intelligence analyst agent writes the prose (routed by the
    endpoint), given the evidence pack built here;
  - a computed, data-driven briefing is the fallback so the button always
    returns something useful when the agent is offline or over budget.

This module is pure: no FastAPI, no vault. It builds the evidence pack, the
computed briefing, and the agent prompt. The endpoint wires routing and saving.
"""
from __future__ import annotations
import json
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PATHS = [
    _REPO / "sector-intel" / "data" / "published" / "sector_intel.json",
    _REPO / "ui" / "sector_intel.json",
]

# Metrics that map to a WijerCo advisory opening when the institution trails the
# sector on them (read in the metric's own direction).
ADVISORY = {
    "A06": "First-year attrition runs above the sector. Retention and first-year learning design is the opening.",
    "B04": "Student support scores below the sector, the sharpest risk for online cohorts. The student support model is the opening.",
    "B01": "Overall educational experience trails the sector. Course design and academic capability is the opening.",
    "B02": "Teaching quality trails the sector. Academic capability and teaching development is the opening.",
    "B06": "Graduate employment trails the sector. Work-integrated design and course-to-career alignment is the opening.",
    "C02": "The student-to-staff ratio runs above the sector. Delivery model and workload design is the opening.",
}

def load_dataset() -> dict | None:
    for p in _PATHS:
        try:
            if p.exists() and p.stat().st_size > 0:
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
    return None

def _fmt(v, unit):
    if v is None:
        return "n/a"
    if unit in ("%", "% positive"):
        return f"{v}%"
    if unit == "$m":
        return f"${v:,.0f}m"
    if unit == "ratio":
        return f"{v}:1"
    if unit in ("headcount", "EFTSL", "FTE"):
        try:
            return f"{round(float(v)):,}"
        except Exception:  # noqa: BLE001
            return str(v)
    return str(v)

def _ord(n):
    if n is None:
        return ""
    n = int(round(n))
    if 10 <= n % 100 <= 20:
        suf = "th"
    else:
        suf = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"

def _standing(value, median, direction):
    """ahead / behind / context, read in the metric's direction."""
    if value is None or median is None or direction == "neutral":
        return "context"
    if abs(value - median) < 1e-9:
        return "level"
    above = value > median
    good = above if direction == "higher" else not above
    return "ahead" if good else "behind"

def build_evidence(dataset: dict, institution_id: str, year=None) -> dict | None:
    if not dataset or not dataset.get("rows"):
        return None
    inst = next((i for i in dataset.get("institutions", []) if i["institution_id"] == institution_id), None)
    if not inst:
        return None
    years = dataset["meta"].get("years") or sorted({r["year"] for r in dataset["rows"]})
    y = year or (years[-1] if years else None)
    mdir = {m["metric_id"]: m for m in dataset.get("metrics", [])}
    metrics = []
    for r in dataset["rows"]:
        if r["institution_id"] != institution_id or r["year"] != y:
            continue
        m = mdir.get(r["metric_id"], {})
        direction = m.get("direction", "neutral")
        val, med = r.get("value"), r.get("sector_median")
        delta = round(val - med, 2) if (val is not None and med is not None) else None
        metrics.append({
            "metric_id": r["metric_id"], "metric_name": r["metric_name"], "domain": r.get("domain", ""),
            "unit": r.get("unit", ""), "direction": direction, "value": val,
            "sector_median": med, "group_median": r.get("group_median"), "state_median": r.get("state_median"),
            "percentile_rank": r.get("percentile_rank"), "yoy_change": r.get("yoy_change"),
            "yoy_pct": r.get("yoy_pct"), "delta_vs_sector": delta,
            "standing": _standing(val, med, direction),
        })
    if not metrics:
        return None
    order = {mid: i for i, mid in enumerate([m["metric_id"] for m in dataset.get("metrics", [])])}
    metrics.sort(key=lambda x: order.get(x["metric_id"], 99))
    return {
        "institution": inst, "year": y, "metrics": metrics,
        "meta": dataset.get("meta", {}),
    }

def _notable(metrics, standing, n=None):
    items = [m for m in metrics if m["standing"] == standing and m["delta_vs_sector"] is not None]
    items.sort(key=lambda x: abs(x["delta_vs_sector"]), reverse=True)
    return items[:n] if n else items

def _line(m):
    parts = [f"{m['metric_name']} {_fmt(m['value'], m['unit'])}",
             f"sector {_fmt(m['sector_median'], m['unit'])}"]
    if m["delta_vs_sector"] is not None:
        sign = "+" if m["delta_vs_sector"] > 0 else ""
        parts.append(f"{sign}{m['delta_vs_sector']} vs sector")
    if m["percentile_rank"] is not None:
        parts.append(f"{_ord(m['percentile_rank'])} percentile")
    if m.get("yoy_change") not in (None, 0):
        s = "+" if m["yoy_change"] > 0 else ""
        parts.append(f"{s}{m['yoy_change']} year on year")
    return " · ".join(parts)

def computed_briefing(ev: dict, audience: str = "partner leadership") -> str:
    inst = ev["institution"]; ms = ev["metrics"]; y = ev["year"]
    name = inst["institution_name"]; short = inst.get("short") or name
    ahead = _notable(ms, "ahead", 3)
    behind = _notable(ms, "behind", 4)
    watch = [m for m in ms if m.get("yoy_pct") is not None and (
        (m["direction"] == "higher" and m["yoy_change"] < 0) or
        (m["direction"] == "lower" and m["yoy_change"] > 0))]
    watch.sort(key=lambda x: abs(x["yoy_pct"]), reverse=True)

    lead_bits = []
    if behind:
        lead_bits.append(f"trails the sector on {behind[0]['metric_name'].lower()} ({_fmt(behind[0]['value'], behind[0]['unit'])})")
    if ahead:
        lead_bits.append(f"sits ahead on {ahead[0]['metric_name'].lower()} ({_fmt(ahead[0]['value'], ahead[0]['unit'])})")
    lead = f"{short} {', and '.join(lead_bits)}." if lead_bits else f"{short} sits close to the sector median across the tracked measures."

    out = [f"# Sector briefing: {name}",
           f"{inst.get('provider_type','')} · {inst.get('mission_group','')} · {inst.get('state','')} · reference year {y}",
           "",
           "## Where it sits",
           lead, ""]
    for m in (behind[:3] + ahead[:2]):
        tag = "behind the sector" if m["standing"] == "behind" else "ahead of the sector"
        out.append(f"- {_line(m)} ({tag})")
    out.append("")

    openings = [ADVISORY[m["metric_id"]] for m in behind if m["metric_id"] in ADVISORY]
    online = next((m for m in ms if m["metric_id"] == "A05"), None)
    weak_exp = any(m["metric_id"] in ("B01", "B04") and m["standing"] == "behind" for m in ms)
    if online and online["value"] is not None and online["value"] >= 25 and weak_exp:
        openings.insert(0, f"A large online cohort ({_fmt(online['value'], online['unit'])} of load) paired with below-sector experience is the clearest online-strategy opening.")
    out.append("## Advisory openings for WijerCo")
    if openings:
        out += [f"- {o}" for o in openings]
    else:
        out.append("- No clear deficit against the sector on the tracked measures. Position on holding strengths and targeted efficiency, not remediation.")
    out.append("")

    out.append("## Watch")
    if watch:
        for m in watch[:3]:
            s = "+" if m["yoy_change"] > 0 else ""
            out.append(f"- {m['metric_name']} moved {s}{m['yoy_change']} ({s}{m['yoy_pct']}%) year on year, against direction.")
    else:
        out.append("- Nothing moving materially against direction year on year.")
    out.append("")

    out.append("## Evidence")
    out.append("| Metric | Value | Sector | Group | State | %ile | YoY |")
    out.append("|---|---|---|---|---|---|---|")
    for m in ms:
        yoy = "" if m.get("yoy_change") in (None,) else (f"+{m['yoy_change']}" if m['yoy_change'] > 0 else str(m['yoy_change']))
        out.append(f"| {m['metric_id']} {m['metric_name']} | {_fmt(m['value'], m['unit'])} | "
                   f"{_fmt(m['sector_median'], m['unit'])} | {_fmt(m['group_median'], m['unit'])} | "
                   f"{_fmt(m['state_median'], m['unit'])} | {m['percentile_rank'] if m['percentile_rank'] is not None else ''} | {yoy} |")
    out.append("")
    status = ev["meta"].get("status", "indicative")
    if ev["meta"].get("sample"):
        out.append(f"Figures are {status} sample data, not verified DoE/QILT numbers. Confirm before client use.")
    else:
        out.append(f"Figures are {status}. Benchmarks are sector, mission-group and state medians for {y}.")
    return "\n".join(out)

def agent_query(ev: dict, audience: str = "partner leadership") -> str:
    inst = ev["institution"]; y = ev["year"]
    rows = []
    for m in ev["metrics"]:
        rows.append(f"{m['metric_id']} {m['metric_name']}: {_fmt(m['value'], m['unit'])} "
                    f"(sector {_fmt(m['sector_median'], m['unit'])}, group {_fmt(m['group_median'], m['unit'])}, "
                    f"{m['standing']}, {_ord(m['percentile_rank'])} pct, yoy {m.get('yoy_change')})")
    evidence = "\n".join(rows)
    return (
        f"Write a client briefing on {inst['institution_name']} "
        f"({inst.get('provider_type','')}, {inst.get('mission_group','')}, {inst.get('state','')}) for {y}.\n"
        f"Audience: {audience}. Use only the benchmarked evidence below; do not invent figures.\n\n"
        f"EVIDENCE (value vs sector/group medians, standing read in each metric's direction):\n{evidence}\n\n"
        "Structure: a one-line lead with the single most important point; 'Where it sits' with the "
        "three or four measures that matter, each backed by its number; 'Advisory openings for WijerCo' "
        "naming where the gap is and the WijerCo work that answers it (online strategy, retention and "
        "first-year learning design, academic capability, student support model); 'Watch' for measures "
        "moving against direction year on year. Lead with the point, name every number, no buzzwords. "
        "Around 300 to 400 words, returned as markdown."
    )

def headline() -> str:
    """One-line sector signal for the weekly initiative. Empty when no data."""
    ds = load_dataset()
    if not ds or not ds.get("institutions"):
        return ""
    inst = ds["institutions"][0]
    ev = build_evidence(ds, inst["institution_id"])
    if not ev:
        return ""
    name = inst.get("short") or inst.get("institution_name") or inst["institution_id"]
    behind = _notable(ev["metrics"], "behind", 1)
    if behind:
        m = behind[0]
        return (f"{name} trails sector on {m['metric_name'].lower()} "
                f"({_fmt(m['value'], m['unit'])} vs {_fmt(m['sector_median'], m['unit'])})")
    return f"{name} sits near the sector median across tracked measures"


if __name__ == "__main__":
    ds = load_dataset()
    ev = build_evidence(ds, (ds["institutions"][0]["institution_id"] if ds else ""), None) if ds else None
    print(computed_briefing(ev) if ev else "no dataset")
