"""
Voice telemetry
===============
Records what each spoken turn actually cost, stage by stage, so tuning is done
against evidence instead of impressions.

This exists because of a specific failure. Optimising the loop by hand, single
measurements varied by a factor of two under load — the same question measured
7.5s and 14.9s minutes apart. Two changes were made on the strength of one
sample each, and one of them (a blanket retrieval deadline) silently dropped the
vault context one run in four before a repeat measurement caught it. Medians
over many real turns are the only honest basis for a latency claim.

One JSON line per turn in logs/voice_metrics.jsonl, and a rollup at
GET /voice/metrics:

    {"ts": ..., "retrieval_s": 1.3, "first_token_s": 4.4, "first_audio_s": 4.7,
     "total_s": 6.1, "chunks": 12, "model": "...", "grounded": true}

Nothing here blocks or can fail a turn: every write is best-effort.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

METRICS_PATH: Path = Path(os.getenv("VOICE_METRICS_PATH", "logs/voice_metrics.jsonl"))
METRICS_ENABLED: bool = os.getenv("VOICE_METRICS", "true").lower() in ("1", "true", "yes")
# Keep the rollup cheap; a turn is a few hundred bytes.
METRICS_MAX_LINES: int = int(os.getenv("VOICE_METRICS_MAX_LINES", "5000"))


@dataclass
class TurnMetrics:
    """
    Timings for one spoken turn, all seconds from the start of the turn.

    `first_audio_s` is the number that matters: everything before it is silence
    the person is sitting through.
    """

    query:         str = ""
    retrieval_s:   float = 0.0
    first_token_s: float = 0.0
    first_audio_s: float = 0.0
    total_s:       float = 0.0
    chunks:        int = 0
    fragments:     int = 0
    answer_chars:  int = 0
    model:         str = ""
    route:         str = ""
    grounded:      bool = False     # did retrieval actually contribute
    ts:            float = field(default_factory=time.time)

    def stages(self) -> dict:
        """Time spent in each stage, rather than cumulative marks."""
        return {
            "retrieval":       round(self.retrieval_s, 3),
            "time_to_first_token": round(max(0.0, self.first_token_s - self.retrieval_s), 3),
            "chunking":        round(max(0.0, self.first_audio_s - self.first_token_s), 3),
            "rest_of_answer":  round(max(0.0, self.total_s - self.first_audio_s), 3),
        }


class TurnTimer:
    """
    Stopwatch for one turn.

        timer = TurnTimer(query)
        ...
        timer.mark_retrieval(len(chunks))
        timer.mark_first_token()
        timer.mark_first_audio()
        timer.finish(answer, model, route)
    """

    def __init__(self, query: str = "", route: str = ""):
        self._t0 = time.perf_counter()
        self.m = TurnMetrics(query=query[:200], route=route)

    def _elapsed(self) -> float:
        return time.perf_counter() - self._t0

    def mark_retrieval(self, chunks: int = 0) -> None:
        self.m.retrieval_s = self._elapsed()
        self.m.chunks = chunks
        self.m.grounded = chunks > 0

    def mark_first_token(self) -> None:
        if not self.m.first_token_s:
            self.m.first_token_s = self._elapsed()

    def mark_first_audio(self) -> None:
        if not self.m.first_audio_s:
            self.m.first_audio_s = self._elapsed()

    def finish(self, answer: str = "", model: str = "", fragments: int = 0) -> TurnMetrics:
        self.m.total_s = self._elapsed()
        self.m.answer_chars = len(answer or "")
        self.m.model = model
        self.m.fragments = fragments
        record(self.m)
        return self.m


def record(metrics: TurnMetrics) -> None:
    """Append one turn. Never raises — telemetry must not break a conversation."""
    if not METRICS_ENABLED:
        return
    try:
        METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with METRICS_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(metrics), ensure_ascii=False) + "\n")
    except Exception:
        pass


def _load(limit: int = METRICS_MAX_LINES) -> list:
    if not METRICS_PATH.exists():
        return []
    try:
        lines = METRICS_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def summary(last: int = 200) -> dict:
    """
    Medians over recent turns.

    Medians, not means: a single cold start or a provider retry skews an average
    badly, and the question being answered is "what does this usually feel like".
    """
    turns = _load()[-last:]
    if not turns:
        return {"turns": 0, "detail": "No spoken turns recorded yet."}

    def med(key: str) -> float:
        vals = [t.get(key) or 0.0 for t in turns if t.get(key)]
        return round(statistics.median(vals), 2) if vals else 0.0

    grounded = sum(1 for t in turns if t.get("grounded"))
    first = [t.get("first_audio_s") or 0.0 for t in turns if t.get("first_audio_s")]

    return {
        "turns": len(turns),
        "median": {
            "retrieval_s":   med("retrieval_s"),
            "first_token_s": med("first_token_s"),
            "first_audio_s": med("first_audio_s"),
            "total_s":       med("total_s"),
            "answer_chars":  med("answer_chars"),
        },
        "first_audio_s": {
            "min": round(min(first), 2) if first else 0.0,
            "max": round(max(first), 2) if first else 0.0,
        },
        "grounded_rate": round(grounded / len(turns), 3),
        "models": sorted({t.get("model", "") for t in turns if t.get("model")}),
        "slowest_stage_median": _slowest(turns),
    }


def _slowest(turns: list) -> str:
    """Which stage usually dominates — the thing worth working on next."""
    stages = {"retrieval": [], "time_to_first_token": [], "chunking": [], "rest_of_answer": []}
    for t in turns:
        retrieval = t.get("retrieval_s") or 0.0
        first_tok = t.get("first_token_s") or 0.0
        first_aud = t.get("first_audio_s") or 0.0
        total = t.get("total_s") or 0.0
        stages["retrieval"].append(retrieval)
        stages["time_to_first_token"].append(max(0.0, first_tok - retrieval))
        stages["chunking"].append(max(0.0, first_aud - first_tok))
        stages["rest_of_answer"].append(max(0.0, total - first_aud))
    medians = {k: statistics.median(v) for k, v in stages.items() if v}
    if not medians:
        return ""
    worst = max(medians, key=medians.get)
    return f"{worst} ({medians[worst]:.2f}s)"
