# Phase 4 Autonomous Operating Layer

Phase 4 formalises the system's operating backbone: persistent plans, task
state, approvals, daily brief, and project memory.

## What It Adds

- `operating_plans`: goals and active operating plans.
- `operating_tasks`: task state across agent, approval, production, memory, and
  manual work.
- `project_memories`: durable project facts used by the operating brief.
- Approval sync: pending governance gates become `waiting_approval` tasks.
- Daily brief: a compact operating summary with priorities, approvals,
  productions, and project memory.
- Intelligent planner: goal decomposition into workflow-specific task plans
  with dependency metadata, risk flags, success criteria, and next-action
  selection.
- Obsidian Projects integration: generated plans can be mirrored to Markdown
  project notes, and project note sections can be imported into planner memory.
- Agent tools for creating plans, adding tasks, reading the daily brief, and
  remembering project facts.
- Command Centre **Operating** view.

## Routes

- `GET /operating/overview`
- `GET /operating/daily-brief`
- `GET /operating/plans`
- `POST /operating/plans`
- `POST /operating/plans/generate`
- `GET /operating/next-action`
- `GET /operating/projects/obsidian-status`
- `POST /operating/plans/{plan_id}/sync-obsidian`
- `POST /operating/projects/import-obsidian`
- `GET /operating/plans/{plan_id}`
- `PATCH /operating/plans/{plan_id}`
- `GET /operating/tasks`
- `POST /operating/tasks`
- `PATCH /operating/tasks/{task_id}`
- `POST /operating/sync-approvals`
- `GET /operating/project-memory`
- `POST /operating/project-memory`

## Agent Tools

- `create_operating_plan`
- `generate_operating_plan`
- `add_operating_task`
- `get_operating_daily_brief`
- `remember_project_fact`
- `sync_operating_plan_to_obsidian`
- `import_obsidian_project_memory`

These are local Command Centre tools, so agents can manage the operating layer
without needing an n8n workflow for every internal step.

## Intelligent Planner

`POST /operating/plans/generate` accepts a goal and optional workflow hint. It
can either preview a plan with `create: false` or create the plan and tasks with
`create: true`.

Supported deterministic workflows:

- `content_studio`: brief -> research -> script -> storyboard -> render ->
  review -> publish
- `deployment`: scope -> migrate -> backup -> rehearse -> monitor -> promote
- `incident`: triage -> contain -> diagnose -> recover -> verify -> postmortem
- `general`: define -> context -> plan -> execute -> review -> close

Each generated task stores planner metadata in `meta.planner`, including
sequence, dependency keys, resolved dependency task IDs, risk flags, and success
criteria. `GET /operating/next-action` returns the highest-priority unblocked
task for a plan or project.

## Obsidian Projects

Set these environment variables:

```text
OBSIDIAN_VAULT_PATH=C:/Users/ajwij/OneDrive/Documents/Obsidian Vault
OBSIDIAN_PROJECTS_PATH=Projects
```

`POST /operating/plans/{plan_id}/sync-obsidian` writes a Markdown note under:

```text
<vault>/<projects>/<project>/<plan title>.md
```

The note includes frontmatter with `planner_plan_id`, project, workflow, status,
owner, and next action. The planner database remains the operational source of
truth; Obsidian is the human-readable project workspace.

`POST /operating/projects/import-obsidian` imports content from these sections
into `project_memories`:

- `## Context`
- `## Decisions`
- `## Risks`
- `## Client Preferences`
