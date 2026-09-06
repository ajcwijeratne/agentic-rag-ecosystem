# Session handoff, 6 September 2026

Written so a new chat can pick this up cold. Read this first, then
`docs/router-calibration-2026-09-04.md` and
`docs/wijerco-operations-runbook.md`.

## The machines

| | wijwork | wijerco |
|---|---|---|
| role | GPU dev desktop, where Aaron works | headless production mini PC, the brain |
| repo | `C:\dev\agentic-rag` | `C:\dev\agentic-rag-ecosystem` |
| tailnet | 100.64.137.16 | 100.109.75.69 |
| reachable by Claude | yes, via Desktop Commander and the connected folders | no shell at all |

Both clone `github.com/ajcwijeratne/agentic-rag-ecosystem`, which is **public**.

## One action unblocks most of what is open

On wijerco, run:

    C:\dev\agentic-rag-ecosystem\scripts\wijerco-secure.bat

It diagnoses why port 8000 is down, quarantines any plaintext deploy
credential, rotates the n8n webhook secret, writes three RBAC keys into `.env`,
restarts, and verifies the roles separate. Every step is independent; a failure
reports and moves on. It writes its log to
`%OneDrive%\Documents\Agents\agentic-rag-ecosystem\_logs`, which syncs to a
folder Claude can read directly on wijwork, so the next session can collect the
result without Aaron pasting anything.

Do not run anything named `wijerco-secure*` from wijerco's Downloads. Older
broken copies are there and Taildrop renames rather than overwrites.

## State on 6 Sep

Stage 1 is 15 of 16 closed. Item 6, router calibration, closed today. Item 13,
RBAC, is done on wijwork and outstanding on wijerco.

Four commits today, all pushed, full unit suite 232 passing after each:

- `cf4263a` router: route on the kind of work, not the subject matter
- `129dab2` tune_router: count both arms of the gate, not just one
- `f01b75c` security: stop shipping the deploy webhook secret in a committed zip
- `be24c3e` wijerco-secure v2: no step can kill the run, and the log has a way home

### Router

73 queries in `data/router_labels.jsonl`, labelled on both axes, reviewed and
accepted by Aaron, now tracked in git. Task type 60.3% to 89.0%, department
34.2% to 76.7%. Holdout, never seen during tuning: 80.0% and 84.0%. Three
defects fixed: substring matching that let "research" fire "search", department
vocabulary contaminating the advisory signal list, and a department gate that
required two keyword hits when real queries give one.

### Security

The n8n deploy webhook secret was public on GitHub from 29 Aug to 4 Sep inside a
committed zip. The endpoint is tailnet-only, path-allowlisted and symlink
guarded, so it was never internet facing, but the secret still needs rotating
and that is step 3 of the script above. The zip and the loose credential are in
`C:\dev\_quarantine\2026-09-04-deploy-credential` on wijwork for Aaron to delete.

## Open, in priority order

1. **wijerco orchestrator is down.** Port 8000 has not answered all session
   while 5678, 6333 and 11434 do. Cause unknown until the script runs.
2. **Rotate the webhook secret.** Step 3 of the script.
3. **RBAC keys on wijerco.** Step 4. This is the machine that is served over
   Tailscale, so it is the one that matters.
4. **The deploy webhook answers 404, not 403**, which means that workflow is not
   active in n8n at all. Confirm once the machine is healthy.
5. **Aaron to delete** `C:\dev\_quarantine\2026-09-04-deploy-credential` on
   wijwork, and deal with `deploy\set-wijerco-api-key.ps1`, which still holds a
   live API key in plaintext. Gitignored now, so it cannot be committed, but it
   should not stay on disk.
6. **QILT permission request** sent 1 Sep, still unanswered. Aaron chose to
   include the SES metrics with attribution; the standing advice is DoE-only
   until permission lands, because seven of the ten metrics are DoE and
   commercially clear.
7. **Stage 2.** Package the Sector Intel briefing as a productised service and
   find two or three design partners. Selling problem before a building problem.

## Decisions already made, do not reopen without a reason

- Staged plan: finish the internal system, then productise. Both, in that order.
- Generated agent output is **evidence**. It is verified before it can enter
  recallable memory. `memory/consolidation.py` does the entailment check.
- No git history rewrite for the leaked secret. Rotation kills it; a rewrite
  changes every hash and breaks wijerco's clone for no security gain.
- The deploy webhook stays. It was not the weakness.
- `ROUTER_MIN_CONFIDENCE` 0.45 and `ROUTER_MIN_MARGIN` 0.15 are **unchanged**,
  pending Aaron's call. Loosening the margin to 0.0 buys 89.0% to 91.8% and
  releases two exact ties that currently misroute to advisory. Both options are
  costed in `docs/router-calibration-2026-09-04.md`. This is his decision.

## Traps that cost real time today

- **git is not on PATH on wijerco.** GitHub Desktop bundles its own. Command
  line `git pull` fails there; use GitHub Desktop. `deploy/wijerco-update.bat`
  already works around this.
- **Taildrop from wijerco to wijwork has never delivered.** Not once. There is
  no `wijerco_update_log*.txt` anywhere in the user profile on wijwork and
  nothing queued. Treat Taildrop as one way, to wijerco only.
- **Do not run git from the Cowork Linux VM** (`device_bash`). It sees a
  CRLF checkout through a Linux git and reports 37 files changed when the tree
  is clean, and it cannot remove `.git/index.lock`. Use Desktop Commander for
  every git and PowerShell operation. `device_bash` is fine for reading and
  editing files.
- **PowerShell 5.1**: `$ErrorActionPreference = 'Stop'` plus a native command
  with `2>&1` throws a terminating error the moment that command writes to
  stderr. This killed v1 of the wijerco script at step 1.
- **The unit suite only runs properly in the Windows venv**,
  `.\.venv\Scripts\python.exe -m pytest tests/unit -q`. The Linux VM is missing
  fastapi, httpx and others, and installing them there chases a dependency
  swamp for no benefit.
- Desktop Commander calls time out at 60 seconds. Long jobs should redirect to
  a file and be read back in a second call.

## Quick state check for a new session

    # on wijwork, via Desktop Commander
    cd C:\dev\agentic-rag; git log --oneline -5; git status --short
    foreach ($p in 8000,5678,6333,11434) { (Test-NetConnection 100.109.75.69 -Port $p -InformationLevel Quiet) }

    # has the wijerco log synced back?
    dir "$env:OneDrive\Documents\Agents\agentic-rag-ecosystem\_logs"

## Suggested opening message for the new chat

> Continuing the WijerCo agentic system work. Read
> `docs/session-handoff-2026-09-06.md` in `C:\dev\agentic-rag` first, then tell
> me where we are and what you need from me.
