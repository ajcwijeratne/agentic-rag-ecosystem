# WijerCo Agentic System — Operations Runbook

Consolidates what previously lived scattered across memory notes, `deploy/README-minipc.md` (a Linux migration path that was drafted but never taken — wijerco runs Windows, same as wijwork), and `docs/clone-and-video-runbook.md`. This is the one document to work from if wijerco needs to be rebuilt, restarted, or debugged from nothing.

Both machines run the same repo (`agentic-rag` on wijwork, `agentic-rag-ecosystem` on wijerco) on Windows. wijwork is the dev laptop; wijerco is the always-on production mini PC, reachable only over Tailscale.

Last verified against the live system: 31 August 2026.

## 1. Boot and autostart

The one-click launcher is `scripts\launch.ps1`, run from the repo root:

```
.\scripts\launch.ps1
```

It runs in order: Docker Desktop → `docker compose up -d` (Qdrant, Ollama, n8n, SearXNG) → wait for each to answer health checks → pull the Ollama models named by `OLLAMA_MODEL`/`EMBED_MODEL` in `.env` → `scripts\start_all.ps1` (the seven core API services: orchestrator, local_data_agent, search_agent, cloud_agent, indexer, retriever, notifier) → `scripts\start_channels.ps1` (the operating daemon, plus Telegram/email channels if their credentials are in `.env`) → open the Command Centre UI (the installed PWA if present, else an app-mode browser window).

The daemon and channel workers are deliberately a separate script from the seven core services — `start_all.ps1`'s own comments note they're long-lived and must retain their own tracking files, so a restart of the core services doesn't also bounce the daemon.

**Autostart on boot is not currently registered.** `scripts\register_scheduled_tasks.ps1 -Autostart` will register a logon task that runs `Start RAG Ecosystem.bat`, but this is a deliberate, standing decision (it also implicitly turns on the daemon and any configured Telegram/email channels every time the machine starts) — do this only when you've decided you want that, not as part of a routine rebuild.

Without autostart, after any reboot: open a PowerShell prompt in the repo root and run `.\scripts\launch.ps1` by hand.

## 2. The update path (wijwork → wijerco)

One command on wijwork ships a change to wijerco: `deploy\wijerco-update-2026-08-16\deploy.bat` (or run from wherever the current deploy bundle lives — check `deploy/` for the newest dated folder). It auto-detects which repo it's running against (`C:\dev\agentic-rag-ecosystem` if present, else `C:\dev\agentic-rag`), so the same script runs unmodified on either machine.

It bundles the changed files, ships them to wijerco through the n8n `remote_deploy` webhook (Header Auth protected, path-prefix allowlisted to `ui/`, `n8n/workflows/`, `docker-compose.yml`, with a symlink guard and an append-only audit log at `deploy/webhook_audit.log`), installs them, and verifies: `/health`, `/kb/overview`, `/app/command_centre.html` all return 200, the service worker cache version on disk matches what was shipped, a known-fixed nav bug hasn't regressed, and the deploy webhook itself answers 403 to an unauthenticated probe (proof it's still locked down, not proof it's broken).

**After every deploy: hard-refresh the Command Centre in the browser (Ctrl+F5) once.** The PWA's service worker caches UI assets aggressively; without a hard refresh you'll see the old UI even though the new files are on disk. This has been mistaken for a failed deploy before — it isn't.

Plaintext credential files used during the deploy are deleted from both the source and target locations as the last bundling step; if a deploy is interrupted partway, check for and remove `remote_deploy_credentials.json` by hand.

## 3. Backup and restore

**Backup** runs nightly via the `AgenticRAG-NightlyBackup` Windows scheduled task, which calls `deploy\backup.ps1`. It writes a timestamped zip to `C:\Users\ajwij\OneDrive\Documents\Agents\agentic-rag-backups\agentic-rag-backup-<timestamp>.zip` containing `manifest.json`, the SQLite databases under `data/` (media.db, harness.db, sessions.db, evals.db), `.env`, `logs/cost_log.jsonl`, and Qdrant snapshots for every collection.

