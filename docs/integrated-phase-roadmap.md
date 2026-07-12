# Integrated Phase Roadmap

This is the canonical roadmap from July 2026 onward. Earlier multimodal build
notes used a narrower implementation numbering; those pieces are now folded
into the operating phases below.

## Phase 3: Content Studio Workflows

Goal: run the complete content production chain:

`brief -> research -> script -> storyboard -> render -> review -> publish`

Integrated components:

- Content Studio roster and specialist subagents.
- Production records and state machine.
- Media library, asset collections, and media moments.
- Render props, Remotion templates, and derived output registration.
- Command Centre Production board and n8n content-studio workflow.
- Publish-blocking governance gates where the workflow reaches review/publish.

Primary files:

- `orchestrator/production.py`
- `orchestrator/production_media.py`
- `orchestrator/wijerco_roster.py`
- `orchestrator/wijerco_agent.py`
- `media/render.py`
- `media/registry.py`
- `ui/command_centre.html`
- `n8n-workflows/13-content-studio-pipeline.json`

## Phase 4: Autonomous Operating Layer

Goal: make the system operate itself with controlled autonomy.

Scope:

- Planner and task state for multi-step agent work.
- Approval gates and approval history.
- Daily brief and scheduled operating cadence.
- Project memory and long-term recall.
- Command Centre surfaces for pending work, approvals, memory, schedule, and
  production state.

Integrated components already present:

- Agentic tool executor for Content Studio actions.
- Production gate approvals and audit logging.
- Memory store and memory overview.
- Productivity dashboard, daily schedule, projects, and capture actions.
- Quality cockpit and work queue from Phase 1 quality ops.

Primary files:

- `orchestrator/agent_executor.py`
- `orchestrator/governance.py`
- `orchestrator/dashboard.py`
- `orchestrator/eval_store.py`
- `memory/memory_store.py`
- `ui/command_centre.html`

## Phase 5: Product-Grade Deployment

Goal: turn the ecosystem into a reliable product-grade system.

Scope:

- Database migrations and backup/restore procedures.
- CI checks for Python, TypeScript, tests, and security scans.
- RBAC beyond API/admin keys.
- Monitoring, deep health checks, traces, and alerts.
- Versioned agent releases and rollback.
- Reproducible dependency management.

Integrated components already present:

- Deep health checks and request traces.
- Audit log and backup helpers.
- Security baseline and pre-commit config.
- Unit and eval test suites.
- Tool registry and runtime availability checks.

Primary files:

- `common/security.py`
- `common/health.py`
- `orchestrator/trace.py`
- `orchestrator/main.py`
- `media/tool_registry.py`
- `tests/`
- `.pre-commit-config.yaml`

## Current Integration Status

- Phase 3 is functionally in place.
- Phase 4 is partially in place; approvals, memory, schedules, and agent tools
  exist, but the planner/task-state layer should be formalised next.
- Phase 5 has foundations, but migrations, RBAC, CI, monitoring dashboards, and
  versioned agent releases need a dedicated hardening pass.
