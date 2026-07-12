# WijerCo n8n Workflows

Automations that wrap the agent workforce. Scheduled ones run on n8n's clock;
webhook ones are triggered on demand (and become callable tools once the n8n
MCP connection is live). All call the orchestrator's `/hybrid` endpoint, routing
to a specific employee agent via `force_route` (department) + `subagent` (slug).

## The set

| # | File | Agents used | Trigger |
|---|---|---|---|
| 1 | morning-briefing | Orchestrator | Weekdays 06:30 |
| 2 | weekly-knowledge-refresh | (indexer) | Monday 05:00 |
| 3 | weekly-self-improvement | Self-Harness loop | Monday 06:00 |
| 4 | sector-intelligence-watch | Senna (Sector Intelligence) | Friday 07:00 |
| 5 | call-any-agent | **any of the 25** | Webhook `/wijerco-agent` |
| 6 | content-pipeline | Remy → Vero → Pax | Tuesday 07:00 |
| 7 | prospect-intake | Reni → Sol → Esme | Webhook `/wijerco-prospect` |
| 8 | support-triage | Tally → Echo | Webhook `/wijerco-inbound` |
| 9 | monthly-finance-report | Fenn (Finance Reporter) | 1st of month 08:00 |
| 10 | policy-regulatory-monitor | Mira (Policy Advisor) | Monday 08:00 |
| 11 | learning-design-scope | Isla → Cory | Webhook `/wijerco-course` |
| 12 | quality-review-gate | Quincy (Quality Reviewer) | Webhook `/wijerco-qa` |
| 13 | content-studio-pipeline | Content Studio team plus media generation | Manual |

## Coverage of the workforce

Every department has at least one dedicated workflow. Every individual agent is
reachable through **#5 Call Any Agent** by posting `{department, subagent, query}`
to its webhook — so Indra, Dax, Otto, Indie, Quill, Lex, Tycho, Theo, Gray, Ada
and the rest are all one call away even without a bespoke pipeline. The dedicated
pipelines (#4, #6–#12) wire the multi-agent chains that recur often enough to be
worth wrapping.

## Import

n8n → Workflows → **Import from File** → pick each JSON. They import **inactive**.

## What you attach (I can't enter secrets)

- **Email node** (in #1, #2, #3, #4, #6, #8, #9, #10): select one **SMTP**
  credential. Create it under n8n → Credentials → New → SMTP:
  host `smtp.gmail.com`, port `465`/`587`, your Gmail, a Gmail **App Password**.
  Reuse the same credential everywhere.
- Webhook workflows (#5, #7, #8, #11, #12) need no credential.

## Host address

n8n is in Docker; the orchestrator is native on Windows, so the workflows call
`http://host.docker.internal:8000` (and `:8005`). Correct for Docker Desktop.
If you ever run n8n natively, change these to `http://localhost:...`.

## Turn on

Open each workflow, attach the email credential where present, hit **Test
workflow** once to confirm, then flip **Active** (top-right).

## Calling the webhook workflows

Each webhook node shows a Production URL (e.g. `http://localhost:5678/webhook/
wijerco-agent`). POST JSON to it. Examples:

- Call any agent: `{"department":"research_intelligence","subagent":"insights-strategist","query":"..."}`
- Prospect: `{"prospect":"University of X, new DVC Academic, exploring online delivery"}`
- Triage: `{"message":"<the incoming email text>"}`
- Course: `{"brief":"12-week postgrad unit on data-informed leadership"}`
- QA gate: `{"draft":"<text to review>"}`

Once the orchestrator↔n8n MCP auth is resolved, these same webhooks are
invocable by the agents themselves as MCP tools.

## Content Studio media flow

Workflow #13 expects an input item with `production_id`. It loops the production
through `/production/{id}/advance`. When the production reaches `asset_plan`, it
calls `/production/{id}/generate-plan` for image, voice, avatar, and animation
jobs, then continues into render and review.

Generated media is registered in `data/media.db` and linked back to the
production. Publish is still blocked by governance until claims, generated media,
client-sensitive assets, and external publication are approved where required.
