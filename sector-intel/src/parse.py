"""Turn messy government workbooks into tidy per-source CSVs.

Government Excel is built for human reading: title rows, a header band spanning
rows, merged cells, footnotes below the data, suppressed 'np' cells, and layouts
that drift year to year. So parsing is configured per source, not written once.
Each parser knows its sheet, header row, and column->metric map. Output is one
tidy CSV per parsed file: source_name, metric_id, year, value, suppressed.
"""
from __future__ import annotations
import csv
from openpyxl import load_workbook
from . import config

# Per-source parser config. When a real file's layout differs, adjust here —
# a layout change next year is a config edit, not new code.
# TODO(real data): confirm sheet name and header_row against the actual DoE/QILT
# workbook before first live run; government headers occasionally shift a row.
PARSERS = {
    "doe_students": {
        "sheet": "Table 2.1 All students",
        "header_row": 4,               # 1-indexed row holding column names
        "name_col": "Provider",
        "columns": {                   # printed column header -> metric_id
            "All students (no.)": "A01",
            "EFTSL": "A02",
            "Commencing (no.)": "A03",
            "International (%)": "A04",
            "External/online load (%)": "A05",
            "First-year attrition (%)": "A06",
        },
    },
    "doe_staff_finance": {
        "sheet": "Table 4 Staff and finance",
        "header_row": 3,
        "name_col": "Provider",
        "columns": {
            "Staff FTE": "C01",
            "Total revenue ($m)": "D01",
            "International fee share (%)": "D03",
        },
    },
    "qilt_ses": {
        "sheet": "STMT_SES_ALL_1Y",
        "header_row": 4,
        "name_col": "Institution",
        "columns": {
            "Overall experience (%)": "B01",
            "Teaching quality (%)": "B02",
            "Student support (%)": "B04",
            "Graduate employment (%)": "B06",
        },
    },
}

def _clean(v):
    if v is None:
        return None, False
    s = str(v).strip()
    if s.lower() in config.SUPPRESSED:
        return None, True
    try:
        return float(s.replace(",", "").replace("$", "").replace("%", "")), False
    except ValueError:
        return None, False

def parse_source(src: dict) -> list[dict]:
    log = config.load_logger()
    cfg = PARSERS[src["parser"]]
    raw = config.ROOT / src["raw_path"]
    if not raw.exists():
        log.warning(f"parse skip (missing raw): {src['key']}")
        return []
    wb = load_workbook(raw, data_only=True, read_only=True)
    ws = wb[cfg["sheet"]] if cfg["sheet"] in wb.sheetnames else wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(c).strip() if c is not None else "" for c in rows[cfg["header_row"] - 1]]
    idx = {h: i for i, h in enumerate(hdr)}
    name_i = idx.get(cfg["name_col"])
    if name_i is None:
        log.error(f"parse FAIL {src['key']}: name column '{cfg['name_col']}' not in header {hdr}")
        return []
    col_i = {mid: idx[h] for h, mid in cfg["columns"].items() if h in idx}
    out = []
    for r in rows[cfg["header_row"]:]:
        if not r or name_i >= len(r) or r[name_i] is None:
            continue
        name = str(r[name_i]).strip()
        if not name or name.lower().startswith("np ="):   # footnote row
            continue
        for mid, ci in col_i.items():
            val, supp = _clean(r[ci] if ci < len(r) else None)
            out.append({"source_name": name, "metric_id": mid,
                        "year": src["release_year"], "value": val, "suppressed": int(supp)})
    dest = config.PARSED / f"{src['key']}.csv"
    with open(dest, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source_name", "metric_id", "year", "value", "suppressed"])
        w.writeheader(); w.writerows(out)
    log.info(f"parsed {src['key']}: {len(out)} observations -> {dest.name}")
    return out

def run(sources: list[dict]) -> list[dict]:
    allrows = []
    for src in sources:
        allrows += parse_source(src)
    return allrows

if __name__ == "__main__":
    import yaml
    man = yaml.safe_load(config.MANIFEST.read_text())
    run(man["sources"])