**Restore drill**: `deploy\restore_drill.ps1` (no arguments needed — it finds the newest backup automatically) extracts the archive into a throwaway temp folder, runs `PRAGMA integrity_check` against every SQLite file, confirms `.env` and the cost log restored non-empty, and — the part that actually proves the backup is usable, not just present — restores every Qdrant snapshot into a `restore_drill_<collection>`-prefixed throwaway collection, checks the point count, then deletes the throwaway collection and the temp folder. Nothing production is touched. Run time is a few seconds.

Every run appends one JSON line to `logs/restore_drill.jsonl` (timestamp, archive, elapsed time, full per-check results) — check that file for a track record rather than trusting that "a drill was probably run at some point."

To restore for real (not a drill): extract the chosen backup zip, copy `data/*.db` and `.env` back into the repo, and for each Qdrant collection use the same snapshot-upload approach `restore_drill.ps1` uses — POST the `.snapshot` file to `/collections/<name>/snapshots/upload?priority=snapshot`, but to the real collection name, not a `restore_drill_`-prefixed one.

## 4. Rebuilding the indexer / vector store

```
.venv\Scripts\python.exe -m rag.indexer --vault "C:\Users\ajwij\OneDrive\Documents\Obsidian Vault"
.venv\Scripts\python.exe -m rag.indexer --wijerco
```

**Gotcha**: always invoke the indexer (and every other service) through `.venv\Scripts\python.exe`, never bare `python` from PATH. Bare `python` resolves to the system interpreter, which is missing the venv's site-packages, and fails with `ModuleNotFoundError: fastapi` / `uvicorn` / `apprise` — this is exactly why `start_all.ps1` and `start_channels.ps1` both hard-code the venv path rather than trusting PATH.

Re-indexing from scratch is the recommended way to move the vector store to a new machine (rather than copying the Qdrant Docker volume) — it also backfills the provenance fields (`section`, `modified_at`, `chunk_id`, `collection`) that older chunks predate. See `docs/runbook.md` for the full provenance/re-index background.

## 5. API keys and RBAC

**Current state (deliberately incomplete — Stage 1 item 13 is still open):** a single `API_KEY` covers every role including admin. `common/rbac.py` already fully supports `RBAC_ROLE_KEYS` (a JSON map of role name to key) with a fallback to `API_KEY` → operator and `ADMIN_API_KEY` → admin, and `role_for_request()` treats loopback requests as admin regardless of key. The code needs no changes; only `.env` needs the new keys generated and set. This has not been done yet — do it deliberately, not as a drive-by edit, since it touches every credential in the system.

**Rotation procedure:** generate new key values, update `.env` on wijwork and wijerco (they do not currently share a value, so both need updating), restart every service that reads the key at startup (the seven core services plus the daemon/channels — a full `launch.ps1` re-run is the simplest way to be sure everything picks up the change), then confirm with `/ops/me` (returns the resolved role for the request) before relying on the new keys.

**Hygiene lesson learned the hard way this session**: never run a broad grep/`Select-String` pattern against `.env` or any secrets file in a way that could print full matching lines containing values — `Select-String -Pattern "API_KEY"` prints the entire matching line, not just whether the key exists. Only ever use boolean-only checks (`[bool](Select-String ... -Quiet)`) or an exact-anchored pattern for a key already known to be non-secret (e.g. `^DAEMON_DRY_RUN=`) when scripting against `.env`.

## 6. Telegram bot recovery

`channels/telegram_bot.py` reads `TELEGRAM_BOT_TOKEN` (falls back to `APPRISE_TELEGRAM_TOKEN`) and `TELEGRAM_ALLOWED_CHAT_ID` (falls back to `APPRISE_TELEGRAM_CHAT_ID`). If either is unset, the process exits immediately with `SystemExit` rather than starting half-configured — check `logs\telegram.err` first if the bot never comes up.

Every message and every inline-button callback from a chat other than `ALLOWED_CHAT_ID` is silently dropped and logged, never answered. **This is by design, not a bug** — if the bot appears to ignore you, confirm you're messaging from the chat ID actually configured, not that the bot is broken.

To restart just the bot: stop the process tracked by `logs\telegram.pid` and re-run `scripts\start_channels.ps1` (it's safe to re-run — it checks each PID file and skips anything already running, so re-running after fixing one thing won't double-start the others).

## 7. Watchdog and weekly rehearsal

