"""
WijerCo Intent Router
======================
Classifies a user query into one of:
  - A WijerCo department (learning_design, academic_development, marketing_sales,
    operations, research_intelligence, support)
  - "rag" — pure knowledge retrieval / web search
  - "hybrid" — needs both RAG context AND WijerCo advisory synthesis

Uses keyword heuristics first (fast, free). Falls back to a lightweight
local LLM call if confidence is low.
"""

from __future__ import annotations

import os
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

WijerCoDept = Literal[
    "learning_design",
    "academic_development",
    "marketing_sales",
    "operations",
    "research_intelligence",
    "support",
    "academic_affairs_registry",
    "student_experience_success",
    "library_scholarly_services",
    "research_innovation",
    "governance_risk_assurance",
    "people_culture",
]

RouteTarget = Literal[
    "learning_design",
    "academic_development",
    "marketing_sales",
    "operations",
    "research_intelligence",
    "support",
    "academic_affairs_registry",
    "student_experience_success",
    "library_scholarly_services",
    "research_innovation",
    "governance_risk_assurance",
    "people_culture",
    # Backwards-compatible capability route. Content Studio is now a workflow
    # within Marketing & Sales, not a seventh department.
    "content_studio",
    "rag",
    "hybrid",
]


class IntentClassification(BaseModel):
    target: RouteTarget
    department: WijerCoDept | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str


# ---------------------------------------------------------------------------
# Keyword signal maps
# ---------------------------------------------------------------------------

_DEPT_SIGNALS: dict[WijerCoDept, list[str]] = {
    "learning_design": [
        "curriculum", "course design", "module", "learning outcome", "assessment",
        "learning materials", "instructional", "program design", "unit outline",
        "course structure", "subject", "accreditation", "AQF", "TEQSA",
        "graduate attribute", "constructive alignment",
    ],
    "academic_development": [
        "workshop", "training", "academic staff", "CPD", "professional development",
        "coaching", "facilitation", "session plan", "capability", "career plan",
        "teaching practice", "reflective practice", "onboarding academics",
    ],
    "marketing_sales": [
        "proposal", "pitch", "linkedin", "outreach", "email campaign", "newsletter",
        "website copy", "social media", "SEO", "GEO", "thought leadership",
        "win client", "tender", "blog post", "article", "content strategy",
        "brand", "positioning", "messaging",
    ],
    "operations": [
        "invoice", "project plan", "budget", "timeline", "milestone", "dashboard",
        "report", "KPI", "cashflow", "hire", "onboarding", "business plan",
        "client engagement", "contract", "resource plan", "capacity",
    ],
    "research_intelligence": [
        "research", "evidence", "literature", "sector intelligence", "QILT",
        "HEIMS", "TEQSA", "AQF", "enrolment data", "retention", "student experience",
        "benchmark", "white paper", "policy", "briefing", "analysis", "data",
        "competition", "higher education sector", "trends",
    ],
    "support": [
        "email reply", "respond to", "follow up", "schedule meeting", "meeting brief",
        "draft reply", "client message", "inbox", "triage email", "correspondence",
        "thank you email", "acknowledgement",
    ],
    "academic_affairs_registry": [
        "academic registrar", "admissions", "credit transfer", "recognition of prior learning",
        "rpl", "course accreditation", "academic integrity", "student records", "conferral",
        "graduation", "timetable", "progression rule", "award certification",
    ],
    "student_experience_success": [
        "student success", "student support", "wellbeing", "student safety", "reasonable adjustment",
        "accessibility service", "equity", "careers", "employability", "complaint", "appeal",
        "student voice", "student engagement", "retention intervention", "critical incident",
    ],
    "library_scholarly_services": [
        "library", "librarian", "database search", "copyright", "open educational resource",
        "oer", "open access", "repository", "scholarly communication", "information literacy",
        "ai literacy", "research metrics",
    ],
    "research_innovation": [
        "research grant", "research ethics", "human research", "animal ethics", "hdr",
        "higher degree research", "research integrity", "research misconduct", "commercialisation",
        "research impact", "research translation", "candidature", "supervision",
    ],
    "governance_risk_assurance": [
        "governing body", "academic board", "board paper", "delegation", "enterprise risk",
        "internal audit", "self-assurance", "privacy impact", "data breach", "regulatory report",
        "attestation", "business continuity", "risk register",
    ],
    "people_culture": [
        "human resources", "people and culture", "workforce plan", "academic staffing",
        "staff qualifications", "scholarship", "work health safety", "whs", "employee relations",
        "performance management", "staff grievance", "psychosocial risk", "succession",
    ],
}

