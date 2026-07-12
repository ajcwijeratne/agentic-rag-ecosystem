"""WijerCo workforce roster loaded from the canonical institutional catalogue."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


CATALOGUE_PATH = Path(__file__).resolve().parents[1] / "workforce" / "agent-catalogue.json"
AVATAR_PATH = Path(__file__).resolve().parents[1] / "ui" / "avatars"
_DEPARTMENT_AVATAR_FALLBACK = {
    "marketing_sales": "content-creator",
    "research_intelligence": "research-analyst",
    "learning_design": "instructional-designer",
    "academic_development": "academic-trainer",
    "support": "virtual-assistant",
    "operations": "project-manager",
    "academic_affairs_registry": "instructional-designer",
    "student_experience_success": "personal-growth-agent",
    "library_scholarly_services": "research-analyst",
    "research_innovation": "research-producer",
    "governance_risk_assurance": "policy-regulatory-advisor",
    "people_culture": "recruiter",
}


def _avatar(slug: str, department: str) -> str:
    selected = slug if (AVATAR_PATH / f"{slug}.svg").exists() else _DEPARTMENT_AVATAR_FALLBACK[department]
    return f"/app/avatars/{selected}.svg"


@lru_cache(maxsize=1)
def _catalogue() -> dict:
    return json.loads(CATALOGUE_PATH.read_text(encoding="utf-8"))


def get_roster() -> dict:
    """Return the 12-department institutional org chart for the Command Centre."""
    departments = []
    for department in _catalogue()["departments"]:
        agents = []
        for agent in department["agents"]:
            agents.append({
                **agent,
                "department": department["key"],
                "department_label": department["label"],
                "color": department["color"],
                "initial": agent["name"][0].upper(),
                "avatar": _avatar(agent["slug"], department["key"]),
            })
        departments.append({
            "key": department["key"],
            "label": department["label"],
            "emoji": department["emoji"],
            "color": department["color"],
            "blurb": department["blurb"],
            "count": len(agents),
            "agents": agents,
        })
    return {
        "version": _catalogue()["version"],
        "operating_model": _catalogue()["operating_model"],
        "department_count": len(departments),
        "agent_count": sum(d["count"] for d in departments),
        "departments": departments,
    }


@lru_cache(maxsize=1)
def _slug_index() -> dict[str, dict]:
    index: dict[str, dict] = {}
    for department in get_roster()["departments"]:
        for agent in department["agents"]:
            index[agent["slug"]] = agent

    # Preserve production-pipeline and historical API slugs while routing them
    # to the canonical specialist. They are capabilities, not departments.
    for alias, canonical in _catalogue().get("capability_aliases", {}).items():
        target = index[canonical]
        index[alias] = {**target, "slug": alias, "canonical_slug": canonical, "is_alias": True}
    return index


def lookup_subagent(slug: str) -> dict | None:
    return _slug_index().get(slug)


def canonical_subagent(slug: str) -> str | None:
    agent = lookup_subagent(slug)
    if not agent:
        return None
    return agent.get("canonical_slug", agent["slug"])
