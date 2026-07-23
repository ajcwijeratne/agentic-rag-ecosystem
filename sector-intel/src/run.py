"""Orchestrate the pipeline: acquire -> parse -> resolve -> normalise ->
benchmark -> validate. A stage that fails stops the run and logs why.

Flags:
  --fixtures     synthesise DoE/QILT-shaped workbooks first (no real files needed)
  --force        re-download even if a raw file is present
  --publish      copy published/sector_intel.json to the command centre (ui/)
  --publish-to P copy the published json to an explicit path
Run from the repo:  python -m sector-intel.src.run --fixtures --publish
"""
from __future__ import annotations
import argparse, shutil, sys
from pathlib import Path
import yaml
from . import config, acquire, parse, resolve, normalise, benchmark, validate

# default publish target: the command centre next to command_centre.html
UI_TARGET = config.ROOT.parent / "ui" / "sector_intel.json"

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="WijerCo Sector Intel pipeline")
    ap.add_argument("--fixtures", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--publish-to", default=None)
    args = ap.parse_args(argv)
    log = config.load_logger()
    ref = config.load_reference()

    fixture = args.fixtures
    if fixture:
        from . import make_fixtures
        make_fixtures.build()

    man = yaml.safe_load(config.MANIFEST.read_text(encoding="utf-8"))
    present = acquire.run(force=args.force) or man.get("sources", [])
    parsed = parse.run(present)
    if not parsed:
        log.error("no parsed observations — stopping. Place source files in data/raw or use --fixtures.")
        return 2
    resolved, unmatched = resolve.run(parsed, ref)
    long_rows = normalise.run(resolved, ref)
    dataset = benchmark.run(long_rows, ref, fixture=fixture)
    report = validate.run(dataset, ref)

    if args.publish or args.publish_to:
        target = Path(args.publish_to) if args.publish_to else UI_TARGET
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config.PUBLISHED / "sector_intel.json", target)
        log.info(f"published to {target}")

    log.info(f"DONE — rows={dataset['meta']['row_count']} "
             f"metrics={dataset['meta']['metric_count']} "
             f"validation={'PASS' if report['passed'] else str(report['flag_count'])+' flags'}")
    return 0 if report["passed"] else 1

if __name__ == "__main__":
    sys.exit(main())
