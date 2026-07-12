# Independence layer: what was built and what you must do

Built 5 July 2026 against the plan in
`OUTPUTS/Projects/WijerHQ/independence-plan/agentic-system-independence-plan.md`.
This document is the handover: the new components, how they connect, and the
manual steps that only you can do (secrets, hardware, activation).

## New components

| Component | File | Runs as |
|---|---|---|
| Operating daemon | `orchestrator/daemon.py` | `python -m orchestrator.daemon` / `rag-daemon.service` |
| Budget breaker | `orchestrator/cost_tracker.py` (added `budget_status`, `month_to_date_cost`) | library, called by daemon |
| Inbox front door | `orchestrator/inbox.py` (mounted in `main.py`) | part of the orchestrator |
| Daemon controls | `POST /operating/daemon/pause`, `/resume`, `GET /status` | part of the orchestrator |
| Unified recall | `memory/recall.py` | library |
| Telegram channel | `channels/telegram_bot.py` | `python -m channels.telegram_bot` / `rag-telegram.service` |
| Email channel | `channels/email_poller.py` | `python -m channels.email_poller` / `rag-email.service` |
| Mini PC installer | `deploy/install.sh` | run once on the Linux box |
| Watchdog | `deploy/watchdog.sh` + systemd timer | every 5 minutes |
| Migration runbook | `deploy/README-minipc.md` | you, one evening |
| Tests | `tests/unit/test_independence.py` | `pytest tests/unit/test_independence.py` |
| Memory consolidation | `memory/consolidation.py` | nightly by the daemon (CONSOLIDATION_HOUR) or `python -m memory.consolidation` |
| Approval links | `orchestrator/inbox.py` (`GET /governance/approve-link`) | signed HMAC tokens in notification emails |
| Cockpit daemon panel | injected into `ui/command_centre.html` | status, pause/resume, inbox quick-send, bottom right |
| Cowork MCP server | `mcp_server/cowork_mcp.py` | register as stdio server in Claude Desktop / Cowork |
| Weekly rehearsal | `deploy/rehearsal.sh` + `rag-rehearsal.timer` | Monday 05:30, result notified |
| CI | `.github/workflows/ci.yml` | compile sweep, unit tests, shell checks, gitleaks |
| Phase 2 tests | `tests/unit/test_independence_phase2.py` | consolidation + approval links |

## How the loop works

1. Work arrives. Any channel posts `{channel, sender, text}` to `POST /inbox`.
   The inbox classifies it: an approve/reject command goes to governance, a
   `plan: <goal>` prefix generates a full operating plan, a question is
   answered directly with unified memory recall as context, anything else
   becomes an operating task assigned to the daemon.
2. The daemon (own process, 60-second cycle) syncs approval and production
   queues, asks `operating.recommend_next_action()` for the highest-priority
   unblocked task, and dispatches by type. `agent` tasks run through
   `call_wijerco_agent` with plan context and project memory. `production`
   tasks advance their production by one state. `memory` tasks write to
   project memory and the semantic store. `approval` and `manual` tasks
   notify you once and wait. The daemon never approves anything.
3. Results land in the task note, `logs/daemon.jsonl`, and (for blocked or
   waiting work) a notification through the existing Apprise channels.

## Guardrails

- Concurrency 1: one task per cycle.
- Two strikes: a task failing twice is marked blocked and you are notified.
- Budget: set `MONTHLY_BUDGET_USD`; at 80 percent the daemon warns once, at
  100 percent it stops dispatching paid work. Computed from the persisted
  `logs/cost_log.jsonl`, so restarts do not reset the month.
- Kill switch: `/operating/daemon/pause` from the cockpit, `/pause` from
  Telegram. Paused state survives restart.
- Channel security: the Telegram bot answers exactly one chat ID; the email
  poller processes only `EMAIL_ALLOWED_SENDERS`; inbound text is data, never
  daemon instructions; gated actions are reachable only through the explicit
  approve command, attributed to the sender.
- Dry run: `DAEMON_DRY_RUN=1` logs what would run without executing. Start
  there.

## What only you can do

1. Env vars on the machine that runs the stack. Append to `.env`:
   `MONTHLY_BUDGET_USD`, `DAEMON_INTERVAL_SEC=60`, `DAEMON_DRY_RUN=1`,
   `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_CHAT_ID` (both fall back to the
   existing `APPRISE_TELEGRAM_*` values), `EMAIL_IMAP_HOST`, `EMAIL_USER`,
   `EMAIL_PASS`, `EMAIL_ALLOWED_SENDERS`.
2. n8n: set Header Auth on the MCP Server Trigger node, put `N8N_MCP_TOKEN`
   and `N8N_MCP_HEADER` in `.env`, attach the SMTP credential, activate the
   13 workflows, confirm `GET /n8n/tools` lists them.
3. Telegram: message @BotFather if you want a dedicated bot rather than
   reusing the Apprise one; either token works.
4. Mini PC: follow `deploy/README-minipc.md`. Until then everything above
   also runs on the Windows PC; start the daemon with
   `python -m orchestrator.daemon` and the bot with
   `python -m channels.telegram_bot` in two terminals.
5. First live run: create one plan
   (`POST /operating/plans/generate`), watch `logs/daemon.jsonl` in dry run,
   then set `DAEMON_DRY_RUN=0`.

## Remaining manual configuration

Everything in the plan is now code. What remains is configuration:

- `PUBLIC_BASE_URL` (your Tailscale serve URL) to render one-click approval
  links into emails; `APPROVAL_LINK_SECRET` if you want a dedicated secret.
- `RBAC_ROLE_KEYS` JSON for viewer / operator / admin keys; falls back to
  `API_KEY` and `ADMIN_API_KEY`.
- Register `mcp_server/cowork_mcp.py` in Claude Desktop / Cowork (snippet in
  the file header) so Cowork sessions use this system as their brain.
- Push to GitHub to activate CI; the workflow extends the existing one with
  the channels and mcp_server compile sweep and deploy script checks.
- The cockpit daemon panel appears bottom right after the orchestrator
  restarts; a `PRE-DAEMON` backup of command_centre.html sits alongside it.
