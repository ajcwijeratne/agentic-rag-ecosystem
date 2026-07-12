# Command Centre Chat Agent Guide

This guide explains how to activate WijerCo agents from the Command Centre chat, how to run Content Studio stages from chat, and how to tell when an action actually ran.

Open the Command Centre:

```text
http://localhost:8000/app/command_centre.html
```

## Before You Start

Use the chat toolbar at the top of the chat view.

- `Auto` lets the system choose the route.
- `RAG only` searches and answers from the retrieval system.
- `WijerCo` opens the agent roster so you can choose a department and named agent.
- `Actions on` lets the selected agent run Command Centre actions, including Content Studio production tools, and any available n8n workflow tools.

If new chat tools were just added, restart the ecosystem first:

```powershell
.\scripts\start_all.ps1
```

## Activate One Named Agent

1. Click `Chat`.
2. Click `WijerCo`.
3. Pick a department.
4. Pick an agent.
5. Type the task directly.

Example:

```text
Draft a 180-word LinkedIn post in Aaron's voice about why universities should treat AI literacy as academic infrastructure.
```

When a named agent is selected, every message goes to that agent until you click `Team` and choose another agent.

## Activate a Department Without Choosing a Person

Use `Auto` or `WijerCo` and name the department in the prompt.

Example:

```text
Ask the Research & Intelligence team to identify one timely Australian higher education angle for a LinkedIn post, with three evidence points.
```

For more control, choose the named agent from the roster instead.

## Agent Roster

### Marketing & Sales

Use this team for public profile, sales material, outreach, partnerships, and copy.

| Agent | Role | Good Chat Request |
|---|---|---|
| Vero | Content Creator | `Draft a LinkedIn post from this angle and evidence.` |
| Pax | Copywriter | `Edit this into Aaron's voice. Cut filler and return the final version only.` |
| Esme | Email Marketer | `Write a two-email outreach sequence for this prospect.` |
| Otto | SEO / GEO Specialist | `Improve this article for search and AI answer visibility.` |
| Sol | Sales Manager | `Qualify this prospect and recommend the next step.` |
| Indie | Partnership Manager | `Assess this partnership opportunity and suggest an approach.` |
| Reni | Business Development Rep | `Research this prospect and brief me on the likely entry point.` |

### Research & Intelligence

Use this team for evidence, sector movement, analysis, and recommendations.

| Agent | Role | Good Chat Request |
|---|---|---|
| Senna | Sector Intelligence Analyst | `Scan for higher education policy or regulatory developments that matter this week.` |
| Dax | Data Scientist | `Analyse this dataset and tell me the pattern, caveats, and next question.` |
| Remy | Research Analyst | `Synthesize the evidence for this claim and give source-backed points.` |
| Indra | Insights Strategist | `Turn these findings into a strategic recommendation for WijerCo.` |

### Learning Design

Use this team for curriculum, outcomes, assessment, and course material.

| Agent | Role | Good Chat Request |
|---|---|---|
| Isla | Instructional Designer | `Design the learning architecture for this short course.` |
| Cory | Course Developer | `Build module one with activities, materials, and a worked example.` |

### Academic Development

Use this team for staff capability, workshops, coaching, and development plans.

| Agent | Role | Good Chat Request |
|---|---|---|
| Theo | Academic Trainer | `Design a 90-minute workshop for academics on this capability.` |
| Gray | Personal Growth Agent | `Create an individual development plan from these goals and constraints.` |

### Operations

Use this team for delivery, finance, contracts, compliance, systems, and quality.

| Agent | Role | Good Chat Request |
|---|---|---|
| Piers | Project Manager | `Turn this engagement into a project plan with milestones and risks.` |
| Fenn | Finance Reporter | `Summarise the financial position for this month and what needs attention.` |
| Quill | Financial Modeller | `Model the pricing and margin for this engagement.` |
| Lex | Legal & Contracts | `Review these terms and flag risks or required changes.` |
| Mira | Policy & Regulatory Advisor | `Check the regulatory implications of this proposal.` |
| Tycho | Technology & Digital | `Recommend the system setup for this workflow.` |
| Quincy | Quality Reviewer | `Quality-review this draft and give a SEND / REVISE verdict.` |

### Support

Use this team for triage, replies, scheduling, and admin.