# Specialist selection is intentionally deterministic and inspectable. The
# orchestrator uses these signals after choosing a department; explicit user
# selection always wins.
_SUBAGENT_SIGNALS: dict[str, dict[str, list[str]]] = {
    "marketing_sales": {
        "sales-manager": ["pipeline", "proposal", "opportunity", "qualify", "closing", "sales strategy"],
        "copywriter": ["website copy", "one-pager", "landing page", "tagline", "rewrite", "script"],
        "content-creator": ["linkedin post", "article", "thought leadership", "content", "storyboard", "video brief"],
        "email-marketer": ["email campaign", "email sequence", "newsletter", "nurture", "outreach email"],
        "seo-geo": ["seo", "geo", "search visibility", "ai visibility", "keyword"],
        "social-media-manager": ["social media", "linkedin calendar", "schedule posts", "engagement", "community"],
        "events-webinars-manager": ["webinar", "roundtable", "event", "workshop logistics", "speaker"],
        "partnership-manager": ["partnership", "alliance", "peak body", "edtech partner", "co-sell"],
    },
    "research_intelligence": {
        "sector-intelligence-analyst": ["teqsa", "aqf", "cricos", "policy update", "competitor", "institution profile"],
        "data-scientist": ["qilt", "heims", "model", "forecast", "benchmark data", "statistical"],
        "research-analyst": ["literature review", "research design", "evidence review", "case study", "methodology"],
        "insights-strategist": ["recommendation", "position paper", "client briefing", "strategic implication", "options"],
    },
    "learning_design": {
        "researcher": ["pedagogy research", "learning evidence", "education literature", "learning framework"],
        "instructional-designer": ["curriculum", "learning outcome", "assessment design", "constructive alignment", "course architecture"],
        "course-developer": ["module content", "activity", "workbook", "rubric", "course script", "learning materials"],
    },
    "academic_development": {
        "academic-trainer": ["staff training", "facilitation", "session plan", "workshop", "faculty development"],
        "personal-growth-agent": ["coaching", "development plan", "cpd", "career", "personal growth"],
    },
    "support": {
        "virtual-assistant": ["schedule", "meeting prep", "follow-up tracking", "admin", "minutes"],
        "triage-agent": ["triage", "classify message", "route message", "inbox", "urgent"],
        "responder-agent": ["draft reply", "respond", "client reply", "prospect reply", "acknowledge"],
        "account-manager": ["renewal", "account health", "client check-in", "client relationship", "expansion"],
    },
    "operations": {
        "business-assistant": ["business plan", "process document", "coordination", "vendor", "operating procedure"],
        "data-analyst": ["kpi", "dashboard", "operational report", "performance report", "data quality"],
        "project-manager": ["project plan", "milestone", "timeline", "status update", "dependency"],
        "recruiter": ["hire", "recruit", "candidate", "screening", "onboarding staff"],
        "finance-reporter": ["invoice", "budget", "p&l", "cash flow", "financial model", "pricing model"],
        "legal-contracts": ["contract", "nda", "sow", "agreement", "legal terms"],
        "technology-digital": ["website", "crm", "infrastructure", "tool stack", "security", "integration"],
        "quality-reviewer": ["quality review", "final qa", "release check", "proofread", "compliance check"],
        "policy-regulatory-advisor": ["regulatory advice", "compliance", "hesf", "esos", "government policy"],
        "process-automation-manager": ["automate", "workflow", "n8n", "process improvement", "human in the loop"],
    },
    "academic_affairs_registry": {
        "academic-registrar": ["academic regulation", "student record", "registrar", "delegation", "progression decision"],
        "admissions-credit-manager": ["admission", "credit", "rpl", "entry requirement", "recognition of prior learning"],
        "course-accreditation-manager": ["course accreditation", "course approval", "aqf mapping", "professional accreditation", "course amendment"],
        "academic-integrity-officer": ["academic integrity", "misconduct", "assessment security", "contract cheating", "integrity case"],
        "timetabling-progression-manager": ["timetable", "class schedule", "enrolment rule", "progression checkpoint", "teaching capacity"],
        "awards-graduation-officer": ["graduation", "conferral", "testamur", "award certification", "completion check"],
    },
    "student_experience_success": {
        "student-success-adviser": ["student success", "early alert", "progression support", "academic support", "retention intervention"],
        "wellbeing-safety-officer": ["wellbeing", "student safety", "critical incident", "safeguarding", "crisis referral"],
        "accessibility-equity-adviser": ["reasonable adjustment", "accessibility service", "equity", "disability support", "barrier"],
        "careers-employability-adviser": ["career", "employability", "work integrated learning", "wil", "graduate outcome"],
        "complaints-appeals-officer": ["complaint", "appeal", "grievance", "procedural fairness", "review decision"],
        "student-engagement-voice-manager": ["student voice", "student engagement", "student representation", "belonging", "co-design"],
    },
    "library_scholarly_services": {
        "academic-librarian": ["library search", "database", "collection", "research librarian", "systematic search"],
        "copyright-oer-adviser": ["copyright", "licence", "permission", "oer", "open educational resource"],
        "scholarly-communications-adviser": ["open access", "repository", "publishing", "research visibility", "research metric"],
        "digital-ai-literacy-librarian": ["information literacy", "ai literacy", "source verification", "misinformation", "digital literacy"],
    },
    "research_innovation": {
        "research-development-grants-manager": ["research grant", "funding call", "grant proposal", "funder", "research budget"],
        "research-ethics-officer": ["research ethics", "ethics approval", "participant", "consent", "human research"],
        "hdr-manager": ["hdr", "candidature", "supervision", "milestone", "thesis examination"],
        "research-integrity-officer": ["research integrity", "authorship", "research misconduct", "data fabrication", "responsible conduct"],
        "research-impact-commercialisation-manager": ["research impact", "commercialisation", "translation", "industry research", "impact pathway"],
    },
    "governance_risk_assurance": {
        "governance-secretary": ["board paper", "agenda", "minutes", "delegation", "conflict of interest", "academic board"],
        "enterprise-risk-manager": ["risk register", "enterprise risk", "control", "incident", "business continuity"],
        "privacy-data-protection-officer": ["privacy", "personal information", "pia", "data breach", "cross-border data"],
        "internal-audit-self-assurance-manager": ["internal audit", "self-assurance", "control test", "audit finding", "assurance plan"],
        "regulatory-reporting-manager": ["regulatory report", "attestation", "submission", "reporting calendar", "data reconciliation"],
    },
    "people_culture": {
        "hr-business-partner": ["human resources", "hr policy", "organisation design", "people matter", "workplace relations"],
        "workforce-planning-manager": ["workforce plan", "capacity", "succession", "capability map", "workforce model"],
        "academic-workforce-standards-adviser": ["academic qualification", "equivalence", "scholarship", "academic workload", "staffing standard"],
        "work-health-safety-officer": ["whs", "work health safety", "hazard", "psychosocial", "return to work"],
        "performance-employee-relations-manager": ["performance management", "employee relations", "staff grievance", "conduct", "workplace complaint"],
    },
}


