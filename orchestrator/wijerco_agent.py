"""
WijerCo Agent — grounded in the WijerCo folder.

System prompt construction follows the read order defined in
the WijerCo folder's AGENTS/CONTEXT.md:

  Always:
    1. ABOUT ME/about-me.md
    2. ABOUT ME/anti-ai-writing-style.md
    3. ABOUT ME/my-company.md

  Then (per task):
    4. AGENTS/departments/{dept}.md
    5. Relevant KNOWLEDGE BASE files (for client/sector-facing tasks)

  Then (if RAG context was retrieved):
    6. Retrieved chunks appended as a context block

Falls back to SKILL.md lookup (legacy Cowork path) if WIJERCO_PATH is not set
or the expected files are not found.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .cost_tracker import tracker as cost_tracker
from .multi_llm import call_model
from .fallback_chain import call_with_fallback

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

WIJERCO_PATH: Path = Path(os.getenv(
    "WIJERCO_PATH",
    r"C:\Users\ajwij\Claude Cowork\WijerCo",
))

# Versioned workforce definitions live with the application. Personal/company
# context remains in WIJERCO_PATH so it can be updated without duplicating it.
WIJERCO_WORKFORCE_PATH: Path = Path(os.getenv(
    "WIJERCO_WORKFORCE_PATH",
    str(Path(__file__).resolve().parents[1] / "workforce"),
))

# Fallback: legacy Cowork skills-plugin path
SKILLS_BASE: Path = Path(os.getenv(
    "WIJERCO_SKILLS_PATH",
    str(Path.home() / "AppData/Roaming/Claude/local-agent-mode-sessions/skills-plugin"),
))

# ─────────────────────────────────────────────────────────────────────────────
# Department → file mapping
# ─────────────────────────────────────────────────────────────────────────────

_DEPT_FILE: dict[str, str] = {
    "learning_design":       "learning-design",
    "academic_development":  "academic-development",
    "marketing_sales":       "marketing-sales",
    "operations":            "operations",
    "research_intelligence": "research-intelligence",
    "support":               "support",
    "content_studio":        "content-studio",
    "academic_affairs_registry": "academic-affairs-registry",
    "student_experience_success": "student-experience-success",
    "library_scholarly_services": "library-scholarly-services",
    "research_innovation": "research-innovation",
    "governance_risk_assurance": "governance-risk-assurance",
    "people_culture": "people-culture",
    "orchestrator":          "orchestrator",   # special: read ORCHESTRATOR.md
}

# Which knowledge base files to include per department
# (omit from operations / internal tasks to keep prompts lean)
_KB_FILES_BY_DEPT: dict[str, list[str]] = {
    "learning_design":       ["wijerco-services", "wijerco-positioning"],
    "academic_development":  ["wijerco-services", "wijerco-positioning"],
    "marketing_sales":       ["wijerco-services", "wijerco-positioning",
                              "wijerco-sector-context", "wijerco-competitors"],
    "research_intelligence": ["wijerco-sector-context", "wijerco-competitors",
                              "wijerco-diagnostic-sprint"],
    "support":               ["wijerco-services", "wijerco-positioning"],
    "content_studio":        ["wijerco-services", "wijerco-positioning"],
    "operations":            [],
    "academic_affairs_registry": ["wijerco-sector-context"],
    "student_experience_success": ["wijerco-positioning", "wijerco-sector-context"],
    "library_scholarly_services": ["wijerco-sector-context"],
    "research_innovation": ["wijerco-sector-context", "wijerco-positioning"],
    "governance_risk_assurance": ["wijerco-sector-context"],
    "people_culture": ["wijerco-positioning"],
    "orchestrator":          ["wijerco-services", "wijerco-positioning",
                              "wijerco-sector-context", "wijerco-competitors"],
}

# WijerCo task type → optimizer task classification
_DEPT_TASK_TYPE: dict[str, str] = {
    "learning_design":       "advisory",
    "academic_development":  "advisory",
    "marketing_sales":       "creative",
    "operations":            "summary",
    "research_intelligence": "reasoning",
    "support":               "creative",
    "content_studio":        "creative",
    "orchestrator":          "advisory",
    "academic_affairs_registry": "reasoning",
    "student_experience_success": "advisory",
    "library_scholarly_services": "reasoning",
    "research_innovation": "reasoning",
    "governance_risk_assurance": "reasoning",
    "people_culture": "advisory",
}


# ─────────────────────────────────────────────────────────────────────────────
# File readers
# ─────────────────────────────────────────────────────────────────────────────

def _read(path: Path) -> str | None:
    """Read a file, return its text or None if missing/unreadable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        return None


