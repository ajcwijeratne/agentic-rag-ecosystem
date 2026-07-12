# Phase 3 Media Moments

Phase 3 completes the heavier ingestion path for video, slide decks, and web
pages by promoting extracted pieces into first-class media moments. A moment is
a navigable part of an asset: transcript segment, keyframe, slide, page text, or
web screenshot.

## What Changed

- Video ingestion records transcript moments with timestamps and keyframe
  moments with thumbnail paths and linked child image assets.
- Slide-deck ingestion records one moment per slide, preserving slide text and
  speaker notes.
- Web-page ingestion records a page-text moment and, when screenshots are
  enabled, a screenshot moment linked to the derived image asset.
- Asset search now matches moment labels and moment text, not only tags and
  transcripts.
- `GET /assets/{asset_id}/moments` returns the moment list for inspection and
  downstream production planning.
- The Command Centre Media Library includes an Inspect action that opens an
  asset's moments.

## Moment Shape

Each moment stores:

- `kind`: `transcript`, `keyframe`, `slide`, `page`, or `screenshot`.
- `label`: human-readable marker such as `Slide 3` or `Keyframe 2`.
- `t_start` and `t_end`: seconds for time-based media.
- `text`: transcript, slide, or page text.
- `thumbnail_path`: preview image for visual moments.
- `child_asset_id`: linked derived asset, such as a keyframe image.
- `meta`: worker-specific detail, such as speaker, language, or slide number.

## Handoff

Production agents can now assemble scripts, clip plans, thumbnails, slide
summaries, and evidence packs from asset moments instead of parsing raw worker
metadata. This gives Phase 4 and later phases a stable interface for planning
multimodal content.
