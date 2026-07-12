"""
Golden-answer set for RAG evals, drawn from the WijerCo knowledge base so the
expected facts are true. Each record names the source file the answer should come
from, the question, and substrings that a correct answer must contain.

Used by tests/eval/test_rag_golden.py (marked `live` — needs the wijerco_knowledge
collection indexed and a reachable model).

Keep this small and curated. Score on expected facts and style, not exact strings,
so it stays maintainable as models change.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GoldenItem:
    id:             str
    question:       str
    source_file:    str                 # KB file the answer should draw on
    expected_any:   list[str] = field(default_factory=list)   # at least one must appear
    expected_all:   list[str] = field(default_factory=list)   # all must appear


GOLDEN: list[GoldenItem] = [
    GoldenItem(
        id="g-sprint-price",
        question="What does the WijerCo Diagnostic Sprint cost and how long does it take?",
        source_file="KNOWLEDGE BASE/wijerco-diagnostic-sprint.md",
        expected_all=["4,000"],
        expected_any=["2-week", "2 week", "two week", "two-week"],
    ),
    GoldenItem(
        id="g-entry-point",
        question="How does a new WijerCo client engagement start?",
        source_file="KNOWLEDGE BASE/wijerco-services.md",
        expected_any=["20-minute", "20 minute", "triage call"],
    ),
    GoldenItem(
        id="g-one-line",
        question="What is WijerCo's one-line position?",
        source_file="KNOWLEDGE BASE/wijerco-positioning.md",
        expected_any=["teaching quality", "faculty workload", "AI safely", "adopt AI"],
    ),
    GoldenItem(
        id="g-core-diagnosis",
        question="What is WijerCo's core diagnosis of Australian higher education?",
        source_file="KNOWLEDGE BASE/wijerco-sector-context.md",
        expected_any=["research prestige", "international student revenue", "teaching and learning"],
    ),
    GoldenItem(
        id="g-teqsa",
        question="Why does the TEQSA teaching-qualification requirement matter for institutions?",
        source_file="KNOWLEDGE BASE/wijerco-sector-context.md",
        expected_any=["forcing function", "get ahead", "Bootcamp", "Leadership"],
    ),
    GoldenItem(
        id="g-competitors",
        question="Who are WijerCo's competitors among the large advisory firms?",
        source_file="KNOWLEDGE BASE/wijerco-competitors.md",
        expected_any=["Deloitte", "KPMG", "PwC", "EY"],
    ),
    GoldenItem(
        id="g-how-wins",
        question="How does WijerCo win against broadly capable competitors?",
        source_file="KNOWLEDGE BASE/wijerco-competitors.md",
        expected_any=["specificity", "delivery experience", "implementable"],
    ),
    GoldenItem(
        id="g-services-count",
        question="How many core services does WijerCo offer and what is the flagship entry service?",
        source_file="KNOWLEDGE BASE/wijerco-services.md",
        expected_any=["Diagnostic Sprint"],
    ),
    GoldenItem(
        id="g-sprint-deliverables",
        question="What does a client receive from the Diagnostic Sprint?",
        source_file="KNOWLEDGE BASE/wijerco-diagnostic-sprint.md",
        expected_any=["roadmap", "templates", "presentation"],
    ),
    GoldenItem(
        id="g-sprint-clienttime",
        question="How much client time does the Diagnostic Sprint require?",
        source_file="KNOWLEDGE BASE/wijerco-diagnostic-sprint.md",
        expected_any=["4-6 hours", "4–6 hours", "4 to 6 hours"],
    ),
]
