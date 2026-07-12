# Legacy Note: Production Pipeline

This document used the older implementation numbering. In the integrated
roadmap, the production pipeline belongs to **Phase 3: Content Studio
Workflows**.

The production pipeline turns Content Studio work into a persistent, inspectable
pipeline. A production record moves through:

`idea -> brief -> research -> outline -> draft -> asset_plan -> render -> review -> publish -> measure`

## What It Provides

- SQLite-backed `productions` and `production_events` tables in the media DB.
- State-machine helpers in `orchestrator/production.py`.
- Content Studio agent handoffs for each forward transition.
- Manual transition support for operator overrides and returns.
- Board data at `GET /production/board`.
- Command Centre controls to create, inspect, advance, and move productions.
- n8n workflow `13-content-studio-pipeline.json` for automated progression.

## Routes

- `GET /production`
- `GET /production/{production_id}`
- `POST /production`
- `POST /production/{production_id}/advance`
- `POST /production/{production_id}/transition`
- `GET /production/board`

Writes require admin access. Read routes use the normal API access path.

## Agent Map

- `idea -> brief`: `brief-builder`
- `brief -> research`: `research-producer`
- `research -> outline`: `scriptwriter`
- `outline -> draft`: `scriptwriter`
- `draft -> asset_plan`: `storyboarder`, then `visual-director`
- `asset_plan -> render`: `editor`, plus render preparation
- `review -> publish`: `qa-brand-reviewer`, gate-checked
- `publish -> measure`: no agent, records progression

## Command Centre

Open **Delivery -> Production** to use the board. Operators can create a new
production, select a card, inspect populated slices and transition events,
advance the next stage, or manually move a production to another state when
review work needs to return upstream.