def _read_wijerco_file(*parts: str) -> str | None:
    """Read a file relative to WIJERCO_PATH."""
    return _read(WIJERCO_PATH / Path(*parts))


def _read_workforce_file(*parts: str) -> str | None:
    """Read a versioned workforce definition bundled with this application."""
    return _read(WIJERCO_WORKFORCE_PATH / Path(*parts))


def _strip_frontmatter(text: str | None) -> str | None:
    if not text:
        return None
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        try:
            end = lines.index("---", 1)
            return "\n".join(lines[end + 1:]).strip()
        except ValueError:
            pass
    return text.strip()


# ─────────────────────────────────────────────────────────────────────────────
# SKILL.md fallback (legacy)
# ─────────────────────────────────────────────────────────────────────────────

_LEGACY_SKILL_DIRS: dict[str, str] = {
    "learning_design":       "wijerco-learning-design",
    "academic_development":  "wijerco-academic-development",
    "marketing_sales":       "wijerco-marketing-sales",
    "operations":            "wijerco-operations",
    "research_intelligence": "wijerco-research-intelligence",
    "support":               "wijerco-support",
    "orchestrator":          "wijerco-orchestrator",
}


def _find_skill_md(department: str) -> str | None:
    skill_dir = _LEGACY_SKILL_DIRS.get(
        department,
        f"wijerco-{department.replace('_', '-')}",
    )
    # Check the versioned workforce and then the external WijerCo folder.
    bundled = WIJERCO_WORKFORCE_PATH / "SKILLS" / skill_dir / "SKILL.md"
    if bundled.exists():
        return _strip_frontmatter(_read(bundled))
    skill_path = WIJERCO_PATH / "SKILLS" / skill_dir / "SKILL.md"
    if skill_path.exists():
        text = _read(skill_path)
        if text:
            # Strip YAML frontmatter
            lines = text.splitlines()
            if lines and lines[0].strip() == "---":
                try:
                    end = lines.index("---", 1)
                    text = "\n".join(lines[end + 1:]).strip()
                except ValueError:
                    pass
            return text
    # Fall back to global skills plugin path
    for candidate in SKILLS_BASE.rglob(f"{skill_dir}/SKILL.md"):
        text = candidate.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if lines and lines[0].strip() == "---":
            try:
                end = lines.index("---", 1)
                text = "\n".join(lines[end + 1:]).strip()
            except ValueError:
                pass
        return text.strip()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# System prompt builder
# ─────────────────────────────────────────────────────────────────────────────

def _read_subagent(slug: str) -> str | None:
    """Read a specialist role, preserving legacy production-role aliases."""
    text = _read_workforce_file("AGENTS", "subagents", f"{slug}.md")
    if not text:
        text = _read_wijerco_file("AGENTS", "subagents", f"{slug}.md")
    if not text:
        try:
            from .wijerco_roster import canonical_subagent
            canonical = canonical_subagent(slug)
            if canonical and canonical != slug:
                text = _read_workforce_file("AGENTS", "subagents", f"{canonical}.md")
        except Exception:
            pass
    return _strip_frontmatter(text)


