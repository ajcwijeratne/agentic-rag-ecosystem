# Hardening And Operational Rehearsal

This is the remaining hardening list and the rehearsal path for moving the
ecosystem from implemented phases to reliable operation.

## Still Needed List

1. Run the deployment rehearsal before each significant change.
2. Keep at least one recent database backup and verify restore readiness.
3. Snapshot the agent release manifest before release changes.
4. Check monitoring for recent errors and blocked approvals.
5. Confirm RBAC keys for `viewer`, `operator`, and `admin` are configured.
6. Rehearse rollback before switching or promoting an agent release.
7. Record the rehearsal result in the project notes or release log.

## Rehearsal Flow

1. Check `/ops/status` for database, backup, release, and RBAC state.
2. Run `/ops/migrate` to apply idempotent schema migrations.
3. Run `/ops/backup` and confirm the returned backup path.
4. Run `/ops/restore` with `dry_run: true` against the latest backup.
5. Run `/ops/releases/snapshot` before changing the release manifest.
6. Run `/ops/releases/rollback` with `dry_run: true` against the snapshot.
7. Check `/ops/monitoring` for recent trace errors and pending approvals.
8. Check `/ops/rehearsal` and clear every `next_actions` item.

## Hardening Gates

- No destructive restore or rollback should run before a successful dry run.
- No release promotion should happen without a fresh release snapshot.
- No public publishing workflow should bypass pending governance approvals.
- No remote access should be enabled without RBAC role keys configured.
- No production run should proceed when `/ops/rehearsal` reports
  `needs_attention`.

## Next Stage Focus

The next improvements should move from feature completion into operating
discipline:

- scheduled rehearsal runs and daily summaries
- alert thresholds for recent trace errors and stalled approvals
- versioned migration files instead of only inline deployment metadata
- automated restore tests against a disposable database
- explicit release promotion and rollback records
- runbook evidence stored with each release
