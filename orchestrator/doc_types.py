"""Document deliverable types.

One registry shared by the Deliverables section, the production pipeline and
governance. It imports nothing from the rest of the orchestrator, so any module
can import it without creating a cycle.

A document is a production whose `format` is one of DOC_FORMATS. It moves
through the same state machine as a video (idea -> brief -> research -> outline
-> draft -> render -> review -> publish), skips `asset_plan`, and is rendered
to a file instead of a video. Its canonical text is the body of its
Deliverables note in the vault; every file is rendered from that note.
"""

from __future__ import annotations

from typing import Any

# Department and subagent slugs are the live roster's (GET /wijerco/roster).
DOC_TYPES: dict[str, dict[str, Any]] = {
    "client_briefing": {
        "label": "Client briefing",
        "cap": "Sector Intelligence & Evidence",
        "output": "docx",
        "length": "4 to 8 pages",
        "words": "1,400 to 2,200",
        "sections": ["Summary", "Context", "Findings", "Implications", "Recommendations", "Sources"],
        "drafting": ("research_intelligence", "sector-intelligence-analyst"),
        "research": ("research_intelligence", "research-analyst"),
        "reviewer": ("operations", "quality-reviewer"),
        "polish": None,
        "guidance": (
            "A briefing tells a client leader what the evidence says and what it means for them. "
            "Lead with a three-point summary. Tie every figure to a listed source."
        ),
    },
    "decision_paper": {
        "label": "Decision paper",
        "cap": "Strategy & Change Advisory",
        "output": "docx",
        "length": "4 to 10 pages",
        "words": "1,400 to 2,800",
        "sections": ["Decision sought", "Background", "Options", "Recommendation",
                     "Implementation", "Risks", "Sources"],
        "drafting": ("research_intelligence", "insights-strategist"),
        "research": ("research_intelligence", "research-analyst"),
        "reviewer": ("operations", "quality-reviewer"),
        "polish": None,
        "guidance": (
            "A decision paper asks a named body to decide one thing. State the decision in the first "
            "sentence. Compare the options in one table with benefits, costs and risks. Recommend one."
        ),
    },
    "proposal": {
        "label": "Proposal",
        "cap": "Business Development",
        "output": "docx",
        "length": "5 to 12 pages",
        "words": "1,800 to 3,200",
        "sections": ["Understanding of need", "Approach", "Deliverables and timeline", "Team", "Next steps"],
        "drafting": ("marketing_sales", "sales-manager"),
        "research": ("research_intelligence", "research-analyst"),
        "reviewer": ("operations", "quality-reviewer"),
        "polish": ("marketing_sales", "copywriter"),
        "guidance": (
            "A proposal shows the client we understood their need and sets out how WijerCo would meet it. "
            "It carries no fees, prices, rates or cost estimates: Aaron discusses those separately."
        ),
    },
    "slide_deck": {
        "label": "Slide deck",
        "cap": "Presentations",
        "output": "pptx",
        "length": "8 to 12 slides",
        "sections": [],
        "drafting": ("marketing_sales", "content-creator"),
        "research": None,
        "reviewer": ("operations", "quality-reviewer"),
        "polish": None,
        "needs_source": True,
        "guidance": (
            "A deck carries one message per slide, drawn from an existing deliverable. At most five short "
            "bullets per slide. Every slide has speaker notes that say what to explain out loud."
        ),
    },
}

DOC_FORMATS = frozenset(DOC_TYPES)

# The only gate a document passes before it is client-ready. The system never
# sends a document anywhere, so external_publish and public_claim do not apply.
DOC_GATES = ("client_sensitive",)

# House rules every document agent receives.
GUARDRAILS = (
    "House rules for this document:\n"
    "- Write in Australian English. Short sentences, active voice, plain words.\n"
    "- Do not use em dashes. Use commas, full stops or semicolons.\n"
    "- Use only facts from the brief, the listed sources and the retrieved context. Never invent "
    "figures, names, dates, quotes, clients or results. If a needed fact is missing, write "
    "[To confirm: what is missing] instead.\n"
    "- State a cost, price, fee or rate only when it appears in a listed source, and cite it.\n"
    "- Never claim anything was sent, published, scheduled or agreed."
)


def is_document(fmt: str | None) -> bool:
    return bool(fmt) and fmt in DOC_FORMATS


def get_type(key: str) -> dict[str, Any]:
    if key not in DOC_TYPES:
        raise KeyError(key)
    return DOC_TYPES[key]


def public_types() -> list[dict[str, Any]]:
    """The registry as the composer needs it (no agent routing details)."""
    out = []
    for key, spec in DOC_TYPES.items():
        out.append({
            "key": key,
            "label": spec["label"],
            "cap": spec["cap"],
            "output": spec["output"],
            "length": spec["length"],
            "sections": list(spec["sections"]),
            "needs_source": bool(spec.get("needs_source")),
            "drafted_by": "/".join(spec["drafting"]),
            "reviewed_by": "/".join(spec["reviewer"]),
        })
    return out
