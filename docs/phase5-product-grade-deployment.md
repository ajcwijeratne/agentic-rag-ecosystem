# Phase 5 Product-Grade Deployment

Phase 5 hardens the system for reliable operation.

## Added

- Deployment metadata migrations via `orchestrator/deployment.py`.
- SQLite backup creation and backup listing.
- SQLite restore rehearsal and execution with a pre-restore backup.
- `/ops/*` routes for status, migrations, backups, releases, and role identity.
- Release manifest snapshots and rollback rehearsal.
- Monitoring and operational rehearsal summaries.
- RBAC helper layer in `common/rbac.py` with `viewer`, `operator`, and `admin`.
- Legacy admin-only routes now accept the configured RBAC admin key.
- Agent release manifest in `config/agent_releases.json`.
- GitHub Actions CI for Python compile, unit tests, Remotion TypeScript/lint,
  and secret scanning.

## Routes

- `GET /ops/me`
- `GET /ops/status`
- `POST /ops/migrate`
- `POST /ops/backup`
- `POST /ops/restore`
- `GET /ops/backups`
- `GET /ops/releases`
- `POST /ops/releases/snapshot`
- `POST /ops/releases/rollback`
- `GET /ops/monitoring`
- `GET /ops/rehearsal`

## RBAC

Set `RBAC_ROLE_KEYS` to a JSON map:

```json
{"viewer":"...","operator":"...","admin":"..."}
```

If unset, the system falls back to the existing `API_KEY` as operator and
`ADMIN_API_KEY` as admin. Loopback remains admin for local Command Centre use.

## Backup

`POST /ops/backup` copies the SQLite media database to `DB_BACKUP_DIR`, default
`logs/db_backups`.

`POST /ops/restore` defaults to `dry_run: true`; set `dry_run: false` only
after the dry run confirms the managed backup path and target database.

## Releases

`GET /ops/releases` reads `config/agent_releases.json` by default. Use this file
to record agent bundle versions, included tools, and rollback notes.

Use `POST /ops/releases/snapshot` before changing the manifest. Use
`POST /ops/releases/rollback` with `dry_run: true` first, then repeat with
`dry_run: false` only when the rollback target is confirmed.

## Operational Rehearsal

`GET /ops/rehearsal` returns the live readiness checklist for hardening:

- schema migrations current
- recent database backup available
- release manifest available
- RBAC keys configured
- monitoring readable
