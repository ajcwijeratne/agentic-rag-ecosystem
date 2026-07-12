# Phase 2 Media Collections

Phase 2 builds on the media registry, ingestion, indexing, and search layers by
adding curated asset collections. A collection is a project-ready pack of assets:
evidence, visuals, b-roll, transcripts, source documents, and generated media
that belong together for a content or client workflow.

## Endpoints

- `GET /asset-collections` lists collections, optionally filtered by `project` or `status`.
- `POST /asset-collections` creates a collection.
- `GET /asset-collections/{collection_id}` returns assets plus readiness.
- `PATCH /asset-collections/{collection_id}` updates collection metadata or status.
- `DELETE /asset-collections/{collection_id}` archives a collection.
- `POST /asset-collections/{collection_id}/assets` adds an asset with a role.
- `DELETE /asset-collections/{collection_id}/assets/{asset_id}` removes an asset.

## Command Centre

The Delivery rail now includes **Media library**. Operators can search assets,
create project media packs, add or remove assets, inspect readiness, sync the
pack status, and archive completed packs without leaving the Command Centre.

## Readiness

Each collection reports:

- `total`: number of assets in the pack.
- `ready`: assets whose registry status is `ready`.
- `rights_ok`: assets with `owned` or `licensed` rights.
- `indexed`: assets with recorded embedding ids.
- `risky_assets`: assets with unknown, third-party, or confidential rights.
- `is_ready`: true when the pack has assets and all are ready with usable rights.

## Example

```powershell
$pack = Invoke-RestMethod -Method Post http://localhost:8000/asset-collections `
  -ContentType "application/json" `
  -Body '{"name":"July LinkedIn evidence pack","project":"WijerCo","purpose":"content"}'

Invoke-RestMethod -Method Post "http://localhost:8000/asset-collections/$($pack.collection_id)/assets" `
  -ContentType "application/json" `
  -Body '{"asset_id":"<asset-id>","role":"evidence"}'

Invoke-RestMethod "http://localhost:8000/asset-collections/$($pack.collection_id)"
```

Use collections as the handoff point between media search and production:
search or ingest assets, curate them into a pack, then generate briefs, scripts,
slides, or videos from a known-ready set.
