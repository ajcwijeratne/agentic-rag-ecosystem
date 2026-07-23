"""Central paths, reference-table loading and the metric dictionary.

One direction of flow: source file -> raw -> parsed -> normalised -> benchmarked
-> published. Every stage writes to disk; no stage overwrites raw.
"""
from __future__ import annotations
import csv, datetime, logging
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # sector-intel/
MANIFEST = ROOT / "manifest" / "sources.yaml"
REF = ROOT / "reference"
RAW = ROOT / "data" / "raw"
PARSED = ROOT / "data" / "parsed"
NORMALISED = ROOT / "data" / "normalised"
PUBLISHED = ROOT / "data" / "published"
LOGS = ROOT / "logs"
for d in (PARSED, NORMALISED, PUBLISHED, LOGS):
    d.mkdir(parents=True, exist_ok=True)

# Suppressed / not-published markers used across DoE and QILT tables.
SUPPRESSED = {"np", "n/a", "na", "n.p.", "-", "", "..", "confidential"}

def load_logger(name: str = "sector-intel") -> logging.Logger:
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    stamp = datetime.date.today().strftime("%Y%m%d")
    fh = logging.FileHandler(LOGS / f"run_{stamp}.log", encoding="utf-8")
    sh = logging.StreamHandler()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in (fh, sh):
        h.setFormatter(fmt); log.addHandler(h)
    return log

def _read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))

def load_reference() -> dict:
    """institutions, mission_groups, states, aliases, metric dictionary."""
    insts = {r["institution_id"]: r for r in _read_csv(REF / "institutions.csv")}
    groups = {r["institution_id"]: r["mission_group"] for r in _read_csv(REF / "mission_groups.csv")}
    states = {r["institution_id"]: r["state"] for r in _read_csv(REF / "states.csv")}
    for iid, r in insts.items():
        r["mission_group"] = groups.get(iid, "Unaligned")
        r["state"] = states.get(iid, "")
    aliases = {}
    ap = REF / "name_aliases.csv"
    if ap.exists():
        for r in _read_csv(ap):
            src = (r.get("source_name") or "").strip()
            if src and not src.startswith("#") and r.get("institution_id"):
                aliases[_norm_name(src)] = r["institution_id"]
    metrics = {r["metric_id"]: r for r in _read_csv(REF / "metric_dictionary.csv")}
    return {"institutions": insts, "aliases": aliases, "metrics": metrics}

def _norm_name(s: str) -> str:
    s = (s or "").strip().lower()
    for junk in ["the ", "  "]:
        s = s.replace(junk, " ").strip() if junk == "  " else (s[len(junk):] if s.startswith(junk) else s)
    out = "".join(ch for ch in s if ch.isalnum() or ch == " ")
    return " ".join(out.split())
