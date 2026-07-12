# Multimedia Production Runbook

Use this when the Content Studio needs to produce media from a production record.

## Local tool setup

The system registers local tools in `data/media.db` on first use. Override paths
or endpoints in `.env` when tools are not on `PATH`.

Common settings:

```env
MEDIA_TOOL_FFMPEG_COMMAND=ffmpeg
MEDIA_TOOL_REMOTION_COMMAND=npx
MEDIA_TOOL_COMFYUI_ENDPOINT=http://127.0.0.1:8188
MEDIA_TOOL_PIPER_COMMAND=piper
MEDIA_TOOL_MUSETALK_ENDPOINT=http://127.0.0.1:7860
MEDIA_TOOL_MANIM_COMMAND=manim
MEDIA_TOOL_BLENDER_COMMAND=blender
```

Check what the system sees:

```bash
curl http://localhost:8000/media/tools
```

Disable a tool:

```bash
curl -X PATCH http://localhost:8000/media/tools/remotion \
  -H "Content-Type: application/json" \
  -d "{\"enabled\": false, \"notes\": \"License review pending\"}"
```

## Production flow

Create a production:

```bash
curl -X POST http://localhost:8000/production \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"Launch short\",\"project\":\"WijerCo\",\"format\":\"linkedin_short\",\"owner\":\"Aaron\"}"
```

Advance it until it reaches `asset_plan`:

```bash
curl -X POST "http://localhost:8000/production/PRODUCTION_ID/advance?actor=operator"
```

Preview planned media jobs:

```bash
curl -X POST http://localhost:8000/production/PRODUCTION_ID/generate-plan \
  -H "Content-Type: application/json" \
  -d "{\"dry_run\": true}"
```

Run planned media jobs:

```bash
curl -X POST http://localhost:8000/production/PRODUCTION_ID/generate-plan \
  -H "Content-Type: application/json" \
  -d "{\"capabilities\":[\"image\",\"voice\",\"avatar\",\"animation\"],\"actor\":\"operator\"}"
```

Render the final video:

```bash
curl -X POST http://localhost:8000/production/PRODUCTION_ID/generate \
  -H "Content-Type: application/json" \
  -d "{\"capability\":\"video\"}"
```

## Governance

Generated image, avatar, animation, and video assets are created with pending
review metadata. A production cannot publish until these assets are approved.

See pending gates:

```bash
curl http://localhost:8000/governance/pending
```

Approve generated media:

```bash
curl -X POST http://localhost:8000/governance/approve \
  -H "Content-Type: application/json" \
  -d "{\"gate\":\"generated_image\",\"target_id\":\"PRODUCTION_ID\",\"actor\":\"Aaron\",\"note\":\"Approved for this production\"}"
```

The approval marks linked generated assets as reviewed and unblocks the
`generated_image` gate for that production.

## n8n workflow

Import `n8n-workflows/13-content-studio-pipeline.json`. Start it with an item
containing `production_id`. The workflow advances the production, generates
planned media at `asset_plan`, continues into render, and stops when a gate
blocks or the production finishes its current loop.
