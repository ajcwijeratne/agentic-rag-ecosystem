"""Routing eval: measure the classifier against a hand-labelled gold set.

The router (classifier.classify) has always graded itself on self-consistency.
This gives it an external judge: 50 real queries, labelled once by Aaron, kept
in data/router_labels.jsonl. From that we get accuracy, a per-label breakdown,
a confusion matrix, and the actual misses, plus an optional embedding-centroid
build that feeds ROUTER_USE_EMBEDDING.

Workflow:
  python -m orchestrator.router_eval --template 50   # draft the label file from real logs
  # Aaron fills the "label" field in data/router_labels.jsonl (~1 hour)
  python -m orchestrator.router_eval --eval          # score the classifier
  python -m orchestrator.router_eval --build-centroids
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import classifier

_ROOT = Path(__file__).resolve().parent.parent
LABELS_PATH = Path(os.getenv("ROUTER_LABELS_PATH", str(_ROOT / "data" / "router_labels.jsonl")))
TEMPLATE_PATH = _ROOT / "data" / "router_labels.template.jsonl"
COST_LOG = _ROOT / "logs" / "cost_log.jsonl"
DAEMON_LOG = _ROOT / "logs" / "daemon.jsonl"

# Valid gold labels: the classifier's task types plus its two special outputs.
LABELS = tuple(classifier.TASK_SIGNALS.keys()) + ("advisory", "long_context")


def _real_queries(limit: int = 200) -> list[str]:
    """Unique real queries the system has actually seen, newest first."""
    seen: set[str] = set()
    out: list[str] = []

    def _add(text: str) -> None:
        q = (text or "").strip()
        if len(q) < 8 or q in seen:
            return
        seen.add(q)
        out.append(q)

    for path, key in ((COST_LOG, "query_preview"), (DAEMON_LOG, "title")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            try:
                _add(str(json.loads(line).get(key) or ""))
            except Exception:
                continue
            if len(out) >= limit:
                return out
    return out


def emit_template(n: int = 50) -> dict[str, Any]:
    """Draft a labelling file from real queries, prefilled with the router's own
    guess so Aaron only corrects rather than labels from scratch."""
    queries = _real_queries(limit=max(n * 2, 100))[:n]
    TEMPLATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TEMPLATE_PATH.open("w", encoding="utf-8") as f:
        for q in queries:
            suggested = classifier.classify(q).task_type
            f.write(json.dumps({"query": q, "suggested": suggested, "label": ""},
                               ensure_ascii=False) + "\n")
    return {"template": str(TEMPLATE_PATH), "count": len(queries),
            "labels": list(LABELS),
            "note": "Fill the 'label' field on each line, then save as "
                    f"{LABELS_PATH.name} in the same folder."}


def load_labels() -> list[dict[str, str]]:
    """Read the gold set. Accepts the labelled file, or a template whose 'label'
    fields have been filled in."""
    rows: list[dict[str, str]] = []
    path = LABELS_PATH if LABELS_PATH.exists() else TEMPLATE_PATH
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            query, label = str(item.get("query") or ""), str(item.get("label") or "").strip()
            if query and label:
                rows.append({"query": query, "label": label})
    except Exception:
        pass
    return rows


def evaluate() -> dict[str, Any]:
    """Score classifier.classify against the gold set."""
    gold = load_labels()
    if not gold:
        return {"ok": False, "reason": "no labelled queries found; run --template then label them",
                "labels_path": str(LABELS_PATH)}
    correct = 0
    per_label: dict[str, dict[str, int]] = {}
    confusion: dict[str, dict[str, int]] = {}
    misses: list[dict[str, str]] = []
    unknown_labels: set[str] = set()
    for row in gold:
        truth = row["label"]
        if truth not in LABELS:
            unknown_labels.add(truth)
        pred = classifier.classify(row["query"]).task_type
        pl = per_label.setdefault(truth, {"n": 0, "hits": 0})
        pl["n"] += 1
        confusion.setdefault(truth, {})
        confusion[truth][pred] = confusion[truth].get(pred, 0) + 1
        if pred == truth:
            correct += 1
            pl["hits"] += 1
        else:
            misses.append({"query": row["query"][:120], "truth": truth, "predicted": pred})
    total = len(gold)
    return {
        "ok": True,
        "total": total,
        "accuracy": round(correct / total, 4),
        "correct": correct,
        "per_label": {k: {**v, "recall": round(v["hits"] / v["n"], 3)} for k, v in per_label.items()},
        "confusion": confusion,
        "misses": misses[:30],
        "unknown_labels": sorted(unknown_labels),
        "embedding_enabled": classifier.USE_EMBEDDING,
    }


def build_centroids() -> dict[str, Any]:
    """Average per-label embeddings from the gold set into the centroid file the
    classifier's optional embedding stage reads. Skips cleanly if the embedder
    is unavailable."""
    gold = load_labels()
    if not gold:
        return {"ok": False, "reason": "no labelled queries"}
    try:
        import asyncio

        from rag.embedder import embed_text
    except Exception as exc:
        return {"ok": False, "reason": f"embedder unavailable: {exc}"}

    buckets: dict[str, list[list[float]]] = {}
    loop = asyncio.new_event_loop()
    try:
        for row in gold:
            vec = loop.run_until_complete(embed_text(row["query"]))
            buckets.setdefault(row["label"], []).append(list(vec))
    finally:
        loop.close()
    centroids = {
        label: [sum(col) / len(vecs) for col in zip(*vecs)]
        for label, vecs in buckets.items() if vecs
    }
    out = Path(classifier._CENTROID_PATH)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(centroids), encoding="utf-8")
    return {"ok": True, "labels": sorted(centroids), "path": str(out),
            "note": "set ROUTER_USE_EMBEDDING=1 to activate"}


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if "--template" in args:
        i = args.index("--template")
        n = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 50
        _print(emit_template(n))
    elif "--build-centroids" in args:
        _print(build_centroids())
    elif "--eval" in args:
        _print(evaluate())
    else:
        print("usage: python -m orchestrator.router_eval [--template N | --eval | --build-centroids]")
