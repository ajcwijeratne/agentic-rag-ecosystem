# WijerCo Sector Intel — data pipeline (Phase 2)

Turns published government source files (DoE, QILT) into the benchmarked
long-format dataset the command centre reads. One direction of flow, every stage
writes to disk, no stage overwrites raw, and the run is idempotent: same sources
in, same output out.

```
source file → raw → parsed → normalised → benchmarked → published
```

## Quick start

```bash
# from the repo root, with pandas / openpyxl / PyYAML installed
cd sector-intel
python -m src.run --fixtures --publish
```

`--fixtures` synthesises DoE/QILT-shaped workbooks so the whole pipeline runs
without the real files. `--publish` copies the result to `../ui/sector_intel.json`
so the command centre picks it up. Drop `--fixtures` once real files are in place.

Output lands in `data/published/`: `sector_intel.json` (the Phase 3 contract),
`sector_intel.csv` (flat), `sector_intel.sqlite`, and `validation_report.json`.

## Going live with real data

1. Open `manifest/sources.yaml`. For each source table and release year, paste
   the official download URL over the `PLACEHOLDER` link. DoE and QILT block
   automated crawling, so if a download fails the log names the exact file to
   place in `data/raw/…` by hand.
2. Run `python -m src.run --publish` (no `--fixtures`).
3. `acquire` fetches or skips present files; `parse` reads each workbook per its
   config in `src/parse.py`; `resolve` maps printed names to institution IDs;
   `normalise` builds long format and derives C02 and D02; `benchmark` adds the
   medians, percentile and year-on-year; `validate` runs the gates.
4. Check `data/published/validation_report.json`. A run with open flags stays
   labelled indicative. Fix flags (usually a parser config or a missing alias),
   then rerun.

When a workbook's layout differs from the fixture shape, adjust that source's
entry in `src/parse.py` `PARSERS` — the sheet name, `header_row`, and the
`columns` map from printed header to metric ID. A layout change next year is a
config edit, not new code. The `# TODO(real data)` note marks what to confirm
against the first real file.

## Folder layout

```
sector-intel/
  manifest/sources.yaml        source registry: url, release year, retrieved date, parser, metric IDs
  reference/                   institutions, mission_groups, states, name_aliases, metric_dictionary
  data/raw/{doe,qilt}/         downloaded source files, never edited
  data/parsed/                 one tidy CSV per source table (+ _unmatched.csv)
  data/normalised/             long-format rows, pre-benchmark
  data/published/              sector_intel.{json,csv,sqlite} + validation_report.json
  src/                         acquire, parse, resolve, normalise, benchmark, validate, run, make_fixtures, config
  logs/                        run_YYYYMMDD.log
```

## Modules

- `acquire.py` — fetches manifest files into `data/raw`; supervised-download aware; never stops the run for one missing file.
- `parse.py` — per-source parsers; handles title rows, header bands, footnotes, and `np` suppressed values; emits tidy CSVs.
- `resolve.py` — printed name → institution ID: exact on a normalised name, then `name_aliases.csv`, then a fuzzy match that is logged for confirmation, never silently accepted. Unresolved names go to `_unmatched.csv`. Uses rapidfuzz if installed, else stdlib difflib.
- `normalise.py` — long format + metric metadata + group/state join; derives C02 (EFTSL ÷ academic FTE) and D02 (100 − international − other).
- `benchmark.py` — sector / group / state medians (with the count behind each), percentile rank, year-on-year; writes SQLite, CSV and the JSON contract. SQLite is built in a temp dir then copied (synced filesystems reject SQLite's live locking).
- `validate.py` — coverage, range, continuity, duplicate and unmatched gates; writes `validation_report.json`.
- `run.py` — orchestrates the stages; flags `--fixtures`, `--force`, `--publish`, `--publish-to`.

## The contract (what the command centre consumes)

Phase 3 reads `GET /sector/dataset` (added to `orchestrator/dashboard.py`) which
serves `data/published/sector_intel.json`, falling back to `ui/sector_intel.json`
then a seed. The JSON shape:

```
{ meta:{sample,status,years,groups,institution_count,metric_count,row_count,...},
  metrics:[{metric_id,metric_name,domain,unit,direction,definition}],
  domains:[...],
  institutions:[{institution_id,institution_name,short,provider_type,mission_group,state}],
  rows:[{institution_id,...,metric_id,year,value,unit,
         sector_median,group_median,state_median,percentile_rank,
         yoy_change,yoy_pct,sector_n,status}] }
```

Set `meta.sample:false` (the pipeline does this automatically when run without
`--fixtures`) to drop the command centre's "indicative sample" banner.

## Current state

Runs end to end on fixtures: 43 institutions × 15 Tier-1 metrics × 3 years =
1,935 rows, validation PASS, idempotent. The published output is fixture data
(synthetic, clearly labelled) until the real DoE/QILT URLs are added to the
manifest. Tier-2 metrics extend by adding rows to `reference/metric_dictionary.csv`
and the relevant parser column maps — the long format needs no schema change.