Both are new as of this session (Stage 1 item 14) and mirror the Linux `deploy/watchdog.sh` / `deploy/rehearsal.sh` design for Windows:

- `scripts\watchdog.ps1` — meant to run every 5 minutes via Task Scheduler. Checks port liveness for all 7 core services plus the daemon heartbeat plus `/health/deep`; restarts a service only after two consecutive failed checks (one blip never restarts anything), and only notifies (never auto-restarts) on a repeated deep-health failure, since Docker's own restart policy already covers the containerized dependencies.
- `scripts\rehearsal.ps1` — meant to run weekly (Monday 05:30). Runs migrate → backup → restore dry-run → release snapshot → rollback dry-run → monitoring → verdict against the live orchestrator. Every step is either idempotent, additive-only, or dry-run-gated, so it's safe to run anytime, including by hand. Currently reports `needs_attention` for exactly one reason: `rbac_keys_configured: false` — that's item 13 above, not a new problem.

Neither is registered as a running Task Scheduler job yet — `scripts\register_scheduled_tasks.ps1` (no `-Autostart` flag) creates both. This has been written and tested but deliberately not run, pending a decision on when to turn on standing automation on the production machine.

## 8. Sector Intel data pipeline

Real DoE/QILT source URLs replaced the fixture-era placeholders and the pipeline has been run against them (Stage 1 item 5): `sector-intel/data/published/sector_intel.json` has `meta.sample: false`, so the "indicative sample data" banner in the Command Centre correctly does not show.

