# WijerCo Handoff Contract

Every multi-step dispatch must include:

```text
TASK: stable task id
FROM: orchestrator or requesting specialist
TO: accountable specialist
OUTCOME: decision or artifact this work enables
INPUTS: source artifacts and authoritative context
EXPECTED OUTPUT: format and acceptance criteria
CONSTRAINTS: scope, privacy, regulatory, accessibility and budget limits
DEADLINE: date or this session
DECISION OWNER: authorised human or named governance body
NEXT: next specialist, Quality Reviewer or decision owner
```

Every return must include:

```text
TASK: stable task id
FROM: specialist
STATUS: complete | blocked | needs-decision
OUTPUT: artifact or durable pointer
SOURCES: evidence and provenance
ASSUMPTIONS: material assumptions
RISKS: open risks and controls
DECISION NEEDED: one specific question, if any
NEXT: recommended owner
```

Do not pass hidden chain-of-thought. Pass evidence, concise rationale, decisions and artifacts.
