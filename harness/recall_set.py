"""
Retrieval recall set: query -> the source file(s) that should appear in the
top-k retrieved chunks. Used by tests/eval/test_retrieval_recall.py.

The offline test seeds a fake BM25 corpus from these terms and asserts recall@k
without any live service. The live variant runs the same queries against the
indexed wijerco_knowledge collection in Qdrant.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RecallCase:
    query:         str
    expected_files: list[str] = field(default_factory=list)
    # Which index the expected file lives in: "knowledge" (wijerco_knowledge)
    # or "agents" (wijerco_agents, the role definitions kept out of client
    # retrieval by rag/indexer.py).
    collection:    str = "knowledge"


RECALL_CASES: list[RecallCase] = [
    RecallCase("diagnostic sprint price cost timeline",
               ["KNOWLEDGE BASE/wijerco-diagnostic-sprint.md"]),
    RecallCase("free triage call entry point new client",
               ["KNOWLEDGE BASE/wijerco-services.md"]),
    RecallCase("WijerCo positioning teaching quality faculty workload AI",
               ["KNOWLEDGE BASE/wijerco-positioning.md"]),
    RecallCase("TEQSA teaching qualification forcing function",
               ["KNOWLEDGE BASE/wijerco-sector-context.md"]),
    RecallCase("competitors Deloitte KPMG PwC EY advisory firms",
               ["KNOWLEDGE BASE/wijerco-competitors.md"]),
    RecallCase("core services list curriculum learning design",
               ["KNOWLEDGE BASE/wijerco-services.md"]),
    # AGENTS/departments — one case per department definition
    RecallCase("learning design department curriculum framework course production arm",
               ["AGENTS/departments/learning-design.md"], collection="agents"),
    RecallCase("support department client communication incoming requests scheduling inbox",
               ["AGENTS/departments/support.md"], collection="agents"),
    RecallCase("operations department project management reporting finance recruitment planning",
               ["AGENTS/departments/operations.md"], collection="agents"),
    # AGENTS/subagents — representative roles
    RecallCase("sales manager business development pipeline prospective clients outreach proposals",
               ["AGENTS/subagents/sales-manager.md"], collection="agents"),
    RecallCase("instructional designer learning outcomes assessment design pedagogical framework architect",
               ["AGENTS/subagents/instructional-designer.md"], collection="agents"),
    RecallCase("research analyst literature review published research sector sources",
               ["AGENTS/subagents/research-analyst.md"], collection="agents"),
    # ABOUT ME — voice and context files
    RecallCase("Aaron academic director OES PhD organisational behaviour Melbourne",
               ["ABOUT ME/about-me.md"]),
    RecallCase("goals pro vice-chancellor academic dean twelve month horizon thought leadership",
               ["ABOUT ME/my-company.md"]),
    RecallCase("banned words em dashes writing style AI tells buzzwords",
               ["ABOUT ME/anti-ai-writing-style.md"]),
]
