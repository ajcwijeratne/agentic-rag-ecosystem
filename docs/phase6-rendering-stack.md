# Legacy Note: Rendering Stack

This document used the older implementation numbering. In the integrated
roadmap, rendering belongs to **Phase 3: Content Studio Workflows**.

The rendering stack connects production records to renderable multimedia outputs. It uses a
single adapter gateway for media capabilities and a Remotion render service for
repeatable video templates.

## Render Contract

`media.render.build_props(production)` builds the stable props object passed to
Remotion:

- `production_id`, `title`, `project`, `format`, `owner`
- `script`, `asset_plan`, `edit_plan`
- `lines`: flattened script lines
- `captions`: caption strings from the edit plan
- `scenes`: normalized scene cards from the asset plan
- `linked_assets`: source asset ids
- `brand`: WijerCo colours and name

`media.render.render(production_id, template, props)` validates the template,
writes the props file, runs Remotion when available, and otherwise writes a
render-plan placeholder. In both cases it registers a derived video asset and
links it to source assets.

## Templates

The Remotion project in `my-video/` registers six production families:

- `linkedin_short`
- `explainer_carousel`
- `talking_head_clip`
- `policy_briefing`
- `course_teaser`
- `proposal_walkthrough`

The templates render from the shared props contract and cover title-led videos,
scene boards, and caption-led talking-head clips.

## Adapter Gateway

`media.adapters.gateway.select(capability, context)` remains the capability
chokepoint. `self` adapters are the default. `mcp:<name>` adapters are blocked
by the `paid_job` governance gate before any remote spend path runs.

## Production Integration

When a production advances from `asset_plan` to `render`, the production store
builds Remotion props from the full production record, runs the render service,
and appends the resulting derived asset to `linked_assets`.
