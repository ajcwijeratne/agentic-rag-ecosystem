"""Turn resolved parsed rows into the Phase 1 long-format model.

Attaches domain, metric_name, unit from the metric dictionary; joins mission
group and state; computes the two derived metrics (C02 student-to-staff ratio
from A02 and C01; D02 domestic+government revenue share from D03). One row per
institution, metric, year, written to data/normalised.
"""
from __future__ import annotations
import csv
from . import config

OTHER_REVENUE_SHARE = 8.0   # non-domestic, non-international 'other', held flat

def run(resolved: list[dict], ref: dict) -> list[dict]:
    log = config.load_logger()
    metrics = ref["metrics"]; insts = ref["institutions"]
    # index base values for derived metrics
    base = {}   # (iid, mid, year) -> value
    for r in resolved:
        if r["value"] is not None:
            base[(r["institution_id"], r["metric_id"], r["year"])] = r["value"]
    years = sorted({r["year"] for r in resolved})
    rows = []
    def emit(iid, mid, year, value):
        m = metrics.get(mid)
        if not m:
            return
        inst = insts[iid]
        rows.append({
            "institution_id": iid, "institution_name": inst["institution_name"],
            "provider_type": inst.get("provider_type", ""), "mission_group": inst["mission_group"],
            "state": inst["state"], "domain": m["domain"], "metric_id": mid,
            "metric_name": m["metric_name"], "year": year,
            "value": round(value, 2) if value is not None else None, "unit": m["unit"],
            "source_name": m.get("source_name", ""), "status": "indicative",
        })
    # direct metrics
    for r in resolved:
        emit(r["institution_id"], r["metric_id"], r["year"], r["value"])
    # derived metrics per institution/year
    for iid in insts:
        for y in years:
            eftsl = base.get((iid, "A02", y)); staff = base.get((iid, "C01", y))
            if eftsl and staff:
                acad = staff * 0.42
                if acad:
                    emit(iid, "C02", y, (eftsl / acad))
            d03 = base.get((iid, "D03", y))
            if d03 is not None:
                emit(iid, "D02", y, max(0.0, 100 - d03 - OTHER_REVENUE_SHARE))
    dest = config.NORMALISED / "sector_intel_long.csv"
    cols = ["institution_id","institution_name","provider_type","mission_group","state",
            "domain","metric_id","metric_name","year","value","unit","source_name","status"]
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
    log.info(f"normalised {len(rows)} rows -> {dest.name}")
    return rows

if __name__ == "__main__":
    print("run via run.py")
