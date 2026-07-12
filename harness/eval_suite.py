"""
Eval Suite + Judges
===================
The verifier that grounds the Self-Harness loop.

Tasks: a seed set of representative WijerCo prompts, tagged by department and
split (held_in for optimisation, held_out to catch overfitting). Edit / extend
TASKS freely — the loop reads whatever is here.

Judges:
  • deterministic_score() — fast, free rule checks against Aaron's style:
      banned words/constructions, em-dashes, length, presence of specifics.
  • llm_judge() — a cheap model scores quality + style adherence 0..1.
  • score_output() — combines both into one 0..1 score plus a list of issues.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────────
# Seed tasks
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class EvalTask:
    id:         str
    department: str
    split:      str        # "held_in" | "held_out"
    prompt:     str


TASKS: list[EvalTask] = [
    # Marketing & Sales
    EvalTask("ms-1", "marketing_sales", "held_in",  "Draft a LinkedIn post on why TEQSA's teaching-qualification push will reshape academic leadership."),
    EvalTask("ms-2", "marketing_sales", "held_in",  "Write a 120-word outreach email to a university DVC offering a diagnostic sprint."),
    EvalTask("ms-3", "marketing_sales", "held_out", "Write website copy for WijerCo's learning design service, 80 words."),
    # Research & Intelligence
    EvalTask("ri-1", "research_intelligence", "held_in",  "Summarise the key forces shaping Australian higher education in 2025 and the implication for a mid-size university."),
    EvalTask("ri-2", "research_intelligence", "held_in",  "A client's first-year retention dropped 6 points. List the three most likely causes and what evidence would confirm each."),
    EvalTask("ri-3", "research_intelligence", "held_out", "Benchmark a regional university's student experience against QILT sector medians and state one strategic implication."),
    # Learning Design
    EvalTask("ld-1", "learning_design", "held_in",  "Write three measurable learning outcomes for a postgraduate unit on data-informed decision making."),
    EvalTask("ld-2", "learning_design", "held_out", "Propose an assessment structure for a 12-week online unit that prioritises feedback and authentic tasks."),
    # Academic Development
    EvalTask("ad-1", "academic_development", "held_in",  "Outline a 90-minute workshop to build assessment-design capability in academic staff."),
    EvalTask("ad-2", "academic_development", "held_out", "Write a coaching conversation brief for a mid-career academic moving into a course-coordinator role."),
    # Operations
    EvalTask("op-1", "operations", "held_in",  "Draft a one-page project plan for a six-week curriculum review engagement, with owners and milestones."),
    EvalTask("op-2", "operations", "held_out", "Summarise this month's engagement margins and flag the two that need attention."),
    # Support
    EvalTask("sp-1", "support", "held_in",  "Draft a reply to a prospect who asked what a diagnostic sprint costs and how long it takes."),
    EvalTask("sp-2", "support", "held_out", "Triage this message: 'We need help but the committee meets in two weeks and we have no budget approved.'"),
]


def tasks_for(department: str | None = None, split: str | None = None) -> list[EvalTask]:
    out = TASKS
    if department:
        out = [t for t in out if t.department == department]
    if split:
        out = [t for t in out if t.split == split]
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic checks (Aaron's anti-AI style)
# ─────────────────────────────────────────────────────────────────────────────

BANNED_WORDS = [
    "elevate", "disrupt", "revolutionize", "revolutionise", "foster", "reimagine",
    "transform", "leverage", "unlock", "scalable", "optimise", "optimize", "empower",
    "innovate", "holistic", "cutting-edge", "next-gen", "seamless", "dynamic",
    "frictionless", "agile", "mission-critical", "thought-leader", "game-changer",
    "ecosystem", "actionable insights", "quick win", "delve", "tapestry", "myriad",
    "plethora", "realm", "robust", "vibrant", "navigate the", "harness", "embark",
    "journey", "landscape", "in today's", "furthermore", "moreover", "additionally",
]

BANNED_CONSTRUCTIONS = [
    r"not just .*?,? but", r"not only .*?,? but also", r"it'?s not about .*?,? it'?s about",
    r"whether you'?re", r"in the realm of", r"in the world of", r"at the heart of",
    r"stands? as a testament", r"plays? a (?:crucial|key) role",
]


@dataclass
class ScoreResult:
    score:   float                      # 0..1, higher is better
    issues:  list[str] = field(default_factory=list)
    detail:  dict = field(default_factory=dict)


def deterministic_score(text: str) -> ScoreResult:
    """Rule-based style/quality score. 1.0 = clean, deductions per violation."""
    issues: list[str] = []
    t = text.lower()

    banned_hits = [w for w in BANNED_WORDS if re.search(rf"\b{re.escape(w)}\b", t)]
    for w in banned_hits:
        issues.append(f"banned word: '{w}'")

    constr_hits = [p for p in BANNED_CONSTRUCTIONS if re.search(p, t)]
    for p in constr_hits:
        issues.append(f"banned construction: /{p}/")

    if "—" in text or "--" in text:
        issues.append("em-dash used (Aaron bans em-dashes)")

    # Specificity: reward presence of digits or proper nouns
    has_digit = bool(re.search(r"\d", text))
    has_proper = len(re.findall(r"\b[A-Z][a-z]{2,}", text)) >= 2
    if not has_digit and not has_proper:
        issues.append("no specifics (no numbers or named entities)")

    # Length sanity (very rough; per-task length isn't enforced here)
    words = len(text.split())
    if words < 8:
        issues.append("too short to be useful")

    # Score: start at 1, subtract weighted penalties
    penalty = (
        0.08 * len(banned_hits) +
        0.10 * len(constr_hits) +
        0.10 * (1 if ("—" in text or "--" in text) else 0) +
        0.15 * (1 if (not has_digit and not has_proper) else 0) +
        0.25 * (1 if words < 8 else 0)
    )
    score = max(0.0, 1.0 - penalty)
    return ScoreResult(score=score, issues=issues, detail={
        "banned_words": banned_hits, "constructions": constr_hits, "words": words,
    })


# ─────────────────────────────────────────────────────────────────────────────
# LLM judge
# ─────────────────────────────────────────────────────────────────────────────

_JUDGE_SYSTEM = """\
You are a strict editor grading an AI assistant's output for Aaron, a senior
Australian higher-education leader. Grade on:
1. Leads with the point, no preamble or throat-clearing.
2. Specific: numbers, names, dates, examples — not vague assertions.
3. Clean style: active voice, short sentences, no buzzwords, no em-dashes.
4. Answers the task fully and usefully.

