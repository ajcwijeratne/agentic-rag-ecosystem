"""Map every source-printed institution name to a stable institution_id.

The same university appears under different names across sources: a leading
'The', abbreviations, trailing notes, former names. Three passes: exact on a
normalised name, then the alias table, then a fuzzy match proposed for review
(never auto-accepted silently — it is logged). Unresolved names go to an
unmatched report; a person adds them to reference/name_aliases.csv and reruns.
"""
from __future__ import annotations
import csv
from . import config

try:
    from rapidfuzz import process, fuzz
    _HAVE_RF = True
except Exception:                      # noqa: BLE001
    import difflib
    _HAVE_RF = False

FUZZ_THRESHOLD = 88

def _candidates(ref: dict) -> dict:
    """normalised name/short -> institution_id"""
    m = {}
    for iid, r in ref["institutions"].items():
        m[config._norm_name(r["institution_name"])] = iid
        if r.get("short"):
            m[config._norm_name(r["short"])] = iid
    m.update(ref["aliases"])            # already normalised keys
    return m

def _fuzzy(name_norm: str, cand: dict):
    if _HAVE_RF:
        hit = process.extractOne(name_norm, list(cand.keys()), scorer=fuzz.WRatio)
        if hit and hit[1] >= FUZZ_THRESHOLD:
            return cand[hit[0]], hit[1]
        return None, hit[1] if hit else 0
    close = difflib.get_close_matches(name_norm, list(cand.keys()), n=1, cutoff=FUZZ_THRESHOLD / 100)
    if close:
        return cand[close[0]], round(difflib.SequenceMatcher(None, name_norm, close[0]).ratio() * 100)
    return None, 0

def run(parsed_rows: list[dict], ref: dict) -> tuple[list[dict], list[dict]]:
    log = config.load_logger()
    cand = _candidates(ref)
    resolved, unmatched, seen_unmatched, warned = [], [], set(), set()
    for row in parsed_rows:
        nm = config._norm_name(row["source_name"])
        iid = cand.get(nm)
        how = "exact"
        if not iid:
            iid, score = _fuzzy(nm, cand)
            how = f"fuzzy:{score}"
            if iid and row['source_name'] not in warned:
                warned.add(row['source_name'])
                log.warning(f"resolve fuzzy {row['source_name']!r} -> {iid} ({how}); confirm and add to name_aliases.csv")
        if iid:
            resolved.append({**row, "institution_id": iid, "resolved_by": how})
        else:
            if row["source_name"] not in seen_unmatched:
                seen_unmatched.add(row["source_name"])
                unmatched.append({"source_name": row["source_name"]})
    if unmatched:
        dest = config.PARSED / "_unmatched.csv"
        with open(dest, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["source_name"]); w.writeheader(); w.writerows(unmatched)
        log.warning(f"resolve: {len(unmatched)} unmatched names -> {dest.name}")
    else:
        (config.PARSED / "_unmatched.csv").write_text("source_name\n", encoding="utf-8")
    log.info(f"resolved {len(resolved)} rows; unmatched {len(unmatched)}; matcher={'rapidfuzz' if _HAVE_RF else 'difflib'}")
    return resolved, unmatched

if __name__ == "__main__":
    print("run via run.py")
