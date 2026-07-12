"""Validate the generated WijerCo workforce and skill catalogue."""

from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
WORKFORCE = ROOT / "workforce"
NAME = re.compile(r"^[a-z0-9-]{1,63}$")


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise AssertionError(f"missing frontmatter: {path}")
    _, raw, _ = text.split("---", 2)
    values = {}
    for line in raw.strip().splitlines():
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def validate() -> dict[str, int]:
    catalogue = json.loads((WORKFORCE / "agent-catalogue.json").read_text(encoding="utf-8"))
    departments = catalogue["departments"]
    agents = [agent for department in departments for agent in department["agents"]]
    assert catalogue["operating_model"] == "one-orchestrator-twelve-departments"
    assert len(departments) == 12
    assert len(agents) == 62
    assert "content_studio" not in {department["key"] for department in departments}
    slugs = [agent["slug"] for agent in agents]
    assert len(slugs) == len(set(slugs))

    role_skills = list((WORKFORCE / "SKILLS" / "role-suites").glob("*/SKILL.md"))
    capability_skills = list((WORKFORCE / "SKILLS" / "capabilities").glob("*/SKILL.md"))
    assert len(role_skills) == 62
    assert len(capability_skills) == 56
    assert len(role_skills) + len(capability_skills) == 118

    known_capabilities = {path.parent.name for path in capability_skills}
    for agent in agents:
        assert agent["roles"], f"no role lenses: {agent['slug']}"
        assert set(agent["skills"]).issubset(known_capabilities)
        assert (WORKFORCE / "AGENTS" / "subagents" / f"{agent['slug']}.md").exists()
    for path in role_skills + capability_skills:
        metadata = _frontmatter(path)
        assert set(metadata) == {"name", "description"}, f"invalid metadata fields: {path}"
        assert NAME.fullmatch(metadata["name"]), f"invalid skill name: {path}"
        assert metadata["description"], f"empty description: {path}"

    return {
        "departments": len(departments),
        "agents": len(agents),
        "role_skills": len(role_skills),
        "capability_skills": len(capability_skills),
        "skills": len(role_skills) + len(capability_skills),
        "aliases": len(catalogue.get("capability_aliases", {})),
    }


if __name__ == "__main__":
    print(json.dumps(validate(), indent=2))