To rebuild: `python -m src.run --publish` from `sector-intel/`. Check `data/published/validation_report.json` afterward — as of this writing it reports 9 open flags: one unmatched institution name (`Batchelor Institute of Indigenous Tertiary Education` — not in `reference/institutions.csv` or `reference/name_aliases.csv`; needs a decision on whether it belongs in the 44-institution benchmarking cohort at all, not just a mechanical add) and 8 year-over-year swings beyond the ±60% continuity band (`A05` for Sydney/Divinity/UQ in 2023 all move in the same direction, which smells more like a shared parsing edge case than eight unrelated real-world events; the `A04`/`D03` swings for Notre Dame, CDU, and Murdoch are more plausibly real — international student numbers moved a lot across 2022–2023 as Australia's borders reopened, and Notre Dame/CDU both carry unusually large external-delivery cohorts). None of this blocks the pipeline being "real" rather than fixture data, but none of it should go into a client-facing briefing unreviewed.

## 9. Media / clone pipeline — parked

The voice and avatar clone workers (ports 8020/7861) are not operational on wijwork and there is no evidence they were ever started on wijerco. Setup requires manual steps only Aaron can do (recording a voice reference, setting `AVATAR_PORTRAIT`, installing the MuseTalk checkout and weights) — see `docs/clone-and-video-runbook.md` for the full setup sequence if and when this becomes a priority. Until then, treat this capability as dormant: it should not appear on a health dashboard as failing, because it isn't meant to be running.

## 10. Tailnet performance

**Update (31 Aug, both machines online simultaneously):** the "relayed through syd, not direct" reading in the original note below turned out to be a stale/idle artifact, not the real picture. `tailscale status --json` shows `Relay: "syd"` for the wijerco peer at rest, but that field just reports the last-negotiated fallback and doesn't update in real time — the field that matters is `CurAddr`. Right after `tailscale ping wijerco`, `CurAddr` populated as `192.168.68.107:41641` — a LAN address, because wijwork and wijerco sit on the same home subnet. Once that direct path was warm, four back-to-back `curl` requests to the cockpit (port 8080) came back in 20-190ms each, not 11 seconds. So the direct path works fine and is fast; it just needs a packet to flow before Tailscale bothers to re-establish it after a period of idleness, and `tailscale status` alone (with no recent traffic) makes it look permanently relayed when it isn't.

Separately, and not the same issue: port 8000 (the orchestrator) is not reachable at all from wijwork over the tailnet — four `curl` attempts each hit the 15s timeout with `connect: 0.000000s` (a dropped SYN, i.e. a firewall silently dropping the packet, not a slow response or an active refusal). This matches the plan's own item-4 finding that `wijerco:8000` is unreachable directly and access is meant to go through Tailscale Serve instead — expected behaviour, not a bug, and not the same "11 second page load" symptom as above (which was about the cockpit UI, not the orchestrator API).

Net effect: the direct-connection path is healthy and fast now that both machines are up together. If the 11-second page loads recur, the next things to check are (a) whether it's specifically the *first* load after a period of idleness (consistent with the warm-up cost above, and not really fixable beyond "the second click is fast"), or (b) something at the application layer — payload size, a cold container, a slow upstream call the cockpit's first request triggers — since DERP/relay latency alone was never a plausible explanation for multi-second loads. `tailscale netcheck` on wijerco itself is still the right tool if a *cold* direct connection turns out to be slow to establish (as opposed to just not yet established), but that remains out of reach from this session (no device bridge to wijerco) and wasn't needed to explain what was actually observed here.

<details>
<summary>Original note (before wijerco came back online)</summary>

wijwork ↔ wijerco is relayed through the Sydney DERP server, not direct (`tailscale status --json` shows `CurAddr: ""`, `Relay: "syd"` for the wijerco peer). `tailscale netcheck` on wijwork shows a healthy, NAT-friendly picture — UPnP port mapping active, a consistent public mapping regardless of destination, 22ms to the Sydney DERP — so wijwork is not the side blocking a direct connection. wijerco's own `netcheck` hasn't been captured (it was offline at time of writing); if page loads are still slow, the next step is running `tailscale netcheck` on wijerco itself to check for symmetric NAT, a missing UPnP/NAT-PMP mapping, or a firewall blocking the UDP range Tailscale uses for hole-punching. Note that DERP relay latency alone (tens of milliseconds) doesn't obviously explain multi-second page loads, so a slow direct connection may not be the whole story — worth checking for a second contributing cause (large asset payloads, cold-start latency in a proxied service) before assuming DERP is the sole culprit.

</details>

## 11. Known gotchas (quick reference)

- **venv interpreter**: always `.venv\Scripts\python.exe`, never bare `python`. See §4.
- **PWA hard-refresh**: Ctrl+F5 after every deploy, or you'll be debugging a UI that already shipped. See §2.
- **OneDrive placeholder hydration**: a repo or vault living inside a OneDrive-synced folder can silently truncate files that haven't been "hydrated" from the cloud yet — this corrupted a stale `agentic-rag-ecosystem` checkout's own `.git` directory earlier in the project's history (superseded 24 July; the working copy itself is scheduled for manual deletion, separate from this runbook). Keep the live repo outside any OneDrive-synced path; OneDrive is fine as a backup *destination* (see §3), just not as the location the live code or vault runs from.
- **Qdrant health signal**: fixed as of Stage 1 item 11 — `/health/qdrant` retries once after a short pause before reporting unhealthy, and the Docker healthchecks for Qdrant/Ollama now do a real HTTP GET rather than a bare TCP connect. If you see it flapping red again, that's a regression, not the old known issue.
- **Daemon dry-run default**: `DAEMON_DRY_RUN=1` by default — the daemon logs what it would do rather than acting. This gates autonomous plan execution specifically; it does not gate the daemon's routine nightly memory-consolidation pass (episodic entries older than `EPISODIC_RETENTION_DAYS`, default 90, get pruned regardless of dry-run — that's routine housekeeping, not a "plan action").

## Rebuild-from-nothing checklist

1. Install Docker Desktop, Python (matching the repo's target version), Tailscale; join the tailnet.
2. Clone the repo, create the venv (`python -m venv .venv`), install requirements.
3. Restore `.env` and `data/*.db` from the newest backup (§3) — or copy from the other machine if this is a fresh second node, not a disaster recovery.
4. `docker compose up -d`; confirm Qdrant and Ollama pass their health checks.
5. Re-index (§4) if not restoring the Qdrant volume directly.
6. `.\scripts\launch.ps1`.
7. Confirm `/health/deep` returns `ok`, the Command Centre loads (Ctrl+F5 once), and — if this machine should run the daemon/channels — that `logs\daemon.log` and `logs\telegram.log` (if configured) show clean startup.
8. Only once satisfied this machine is meant to be a standing production node: register the watchdog and rehearsal scheduled tasks (§7), and separately decide about autostart-on-boot (§1).