| Agent | Role | Good Chat Request |
|---|---|---|
| Tally | Triage Agent | `Triage this incoming message and route it to the right agent.` |
| Echo | Responder Agent | `Draft a reply to this message in Aaron's voice.` |
| Ada | Virtual Assistant | `Prepare follow-ups and scheduling notes from this thread.` |

### Content Studio

Use this team for multimedia production from idea through review.

| Agent | Role | Good Chat Request |
|---|---|---|
| Bria | Brief Builder | `Create a content brief for this video idea.` |
| Sera | Research Producer | `Gather evidence and citations for this production brief.` |
| Scout | Scriptwriter | `Write the script from this brief and research.` |
| Bree | Storyboarder | `Turn this script into a scene-by-scene storyboard.` |
| Vidal | Visual Director | `Create visual generation briefs for each scene.` |
| Cade | Editor | `Create the edit plan, captions guidance, and platform variants.` |
| Wren | QA / Brand Reviewer | `Review this production package for voice, claims, accessibility, and readiness.` |

## Automate Content Studio Stages From Chat

Keep `Actions on` enabled. Then ask chat to create and advance a production.

Example:

```text
Create a Content Studio production for a 60-second talking head clip on AI literacy as academic infrastructure, then advance it through the Content Studio agents until review. Summarise what each agent produced.
```

The chat can use these local tools:

| Tool | What It Does |
|---|---|
| `create_content_production` | Creates a production record. |
| `advance_content_production` | Runs the next production stage once. |
| `advance_content_production_until_blocked` | Keeps advancing until a gate, stopping state, or step limit. |
| `get_content_production` | Retrieves the current production record and generated outputs. |
| `list_content_productions` | Lists recent productions. |

You should see tool chips in the assistant message, such as:

```text
running - create_content_production
ran - advance_content_production_until_blocked
```

If you already have a production ID, use:

```text
Advance production ID <production-id> through the Content Studio stages until review, then summarise the current state and any blocked gates.
```

## Manual Content Studio Chain

If you want to inspect every handoff manually, select each agent in order and paste the previous output into the next prompt:

1. Bria creates the brief.
2. Sera adds research.
3. Scout writes the script.
4. Bree storyboards it.
5. Vidal writes visual briefs.
6. Cade creates the edit plan.
7. Wren reviews the package.

Use this when you want maximum control. Use the automated production tools when you want the system to drive the workflow.

## Run n8n Workflow Actions From Chat

Chat can also use n8n workflow tools when:

1. n8n is running.
2. The workflow has an MCP Server Trigger.
3. The workflow is active.
4. The Command Centre shows the tool under `Automations`.
5. `Actions on` is enabled in chat.

Example:

```text
Use the available n8n tool to run the weekly content workflow and tell me what happened.
```

If no n8n tools are available, local Content Studio production tools still work.

## Useful Prompt Patterns

Create and run:

```text
Create a production for <topic>, format <format>, project <project>, owner Aaron. Advance it until review and summarise each stage.
```

Continue an existing production:

```text
Continue production <production-id> by one stage. Tell me the new state, which agent ran, and what changed.
```

Inspect a production:

```text
Get production <production-id> and summarise the brief, research, script, asset plan, edit plan, review, and current state.
```

Ask one named agent:

```text
Ask <agent name> to <specific task>. Return a concise, usable output.
```

Quality gate:

```text
Ask Wren to review this production package and give READY / REVISE, with specific fixes if it is not ready.
```

## Troubleshooting

If the chat answers but does not run tools:

- Check `Actions on` is enabled.
- Check the route is not `RAG only`.
- Restart the ecosystem if tools were recently added.
- Use a clear verb: `create`, `advance`, `continue`, `get`, or `list`.
- Include the production ID when continuing an existing production.

If the production does not move:

- It may be blocked by a governance gate.
- Ask: `Get production <id> and tell me whether a gate is blocking it.`
- Check the `Production` view in the left rail.

If the wrong agent answers:

- Click `WijerCo`, choose the exact department and named agent, then ask again.
- For automated Content Studio work, ask for a production run rather than a normal chat answer.

## Formats For Productions

Use one of these format values when asking for a production:

- `linkedin_short`
- `explainer_carousel`
- `talking_head_clip`
- `policy_briefing`
- `course_teaser`
- `proposal_walkthrough`

Example:

```text
Create a production titled "AI literacy infrastructure", project WijerCo, format talking_head_clip, owner Aaron, and advance it until review.
```