def select_subagent(query: str, department: str | None) -> tuple[str | None, float, str]:
    """Select the best specialist inside a known department."""
    signals = _SUBAGENT_SIGNALS.get(department or "")
    if not signals:
        return None, 0.0, "No specialist map for department"
    q = query.lower()
    scores = {slug: sum(1 for signal in terms if signal in q) for slug, terms in signals.items()}
    best = max(scores, key=scores.get)
    score = scores[best]
    if score == 0:
        return None, 0.0, "No specialist signal matched; department director should triage"
    return best, min(0.55 + 0.1 * score, 0.95), f"{best} matched {score} specialist signal(s)"

_RAG_SIGNALS: list[str] = [
    "search", "find", "retrieve", "look up", "what is", "explain",
    "summarise", "summarize", "web", "news", "latest", "recent",
    "obsidian", "note", "document", "transcript",
]


def _score_dept(query: str) -> dict[WijerCoDept, int]:
    q = query.lower()
    scores: dict[WijerCoDept, int] = {d: 0 for d in _DEPT_SIGNALS}
    for dept, signals in _DEPT_SIGNALS.items():
        for sig in signals:
            if sig in q:
                scores[dept] += 1
    return scores


def classify_intent(query: str) -> IntentClassification:
    """
    Classify the query. Returns an IntentClassification with the routing target.
    """
    q = query.lower()
    dept_scores = _score_dept(q)
    rag_score = sum(1 for sig in _RAG_SIGNALS if sig in q)

    best_dept: WijerCoDept = max(dept_scores, key=lambda d: dept_scores[d])  # type: ignore
    best_dept_score = dept_scores[best_dept]

    # Hybrid: strong signals in both RAG and a WijerCo dept
    if best_dept_score >= 2 and rag_score >= 1:
        return IntentClassification(
            target="hybrid",
            department=best_dept,
            confidence=min(0.5 + 0.1 * (best_dept_score + rag_score), 0.95),
            reason=f"Both {best_dept} ({best_dept_score} signals) and RAG ({rag_score} signals) detected",
        )

    # Strong WijerCo department signal
    if best_dept_score >= 2:
        return IntentClassification(
            target=best_dept,
            department=best_dept,
            confidence=min(0.4 + 0.1 * best_dept_score, 0.95),
            reason=f"Department '{best_dept}' matched {best_dept_score} keyword signals",
        )

    # RAG-only
    if rag_score >= 1 and best_dept_score == 0:
        return IntentClassification(
            target="rag",
            department=None,
            confidence=min(0.5 + 0.1 * rag_score, 0.9),
            reason=f"RAG retrieval signals ({rag_score}) with no department match",
        )

    # Weak signals — default to hybrid (safest fallback)
    return IntentClassification(
        target="hybrid",
        department=best_dept if best_dept_score > 0 else None,
        confidence=0.3,
        reason="Ambiguous query — defaulting to hybrid routing",
    )


