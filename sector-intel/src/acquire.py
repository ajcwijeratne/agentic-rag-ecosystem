"""Fetch every file named in the manifest into data/raw, unchanged.

DoE/QILT block automated crawling, so a real refresh is often a supervised
download: paste the link into the manifest and drop the file in data/raw. This
module downloads when it can, and when it cannot it logs exactly which file the
operator must place by hand. Never stops the run for one missing file.
"""
from __future__ import annotations
import datetime
import yaml
from . import config

def _load_manifest() -> dict:
    return yaml.safe_load(config.MANIFEST.read_text(encoding="utf-8"))

def _save_manifest(man: dict) -> None:
    config.MANIFEST.write_text(yaml.safe_dump(man, sort_keys=False, allow_unicode=True), encoding="utf-8")

def run(force: bool = False) -> list[dict]:
    log = config.load_logger()
    man = _load_manifest()
    present = []
    for src in man.get("sources", []):
        raw = config.ROOT / src["raw_path"]
        raw.parent.mkdir(parents=True, exist_ok=True)
        if raw.exists() and raw.stat().st_size > 0 and not force:
            log.info(f"acquire skip (present): {src['key']} {raw.stat().st_size} bytes")
            src["retrieved_date"] = src.get("retrieved_date") or datetime.date.today().isoformat()
            present.append(src); continue
        url = src.get("url", "")
        if not url or "PLACEHOLDER" in url:
            log.warning(f"acquire MANUAL: {src['key']} has no real URL. "
                        f"Place the file at {src['raw_path']} by hand, then rerun.")
            continue
        try:
            import requests
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            raw.write_bytes(r.content)
            src["retrieved_date"] = datetime.date.today().isoformat()
            log.info(f"acquire ok: {src['key']} {len(r.content)} bytes from {url}")
            present.append(src)
        except Exception as e:  # noqa: BLE001
            log.error(f"acquire FAIL {src['key']}: {e}. Place {src['raw_path']} by hand.")
    _save_manifest(man)
    return present

if __name__ == "__main__":
    run()
