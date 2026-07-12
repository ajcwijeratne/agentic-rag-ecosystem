from __future__ import annotations

import pytest

from orchestrator.wijerco_roster import get_roster, lookup_subagent
from orchestrator.wijerco_router import classify_intent, select_subagent
from scripts.validate_workforce import validate


def test_workforce_structure_and_skills():
    assert validate() == {
        "departments": 12,
        "agents": 62,
        "role_skills": 62,
        "capability_skills": 56,
        "skills": 118,
        "aliases": 9,
    }


def test_roster_has_requested_department_counts():
    roster = get_roster()
    assert roster["department_count"] == 12
    assert roster["agent_count"] == 62
    assert {d["key"]: d["count"] for d in roster["departments"]} == {
        "marketing_sales": 8,
        "research_intelligence": 4,
        "learning_design": 3,
        "academic_development": 2,
        "support": 4,
        "operations": 10,
        "academic_affairs_registry": 6,
        "student_experience_success": 6,
        "library_scholarly_services": 4,
        "research_innovation": 5,
        "governance_risk_assurance": 5,
        "people_culture": 5,
    }


def test_legacy_content_capability_routes_to_canonical_department():
    scriptwriter = lookup_subagent("scriptwriter")
    assert scriptwriter["department"] == "marketing_sales"
    assert scriptwriter["canonical_slug"] == "copywriter"


def test_orchestrator_selects_department_and_specialist():
    classification = classify_intent("Create an AQF-aligned curriculum and assessment design")
    assert classification.department == "learning_design"
    subagent, confidence, _ = select_subagent(
        "Create an AQF-aligned curriculum and assessment design", classification.department
    )
    assert subagent == "instructional-designer"
    assert confidence > 0.5


def test_orchestrator_routes_regulatory_monitoring():
    classification = classify_intent("Find the latest TEQSA policy update and prepare an institution profile")
    assert classification.department == "research_intelligence"
    subagent, _, _ = select_subagent(
        "Find the latest TEQSA policy update and prepare an institution profile", classification.department
    )
    assert subagent == "sector-intelligence-analyst"


@pytest.mark.parametrize(
    ("query", "department", "subagent"),
    [
        ("Process a credit transfer application and RPL decision", "academic_affairs_registry", "admissions-credit-manager"),
        ("Design a student wellbeing and critical incident referral process", "student_experience_success", "wellbeing-safety-officer"),
        ("Develop library copyright and OER guidance", "library_scholarly_services", "copyright-oer-adviser"),
        ("Prepare a human research ethics submission and participant consent review", "research_innovation", "research-ethics-officer"),
        ("Prepare academic board papers, delegations and an internal audit plan", "governance_risk_assurance", "governance-secretary"),
        ("Verify staff qualifications, equivalence and scholarship requirements", "people_culture", "academic-workforce-standards-adviser"),
    ],
)
def test_university_functions_route_to_accountable_specialist(query, department, subagent):
    classification = classify_intent(query)
    assert classification.department == department
    selected, confidence, _ = select_subagent(query, department)
    assert selected == subagent
    assert confidence > 0.5


def test_new_specialist_prompt_loads_role_and_capabilities():
    from orchestrator.wijerco_agent import _build_system_prompt

    prompt = _build_system_prompt(
        "academic_affairs_registry",
        subagent="academic-integrity-officer",
    )
    assert "Academic Integrity Officer" in prompt
    assert "Capability skill: academic-integrity" in prompt
    assert "authorised human approval" in prompt
