"""Add the comparison figures and write the published store.

For each metric and year: sector median, group median, state median, percentile
rank, and year-on-year change. Medians ignore nulls and record the count behind
them so a thin benchmark is visible. Raw numbers are stored; good-or-bad framing
is left to the Phase 3 view and its direction map. Writes SQLite, a flat CSV,
and sector_intel.json in the Phase 3 contract shape.
"""
from __future__ import annotations
import csv, json, sqlite3, statistics, datetime, tempfile, shutil
from pathlib import Path
from . import config

COLS = ["institution_id","institution_name","provider_type","mission_group","state","domain",
        "metric_id","metric_name","year","value","unit","sector_median","group_median",
        "state_median","percentile_rank","yoy_change","yoy_pct","sector_n","status"]

def _median(vals):
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 2) if vals else None

def _write_sqlite(rows, log):
    dbp = config.PUBLISHED / "sector_intel.sqlite"
    try:
        tmp = Path(tempfile.gettempdir()) / "sector_intel_build.sqlite"
        if tmp.exists():
            tmp.unlink()
        con = sqlite3.connect(tmp); cur = con.cursor()
        cur.execute(f"CREATE TABLE sector_intel ({','.join(c + ' TEXT' for c in COLS)})")
        cur.executemany(f"INSERT INTO sector_intel VALUES ({','.join('?' * len(COLS))})",
                        [[r.get(c) for c in COLS] for r in rows])
        cur.execute("CREATE INDEX ix ON sector_intel(institution_id, metric_id, year)")
        con.commit(); con.close()
        shutil.copyfile(tmp, dbp)
        log.info("sqlite export ok")
    except Exception as e:  # noqa: BLE001
        log.warning(f"sqlite export skipped ({e}); csv + json still written")

def run(rows: list[dict], ref: dict, fixture: bool = False) -> dict:
    log = config.load_logger()
    metrics = ref["metrics"]; insts = ref["institutions"]
    by = {(r["institution_id"], r["metric_id"], r["year"]): r for r in rows}
    years = sorted({r["year"] for r in rows})
    mids = sorted({r["metric_id"] for r in rows})

    for mid in mids:
        for y in years:
            present = [r for r in rows if r["metric_id"] == mid and r["year"] == y and r["value"] is not None]
            vals = sorted(r["value"] for r in present)
            sect = _median(vals)
            for r in present:
                g = r["mission_group"]; st = r["state"]
                r["sector_median"] = sect
                r["sector_n"] = len(vals)
                r["group_median"] = _median([x["value"] for x in present if x["mission_group"] == g])
                r["state_median"] = _median([x["value"] for x in present if x["state"] == st])
                r["percentile_rank"] = round(100 * vals.index(r["value"]) / (len(vals) - 1)) if len(vals) > 1 else 50
                prev = by.get((r["institution_id"], mid, years[years.index(y) - 1])) if years.index(y) > 0 else None
                pv = prev["value"] if prev else None
                r["yoy_change"] = round(r["value"] - pv, 2) if pv is not None else None
                r["yoy_pct"] = round(100 * (r["value"] - pv) / pv, 1) if pv not in (None, 0) else None
            for r in [r for r in rows if r["metric_id"] == mid and r["year"] == y and r["value"] is None]:
                r.update(sector_median=sect, sector_n=len(vals), group_median=None,
                         state_median=None, percentile_rank=None, yoy_change=None, yoy_pct=None)

    _write_sqlite(rows, log)

    with open(config.PUBLISHED / "sector_intel.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS); w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c) for c in COLS})

    domains = []
    for m in metrics.values():
        if m["domain"] not in domains:
            domains.append(m["domain"])
    # Attribution framework, modelled on how CoreBridge Intelligence (the named
    # UK reference competitor) cites its own open-government sources: name the
    # publisher and licence next to every figure, claim the derived analysis as
    # WijerCo's own, and tell the reader to verify against the original before
    # relying on any single number. See docs from the Stage 2.3 data-rights
    # review (31 Aug/1 Sep 2026) for the underlying licence research.
    #
    # QILT's licence line is deliberately not marked as cleared: QILT publishes
    # no open licence for its report tables (unlike DoE's CC BY 4.0) and its
    # copyright page directs reuse requests to qilt@srcentre.com.au. Update the
    # licence string below once that permission is actually granted or denied
    # — do not silently upgrade it based on assumption.
    data_sources = [
        {
            "publisher": "Department of Education, Australian Government",
            "collection": "Selected Higher Education Statistics (student, finance, staff data)",
            "licence": "CC BY 4.0 — commercial reuse and derivative works permitted with attribution",
            "attribution": "© Commonwealth of Australia, Department of Education, CC BY 4.0",
            "source_url": "https://www.education.gov.au/higher-education-statistics",
        },
        {
            "publisher": "QILT (Social Research Centre, on behalf of the Department of Education)",
            "collection": "Student Experience Survey (SES) national report tables",
            "licence": "No open licence published — commercial reuse requires permission from QILT/SRC (requested, not yet confirmed as of 1 Sep 2026)",
            "attribution": "Source: QILT, qilt.edu.au",
            "source_url": "https://www.qilt.edu.au/",
        },
    ]
    rights_note = ("Derived metrics, benchmarks and comparisons in this dataset are WijerCo's own "
                   "analysis. Underlying figures are sourced under the licence terms listed in "
                   "data_sources; verify an individual figure against its original source before "
                   "relying on it.")

    dataset = {
        "meta": {
            "title": "WijerCo Sector Intel",
            "sample": bool(fixture),
            "status": "fixture" if fixture else "validated",
            "note": ("FIXTURE data — synthetic stand-in that proves the pipeline. "
                     "Not real DoE/QILT figures.") if fixture else
                    "Built by the Phase 2 pipeline from published DoE/QILT source files.",
            "generated": datetime.date.today().isoformat(),
            "years": years, "groups": ["Go8", "ATN", "IRU", "RUN", "Unaligned"],
            "institution_count": len({r["institution_id"] for r in rows}),
            "metric_count": len(mids), "row_count": len(rows),
            "data_sources": data_sources,
            "rights": rights_note,
        },
        "metrics": [{"metric_id": m["metric_id"], "metric_name": m["metric_name"],
                     "domain": m["domain"], "unit": m["unit"], "direction": m["direction"],
                     "definition": m.get("definition", "")}
                    for m in metrics.values() if m["metric_id"] in mids],
        "domains": domains,
        "institutions": [{"institution_id": i, "institution_name": r["institution_name"],
                          "short": r.get("short", ""), "provider_type": r.get("provider_type", ""),
                          "mission_group": r["mission_group"], "state": r["state"]}
                         for i, r in insts.items()],
        "rows": [{c: r.get(c) for c in COLS} for r in rows],
    }
    (config.PUBLISHED / "sector_intel.json").write_text(json.dumps(dataset, separators=(",", ":")), encoding="utf-8")
    log.info(f"published {len(rows)} rows: sqlite + csv + json "
             f"({(config.PUBLISHED / 'sector_intel.json').stat().st_size // 1024} KB)")
    return dataset

if __name__ == "__main__":
    print("run via run.py")