def _read_agent_capabilities(slug: str) -> list[tuple[str, str]]:
    """Load only the capability skills assigned to a selected specialist."""
    try:
        from .wijerco_roster import lookup_subagent
        agent = lookup_subagent(slug)
    except Exception:
        agent = None
    if not agent:
        return []
    loaded: list[tuple[str, str]] = []
    for skill in agent.get("skills", []):
        text = _read_workforce_file("SKILLS", "capabilities", skill, "SKILL.md")
        clean = _strip_frontmatter(text)
        if clean:
            loaded.append((skill, clean))
    return loaded


def _build_system_prompt(
    department:        str,
    rag_context:       list[dict] | None = None,
    subagent:          str | None = None,
    extra_instructions: str | None = None,
) -> str:
    """
    Build the system prompt following CONTEXT.md read order:
      1. ABOUT ME/about-me.md
      2. ABOUT ME/anti-ai-writing-style.md
      3. ABOUT ME/my-company.md
      4. AGENTS/departments/{dept}.md  (or ORCHESTRATOR.md for orchestrator)
      5. Relevant KNOWLEDGE BASE files
      6. RAG context (if supplied)

    Falls back to SKILL.md if the WijerCo folder is not found.
    """
    sections: list[str] = []

    # ── 1–3: Always-read ABOUT ME files ──────────────────────────────────
    for fname in ("about-me.md", "anti-ai-writing-style.md", "my-company.md"):
        text = _read_wijerco_file("ABOUT ME", fname)
        if text:
            sections.append(text)

    # ── 4: Department file ────────────────────────────────────────────────
    if department == "orchestrator":
        dept_text = (
            _read_workforce_file("AGENTS", "ORCHESTRATOR.md")
            or _read_wijerco_file("AGENTS", "ORCHESTRATOR.md")
        )
    elif department == "content_studio":
        # Content Studio remains a backward-compatible production capability.
        # Its organisational home is Marketing & Sales.
        dept_text = (
            _read_wijerco_file("AGENTS", "departments", "content-studio.md")
            or _read_workforce_file("AGENTS", "departments", "marketing-sales.md")
        )
    else:
        dept_slug = _DEPT_FILE.get(
            department,
            department.replace("_", "-"),
        )
        dept_text = (
            _read_workforce_file("AGENTS", "departments", f"{dept_slug}.md")
            or _read_wijerco_file("AGENTS", "departments", f"{dept_slug}.md")
        )

    if dept_text:
        sections.append(dept_text)
    else:
        # Fall back to SKILL.md if WijerCo folder isn't reachable
        skill_text = _find_skill_md(department)
        if skill_text:
            sections.append(skill_text)
        else:
            fallback_labels = {
                "learning_design":       "Learning Design department for WijerCo",
                "academic_development":  "Academic Development department for WijerCo",
                "marketing_sales":       "Marketing & Sales department for WijerCo",
                "operations":            "Operations department for WijerCo",
                "research_intelligence": "Research & Intelligence department for WijerCo",
                "support":               "Support department for WijerCo",
                "academic_affairs_registry": "Academic Affairs & Registry department for WijerCo",
                "student_experience_success": "Student Experience & Success department for WijerCo",
                "library_scholarly_services": "Library & Scholarly Services department for WijerCo",
                "research_innovation": "Research & Innovation department for WijerCo",
                "governance_risk_assurance": "Governance, Risk & Assurance department for WijerCo",
                "people_culture": "People & Culture department for WijerCo",
            }
            sections.append(
                f"You are the {fallback_labels.get(department, 'WijerCo agent')}. "
                "Be direct, concise, and deliver actionable work without filler."
            )

    # ── 4b: Specific subagent role (the individual "employee") ────────────
    if subagent:
        sub_text = _read_subagent(subagent)
        if sub_text:
            sections.append(
                "---\n\n## Your specific role\n\n"
                "You are this individual agent. This role definition takes "
                "precedence over the general department description above.\n\n"
                + sub_text
            )
        for skill_name, skill_text in _read_agent_capabilities(subagent):
            sections.append(f"---\n\n## Capability skill: {skill_name}\n\n{skill_text}")

    # ── 5: Knowledge base files ───────────────────────────────────────────
    kb_files = _KB_FILES_BY_DEPT.get(department, [])
    for kb_name in kb_files:
        kb_text = _read_wijerco_file("KNOWLEDGE BASE", f"{kb_name}.md")
        if kb_text:
            sections.append(f"---\n\n## Knowledge: {kb_name}\n\n{kb_text}")

    # ── 6: RAG context ────────────────────────────────────────────────────
    if rag_context:
        lines = ["\n\n---\n\n## Retrieved context\n"]
        for i, chunk in enumerate(rag_context[:8], 1):
            source = (
                chunk.get("source_agent")
                or chunk.get("source")
                or chunk.get("file")
                or "?"
            )
            text = chunk.get("text", "")
            lines.append(f"**[{i}] {source}**\n{text[:800]}")
        sections.append("\n\n".join(lines))

    # Candidate rule under test by the Self-Harness validator (in-memory only)
    if extra_instructions:
        sections.append(
            "---\n\n## Candidate rule under evaluation\n\n" + extra_instructions
        )

    # If we found nothing useful, raise so the caller can use a bare prompt
    if not sections:
        return (
            "You are a WijerCo AI assistant. "
            "Be direct and accurate. No filler."
        )

    return "\n\n---\n\n".join(sections)


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

