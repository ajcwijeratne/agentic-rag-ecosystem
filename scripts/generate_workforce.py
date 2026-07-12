"""Generate WijerCo prompt, skill and machine-readable catalogue files."""

from __future__ import annotations

import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workforce.catalogue import AGENTS, CAPABILITY_ALIASES, DEPARTMENTS, VERSION  # noqa: E402

OUT = ROOT / "workforce"

CAPABILITIES = {
    "academic-governance": "Design and evidence effective academic-board authority, delegations, course oversight, student participation and links to corporate governance.",
    "academic-integrity": "Design academic-integrity education, assessment-security, investigation and continuous-improvement controls, including appropriate generative-AI use.",
    "academic-quality": "Design quality assurance, course monitoring, external referencing, moderation, review and continuous-improvement evidence for higher education work.",
    "academic-registry": "Operate authoritative enrolment, progression, results, completion, awards and certification records under approved academic regulations.",
    "academic-workforce": "Assure academic staffing sufficiency, qualifications, equivalence, scholarship, workload and capability.",
    "admissions-and-credit": "Design transparent admissions, credit, recognition-of-prior-learning and progression rules with equitable opportunity for success.",
    "accessibility-and-inclusion": "Apply inclusive design, accessibility, reasonable-adjustment and equitable-participation requirements to content, learning and service artifacts.",
    "ai-governance": "Assess and control AI use cases through risk classification, human accountability, privacy, security, evaluation, monitoring and incident response.",
    "assessment-design": "Design aligned, authentic and AI-aware assessment with clear criteria, integrity controls, feedback and assurance of learning.",
    "campaign-measurement": "Define campaign objectives, measures, attribution assumptions, reporting cadence and improvement decisions.",
    "contracts-and-procurement": "Prepare structured contract and procurement inputs, identify obligations and risks, and escalate legal decisions.",
    "content-production": "Turn an approved brief into accessible, evidence-led written or multimedia content with rights, brand and release checks.",
    "complaints-and-appeals": "Design accessible, timely and procedurally fair complaint, grievance, review and appeal processes with independent escalation.",
    "copyright-and-oer": "Apply copyright, licensing, permissions, open-resource and rights-management practices to teaching and scholarly materials.",
    "course-accreditation": "Coordinate course approval and accreditation evidence, AQF alignment, external review, amendments, renewal and teach-out.",
    "course-development": "Develop modules, activities, assessment assets, workbooks and scripts from an approved design specification.",
    "curriculum-design": "Design AQF-aware course architecture, outcomes, sequencing, constructive alignment and approval evidence.",
    "digital-ai-literacy": "Build critical information, source, data and AI literacy, including verification, limitations, ethical use and disclosure.",
    "enterprise-risk": "Maintain enterprise risk, controls, incidents, continuity, scenario testing, reporting and accountable risk acceptance.",
    "evidence-synthesis": "Search, appraise, triangulate and synthesise evidence with traceable claims, uncertainty and implications.",
    "executive-briefs": "Convert complex evidence into concise decision briefs with options, trade-offs, risks and a recommended decision.",
    "faculty-development": "Design capability programs, workshops, facilitation, coaching and evaluation for academic staff.",
    "flipped-learning": "Design purposeful pre-class, facilitated and post-class learning that preserves alignment and learner workload.",
    "governance-self-assurance": "Build governing-body and academic-governance evidence showing that controls operate effectively and drive accountable improvement.",
    "implementation-readiness": "Assess people, process, technology, data, governance, change and benefit-realisation readiness before launch.",
    "institutional-analytics": "Define governed institutional metrics, data quality, benchmarking, models, dashboards and decision use.",
    "higher-degree-research": "Govern HDR admission, candidature, supervision, milestones, examination, researcher development and completion.",
    "internal-audit": "Plan independent assurance, test control design and operation, report findings and verify corrective action.",
    "library-services": "Design scholarly information access, collections, research support, discovery and discipline liaison services.",
    "market-research": "Plan and execute market, competitor, audience and institutional research with explicit sources and limitations.",
    "market-positioning": "Translate evidence into differentiated market position, audience value, proof and message hierarchy.",
    "opportunity-assessment": "Evaluate an opportunity's problem, audience, evidence, strategic fit, value, feasibility, risk and next test.",
    "policy-analysis": "Interpret policy and regulation, map obligations to controls and evidence, and separate advice from legal determination.",
    "privacy-and-data-protection": "Apply privacy principles, data minimisation, impact assessment, access, disclosure, cross-border and breach controls.",
    "positioning-and-messaging": "Create audience-specific message architecture grounded in WijerCo's approved services, positioning and proof.",
    "pricing-and-proposals": "Develop scoped pricing and proposal inputs with assumptions, exclusions, dependencies, approval gates and commercial risk.",
    "records-governance": "Apply records classification, provenance, retention, access, privacy, versioning and disposal rules.",
    "regulatory-reporting": "Coordinate governed reporting calendars, data reconciliation, approvals, attestations, submissions and evidence retention.",
    "research-design": "Design answerable research questions, methods, sampling, ethics, analysis and reporting plans.",
    "research-ethics": "Design ethical review, consent, participant safeguards, monitoring, amendments, incidents and records.",
    "research-governance": "Govern research approvals, funding obligations, data, partnerships, performance and responsible conduct.",
    "research-impact": "Plan, evidence and evaluate research translation, societal impact, industry engagement and commercialisation pathways.",
    "research-integrity": "Promote responsible research and administer concerns about authorship, data, conflicts and research conduct.",
    "risk-and-escalation": "Classify risk, define controls, set escalation thresholds and preserve accountable human decisions.",
    "sales-enablement": "Prepare credible higher-education sales briefs, outreach, proposals, objection responses and handoffs without unsupported claims.",
    "sector-intelligence": "Monitor and interpret Australian higher-education regulatory, policy, competitor and institutional signals.",
    "service-operations": "Design reliable service workflows with intake, ownership, service levels, exceptions, records and outcome measures.",
    "scholarly-communications": "Support responsible publishing, open access, repositories, research visibility and research-metrics literacy.",
    "student-engagement": "Design student partnership, representation, belonging, co-creation and closed feedback loops.",
    "student-support": "Design accessible learner support, wellbeing, safety, complaints, progression and referral workflows with urgent-risk escalation.",
    "student-success-analytics": "Define governed measures and interventions for access, participation, progression, completion, outcomes and cohort equity.",
    "third-party-assurance": "Assess partners and delivery arrangements through due diligence, contracts, student protections, performance monitoring and exit plans.",
    "careers-and-employability": "Design career development, work-integrated learning, employer engagement and graduate-outcome services.",
    "wellbeing-and-safety": "Design student wellbeing, safeguarding, critical-incident, referral and safe-campus controls for physical and online environments.",
    "workforce-governance": "Design equitable workforce policies, role architecture, workforce planning, performance, conduct and employee-relations controls.",
    "work-health-safety": "Design workplace health, safety, hazard, incident, psychosocial risk and recovery-at-work controls.",
}


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def agent_record(row: tuple) -> dict:
    dept, slug, name, role, blurb, roles, skills = row
    return {"department": dept, "slug": slug, "name": name, "role": role, "blurb": blurb, "roles": roles, "skills": skills}