Return ONLY a JSON object: {"score": 0.0-1.0, "issues": ["...", "..."]}
where score is overall quality and issues are concrete problems (empty if none).
"""


async def llm_judge(task_prompt: str, output: str) -> ScoreResult:
    """Score an output 0..1 with a cheap model. Falls back to neutral on error."""
    import json
    from orchestrator.fallback_chain import call_with_fallback

    try:
        resp = await call_with_fallback(
            user_message    = f"TASK:\n{task_prompt}\n\nOUTPUT:\n{output[:3000]}",
            system_prompt   = _JUDGE_SYSTEM,
            force_task_type = "fast",
            max_tier        = 1,
        )
        raw = (resp.content or "").strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        return ScoreResult(
            score=float(data.get("score", 0.5)),
            issues=list(data.get("issues", [])),
            detail={"judge_model": resp.model_label},
        )
    except Exception:
        return ScoreResult(score=0.5, issues=["judge unavailable"], detail={})


async def score_output(task_prompt: str, output: str) -> ScoreResult:
    """Combine deterministic + LLM judge into one score (60% LLM, 40% rules)."""
    det = deterministic_score(output)
    jud = await llm_judge(task_prompt, output)
    combined = round(0.4 * det.score + 0.6 * jud.score, 4)
    return ScoreResult(
        score=combined,
        issues=det.issues + jud.issues,
        detail={"deterministic": det.score, "llm": jud.score},
    )
