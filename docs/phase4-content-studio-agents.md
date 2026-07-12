# Legacy Note: Content Studio Agents

This document used the older implementation numbering. In the integrated
roadmap, these agents belong to **Phase 3: Content Studio Workflows**.

The Content Studio agent layer activates the department and seven
specialists are now available through the existing WijerCo routes rather than a
separate invocation path.

## Agents

- `brief-builder`: turns a goal into a structured production brief.
- `research-producer`: gathers evidence, citations, and media references.
- `scriptwriter`: writes outlines and scripts to the brief and format.
- `storyboarder`: maps script beats to scenes and reusable assets.
- `visual-director`: writes generation briefs for missing visuals.
- `editor`: prepares captions, cut lists, and format variants.
- `qa-brand-reviewer`: checks voice, claims, accessibility, rights, and gates.

## Invocation

Use the existing endpoints with `subagent`:

```json
{
  "query": "Advance this production record from idea to brief.",
  "force_route": "content_studio",
  "subagent": "brief-builder"
}
```

Supported routes:

- `POST /hybrid`
- `POST /hybrid/stream`
- `POST /wijerco`

When a `subagent` is supplied, the API validates the slug against the roster and
uses the agent's registered department. A mismatched department returns `422`
instead of silently routing to the wrong team.

## Contract

The role files live in the WijerCo folder at:

- `AGENTS/departments/content-studio.md`
- `AGENTS/subagents/{slug}.md`

`orchestrator/wijerco_agent.py` loads the Content Studio department file, then
the selected subagent role file, then the normal WijerCo knowledge and retrieved
context layers. Production state-machine calls can therefore use the same
`call_wijerco_agent(..., department="content_studio", subagent="<slug>")`
contract as the chat routes.