def agent_skill(agent: dict, department_label: str) -> str:
    lenses = "\n".join(f"- {role.title()}" for role in agent["roles"])
    skills = "\n".join(f"- `{skill}`" for skill in agent["skills"])
    return f"""---
name: {agent['slug']}
description: {agent['blurb']} Use for work assigned to the {agent['role']} in WijerCo's {department_label} department.
---

# {agent['role']}

## Mission

{agent['blurb']}

## Role lenses

Select the minimum lenses needed for the task:

{lenses}

## Capability skills

Load only the relevant capabilities:

{skills}

## Workflow

1. Confirm the task, decision owner, audience, output, deadline and downstream consumer.
2. Read shared context and the task-relevant capability skills.
3. State material assumptions and evidence gaps before relying on them.
4. Produce the requested artifact in the handoff contract's expected format.
5. Self-check evidence, privacy, accessibility, risk and voice.
6. Return the artifact, sources, assumptions, decisions needed and recommended next owner.

## Boundaries

- Do not make binding academic, legal, financial, employment, safety or regulatory decisions.
- Do not send, publish, enrol, grade, contract, pay or change a system without authorised human approval.
- Do not invent evidence, clients, institutional data, outcomes or regulatory interpretations.
- Apply data minimisation to personal and confidential information.
"""


def capability_skill(name: str, description: str) -> str:
    return f"""---
name: {name}
description: {description} Use when a WijerCo agent needs this capability to complete or review a task.
---

# {name.replace('-', ' ').title()}

1. Define the decision, audience, scope and required evidence.
2. Load current authoritative sources and relevant WijerCo context.
3. Separate facts, analysis, assumptions, options and recommendations.
4. Produce an artifact with sources, limitations, risks, owners and next actions.
5. Apply the relevant human approval and Quality Reviewer gate before release.

Minimum checks: trace material claims; identify stale or contradictory evidence; minimise personal data; check accessibility and foreseeable learner impact; escalate regulated or high-consequence decisions.
"""


def generate() -> None:
    agents = [agent_record(row) for row in AGENTS]
    departments = []
    for key, (label, emoji, color, blurb) in DEPARTMENTS.items():
        members = [agent for agent in agents if agent["department"] == key]
        departments.append({"key": key, "label": label, "emoji": emoji, "color": color, "blurb": blurb, "agents": members})
        roster = "\n".join(f"- **{a['role']} (`{a['slug']}`):** {a['blurb']}" for a in members)
        write(OUT / "AGENTS" / "departments" / f"{key.replace('_', '-')}.md", f"""# {label} Department

## Mandate

{blurb}

## Roster

{roster}

## Operating rules

- Accept work through the orchestrator or an explicit named-agent request.
- Use the handoff contract for multi-step tasks.
- Distinguish sourced facts, analysis, assumptions and recommendations.
- Route client-, learner- or regulator-facing work through the Quality Reviewer.
- Escalate binding academic, legal, safety, privacy, financial and regulatory decisions to an authorised human.
""")
        for agent in members:
            content = agent_skill(agent, label)
            write(OUT / "AGENTS" / "subagents" / f"{agent['slug']}.md", content)
            write(OUT / "SKILLS" / "role-suites" / agent["slug"] / "SKILL.md", content)

    for name, description in CAPABILITIES.items():
        write(OUT / "SKILLS" / "capabilities" / name / "SKILL.md", capability_skill(name, description))

    catalogue = {"version": VERSION, "operating_model": "one-orchestrator-twelve-departments", "departments": departments, "capability_aliases": CAPABILITY_ALIASES}
    write(OUT / "agent-catalogue.json", json.dumps(catalogue, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    generate()
