"""Fail loudly. These gates catch the errors that quietly ruin a briefing.

Coverage, range, continuity, duplicates, and an empty unmatched report. Writes
a validation report per run. A run with open flags stays labelled indicative.
"""
from __future__ import annotations
import json, datetime
from . import config

# sane bands per metric_id (min, max)
RANGE = {
    "A04": (0, 100), "A05": (0, 100), "A06": (0, 60),
    "B01": (0, 100), "B02": (0, 100), "B04": (0, 100), "B06": (0, 100),
    "C02": (5, 60), "D02": (0, 100), "D03": (0, 100),
}
MIN_COVERAGE = 0.6      # a metric should cover at least 60% of institutions

def run(dataset: dict, ref: dict) -> dict:
    log = config.load_logger()
    rows = dataset["rows"]; n_inst = len(ref["institutions"])
    flags = []
    years = dataset["meta"]["years"]
    metrics = [m["metric_id"] for m in dataset["metrics"]]

    # coverage
    for mid in metrics:
        for y in years:
            have = sum(1 for r in rows if r["metric_id"] == mid and r["year"] == y and r["value"] is not None)
            if have < MIN_COVERAGE * n_inst:
                flags.append(f"coverage: {mid} {y} only {have}/{n_inst}")
    # range
    for r in rows:
        b = RANGE.get(r["metric_id"])
        if b and r["value"] is not None and not (b[0] <= float(r["value"]) <= b[1]):
            flags.append(f"range: {r['institution_id']} {r['metric_id']} {r['year']}={r['value']} outside {b}")
    # continuity: yoy percent within +/-60%
    for r in rows:
        if r.get("yoy_pct") is not None and abs(float(r["yoy_pct"])) > 60:
            flags.append(f"continuity: {r['institution_id']} {r['metric_id']} {r['year']} yoy {r['yoy_pct']}%")
    # duplicates
    seen = set()
    for r in rows:
        k = (r["institution_id"], r["metric_id"], r["year"])
        if k in seen: flags.append(f"duplicate: {k}")
        seen.add(k)
    # unmatched report empty
    um = config.PARSED / "_unmatched.csv"
    if um.exists() and len([l for l in um.read_text(encoding="utf-8").splitlines()[1:] if l.strip()]) > 0:
        flags.append("unmatched: _unmatched.csv is not empty")

    report = {"generated": datetime.date.today().isoformat(),
              "rows": len(rows), "institutions": n_inst, "metrics": len(metrics),
              "passed": len(flags) == 0, "flag_count": len(flags), "flags": flags[:200]}
    (config.PUBLISHED / "validation_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lvl = log.info if report["passed"] else log.warning
    lvl(f"validate: {'PASS' if report['passed'] else 'FLAGS'} — {len(flags)} flag(s)")
    for fl in flags[:10]:
        log.warning("  " + fl)
    return report

if __name__ == "__main__":
    print("run via run.py")