async def call_wijerco_agent(
    department:           str,
    query:                str,
    rag_context:          list[dict] | None = None,
    conversation_history: list[dict] | None = None,
    force_model_key:      str | None = None,
    max_tier:             int = 3,
    subagent:             str | None = None,
) -> dict[str, Any]:
    """
    Call a WijerCo department agent using the proper context layer.

    System prompt = ABOUT ME + department file + (subagent role) + KB + RAG.
    Model is chosen by the token optimizer unless force_model_key is set.
    """
    system_prompt = _build_system_prompt(department, rag_context, subagent=subagent)
    task_type     = _DEPT_TASK_TYPE.get(department, "advisory")

    # Prepend long-term memories
    try:
        from memory.memory_agent import recall as memory_recall
        memory_block = await memory_recall(query=query, department=department)
        if memory_block:
            system_prompt = memory_block + "\n\n---\n\n" + system_prompt
    except Exception:
        pass

    response = await call_with_fallback(
        user_message    = query,
        system_prompt   = system_prompt,
        history         = conversation_history or [],
        force_model_key = force_model_key,
        force_task_type = task_type,
        max_tier        = max_tier,
    )

    cost_tracker.record(
        model_key     = response.model_key,
        model_label   = response.model_label,
        provider      = response.provider,
        task_type     = f"wijerco/{department}",
        input_tokens  = response.input_tokens,
        output_tokens = response.output_tokens,
        cost_usd      = response.cost_usd,
        latency_ms    = response.latency_ms,
        query         = query,
    )

    # Extract and store memorable facts in the background
    try:
        import asyncio
        from memory.memory_agent import extract_and_store
        asyncio.ensure_future(
            extract_and_store(department, query, response.content)
        )
    except Exception:
        pass

    return {
        "answer":        response.content,
        "department":    department,
        "model_key":     response.model_key,
        "model_label":   response.model_label,
        "provider":      response.provider,
        "task_type":     task_type,
        "cost_usd":      response.cost_usd,
        "latency_ms":    response.latency_ms,
        "input_tokens":  response.input_tokens,
        "output_tokens": response.output_tokens,
        "error":         response.error,
        # Backwards compat
        "model":         response.model_label,
        "tokens_used":   response.input_tokens + response.output_tokens,
    }