# ---------------------------------------------------------------------------
# Department display metadata (used by the UI)
# ---------------------------------------------------------------------------

DEPT_META: dict[str, dict] = {
    "learning_design": {
        "label":       "Learning Design",
        "emoji":       "📐",
        "color":       "#6366f1",
        "description": "Curriculum, course design, assessments, learning materials",
    },
    "academic_development": {
        "label":       "Academic Development",
        "emoji":       "🎓",
        "color":       "#8b5cf6",
        "description": "Workshops, CPD, coaching, facilitation for academic staff",
    },
    "marketing_sales": {
        "label":       "Marketing & Sales",
        "emoji":       "📣",
        "color":       "#ec4899",
        "description": "Proposals, thought leadership, outreach, LinkedIn, content",
    },
    "operations": {
        "label":       "Operations",
        "emoji":       "⚙️",
        "color":       "#f59e0b",
        "description": "Invoicing, project tracking, dashboards, business planning",
    },
    "research_intelligence": {
        "label":       "Research & Intelligence",
        "emoji":       "🔬",
        "color":       "#10b981",
        "description": "Sector intel, TEQSA/AQF, benchmarking, evidence & data",
    },
    "support": {
        "label":       "Support",
        "emoji":       "📬",
        "color":       "#3b82f6",
        "description": "Email triage, client correspondence, scheduling, follow-ups",
    },
    "academic_affairs_registry": {
        "label": "Academic Affairs & Registry", "emoji": "🏛️", "color": "#2563eb",
        "description": "Academic regulations, admissions, progression, records, integrity and awards",
    },
    "student_experience_success": {
        "label": "Student Experience & Success", "emoji": "🧭", "color": "#16a34a",
        "description": "Student support, wellbeing, accessibility, engagement, careers and complaints",
    },
    "library_scholarly_services": {
        "label": "Library & Scholarly Services", "emoji": "📖", "color": "#0f766e",
        "description": "Information access, copyright, open scholarship and digital literacy",
    },
    "research_innovation": {
        "label": "Research & Innovation", "emoji": "🧪", "color": "#7c3aed",
        "description": "Research development, ethics, integrity, HDR, impact and translation",
    },
    "governance_risk_assurance": {
        "label": "Governance, Risk & Assurance", "emoji": "🛡️", "color": "#b45309",
        "description": "Boards, risk, privacy, internal assurance and regulatory reporting",
    },
    "people_culture": {
        "label": "People & Culture", "emoji": "👥", "color": "#be185d",
        "description": "Workforce strategy, academic staffing, safety and employee relations",
    },
    "rag": {
        "label":       "Knowledge RAG",
        "emoji":       "🔍",
        "color":       "#64748b",
        "description": "Local notes, web search, cloud storage retrieval",
    },
    "hybrid": {
        "label":       "Hybrid",
        "emoji":       "⚡",
        "color":       "#f97316",
        "description": "RAG context + WijerCo advisory synthesis",
    },
}
